# Campus Idle-Compute Cloud

A private compute cloud built from the idle time of campus lab PCs. Each PC
lends itself to the cluster while nobody is using it and is handed back within
seconds when its owner returns. Users submit jobs through a website, the
platform splits them into tasks, runs copies of each task on different PCs,
checks that the copies agree and returns a verified result. When the PCs are
full, it bursts onto AWS EC2.

Built for CSC3104 (Cloud and Distributed Computing) at the Singapore Institute
of Technology.

## Features

- **Idle PCs as workers.** Windows lab PCs run a k3s worker inside WSL2 and
  join the cluster over WireGuard. A sensor on Windows watches keyboard, mouse
  and CPU activity, and the node agent stops taking work and evicts running
  tasks as soon as the owner is back.
- **Verified results.** A task's copies run on different PCs and their result
  hashes are compared by majority vote. Each PC earns a trust score, so trusted
  PCs are only spot-checked while new ones are always double-checked.
- **Checkpoints and recovery.** Work saves progress as it goes. A task taken
  off a reclaimed PC resumes from its last checkpoint on another node.
- **Cloud bursting.** An autoscaler launches bounded EC2 (including Spot)
  workers when queued work outgrows the campus PCs, tracks their cost and
  retires them when they are idle.
- **Serverless page pipeline.** Uploaded PDFs are split and rendered into
  pages by AWS Lambda, in parallel, before the job starts.
- **Job types.** OCR of PDFs (Tesseract), Python scripts, PyTorch training on
  CPU, and PyTorch training on NVIDIA (CUDA) or AMD (ROCm) GPUs, with trained
  models scored on a node the platform controls.
- **Web app and API.** Submit jobs, follow live progress and download results.
  Admins get a dashboard for jobs, users, events, burst cost and campus
  workers, including adding a lab PC to the cluster without a shell on the hub.

## Architecture

```text
             Users (browser / API)
                      |
                      v
   +------------------------------------------+       +-------------------+
   | Hub (AWS EC2)                            |       | S3 + Lambda       |
   |  web app -> coordinator -> verifier      |<----->| PDF splitter and  |
   |  PostgreSQL, autoscaler, k3s control     |       | page renderer     |
   +------------------------------------------+       +-------------------+
          |  WireGuard               |  EC2 API
          v                          v
   Campus PCs (k3s in WSL2)     Burst EC2 workers
```

| Component | Folder | Role |
| --- | --- | --- |
| Web app | [`services/web/`](services/web/README.md) | Website, public job API, admin dashboard |
| Coordinator | [`services/coordinator/`](services/coordinator/README.md) | Splits jobs into tasks and copies, schedules Kubernetes Jobs, recovers interrupted work |
| Verifier | [`services/verifier/`](services/verifier/README.md) | Majority voting, trust scores, final output |
| Autoscaler | [`services/autoscaler/`](services/autoscaler/README.md) | EC2 burst capacity, Spot interruption handling, cost controls |
| Worker agent | [`agent/`](agent/README.md) | Windows idle sensor and Linux node agent |
| Worker images | [`worker/`](worker/) | OCR, Python, PyTorch (CPU, CUDA, ROCm), model checker, generic job runner |
| Page pipeline | [`serverless/`](serverless/README.md) | Lambdas that split and render uploaded PDFs |
| Infrastructure | [`infra/`](infra/README.md) | AWS, WireGuard, k3s manifests, worker enrollment |
| Shared code | [`shared/`](shared/README.md) | Database models, SQL migrations, common helpers |

## Running locally

The root `docker-compose.yml` runs the whole system on one machine, jobs
included: PostgreSQL, MinIO (standing in for S3), the coordinator, verifier and
web app, a k3s control plane and three k3s worker nodes standing in for lab
PCs. You need Docker (or Podman) with Compose. From the repo root:

```sh
docker compose up -d --build
scripts/load-worker-images.sh   # builds the worker images and loads them into the k3s workers
docker compose exec coordinator python scripts/seed_test_users.py   # optional test logins
```

Open http://localhost:8000. The seed script creates two local-only test
logins: `user@example.com` / `password123` and `admin@example.com` /
`admin12345`. A Test workload job (New job page) is the quickest end-to-end
check.

- Run `scripts/load-worker-images.sh` again after changing anything in
  `worker/`. The k3s workers have their own image store and don't see Docker's.
- Page rendering runs inside the web app (`LOCAL_RENDER=true`), so no Lambda is
  needed.
- Inspect the cluster with
  `docker compose exec k3-server kubectl get pods -n idlecloud-jobs`.
- `docker compose down` stops everything. `docker compose down -v` also deletes
  the database, MinIO data and the k3s state.

More detail, such as the network layout, is in
[`infra/README.md`](infra/README.md#local-stack).

## Deploying to AWS

On AWS, the hub is an EC2 instance running the k3s control plane, PostgreSQL,
the coordinator, verifier, web app and autoscaler. Campus PCs join it as k3s
workers over WireGuard, burst EC2 workers are added when the PCs are full, and
two Lambdas split uploaded PDFs into pages in S3.

### Before the first deployment

- The hub runs k3s with `kubectl` access, and its node is labelled
  `idlecloud/role=control`.
- `S3_BUCKET` and `AWS_DEFAULT_REGION` in `infra/k8s/overlays/aws/*-s3.yml`
  name your bucket and its region.
- The autoscaler's Secrets (`k3s-agent-token`, `burst-wg-slots`) and WireGuard
  peers exist. See
  [`services/autoscaler/README.md`](services/autoscaler/README.md#needed-from-member-1).
- The worker images are published and public on GHCR: the Publish worker
  images workflow pushes `ghcr.io/sitprojects/idlecloud-*` images on every
  merge to `main`. A new package is private, so an organisation owner must set
  it to public once (Package settings, Change visibility). Workers download
  images without a login.

### Deploy or rebuild

SSH into the hub, then from the repo root:

```sh
git switch main && git pull --ff-only
bash infra/k8s/deploy.sh             # Postgres, coordinator, web app, verifier
bash services/autoscaler/deploy.sh   # burst autoscaler, Spot watcher, OCR image
```

- Both scripts build their images on the hub, load them into k3s and restart
  the services. Add `--no-build` to redeploy the images already there.
- The coordinator applies new SQL migrations from `shared/migrations/` when it
  starts.
- The autoscaler script ends by pinning the newest published OCR image. It
  fails if the GHCR package isn't public, and keeps the current image while OCR
  jobs are running; run it again once they finish.
- Both scripts are safe to run again and keep existing Secrets.

### Page-splitting Lambdas

Only when `serverless/` changes, on the hub after `infra/k8s/deploy.sh`, with
AWS credentials in `runtime/aws-credentials`:

```sh
python -m serverless.deploy --bucket <your bucket> --web-url https://<web app domain>
```

`--web-url` is only needed the first time. See
[`serverless/README.md`](serverless/README.md#aws-deployment).

### Adding workers

- **Campus PCs (Windows):** sign in as an admin and open **Campus workers**
  (`/admin/workers`). Add the PC by name, download its setup zip and run
  `agent/install-worker.ps1 -Bundle <zip>` on the PC. Once the page shows it as
  joined, download its agent credentials and run the installer again with
  `-AgentCredentials <zip>`. See
  [`agent/README.md`](agent/README.md#set-up-a-worker) and
  [`services/web/README.md`](services/web/README.md#campus-workers-admin).
- **GPU PCs:** NVIDIA and AMD PCs, on Linux or on Windows through WSL2, join
  as GPU workers. See [`infra/README.md`](infra/README.md#gpu-workers).
- **Laptops** (VPN access only) use
  `sudo bash infra/wireguard/add-peer.sh laptop <name>` on the hub.
- **Burst EC2 workers** are launched and ended by the autoscaler. See
  [`services/autoscaler/README.md`](services/autoscaler/README.md).

Check the deployment with `kubectl -n idlecloud-system get pods` and
`kubectl get nodes`.

## Development

The repo includes a Dev Container (`.devcontainer/`) with the Python toolchain,
and [pre-commit](https://pre-commit.com) hooks for linting and formatting:

```sh
uv sync
pre-commit install
```

Each component's README covers how to run and test it on its own.

Keep local settings in ignored `.env` files and commit only `.env.example`
templates. Generated WireGuard configs go in `infra/wireguard/generated/` and
transient runtime files in `runtime/`, both ignored.

## Repository layout

```text
agent/          Windows idle sensor, Linux node agent, worker installer
core/           Shared service settings
docs/           Reports, diagrams, testing notes
eval/           Experiments, raw results and report graphs
infra/          AWS, WireGuard, k3s manifests, worker enrollment
scripts/        Local helper scripts
serverless/     PDF splitter and renderer Lambdas
services/       web, coordinator, verifier, autoscaler
shared/         Database models, SQL migrations, shared helpers
worker/         Job container images and the generic job runner
```

## Team

| Member | GitHub | Contributions |
| --- | --- | --- |
| Ng Ying Xuan | [@Kitsuneez](https://github.com/Kitsuneez) | Coordinator and scheduling, database migrations, infrastructure (WireGuard, k3s, AWS hub), GPU workers |
| Dylan Ho Sheng Xue | [@BlackberryDealer](https://github.com/BlackberryDealer) | Burst autoscaler, serverless page pipeline, Python and PyTorch job types, spot checks |
| Kai Wen Teo | [@kaiwenteoo](https://github.com/kaiwenteoo) | Verifier: majority voting and trust scores |
| Timothy Seow | [@Timothy-Seow](https://github.com/Timothy-Seow) | Web app and public API |
| felicia-chuaxy | [@felicia-chuaxy](https://github.com/felicia-chuaxy) | Node agent, Windows idle sensor and worker installer, eviction evaluation |

## License

[MIT](LICENSE)
