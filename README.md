# Cloud and Distributed Computing

> [!NOTE]
> WIP, remove this note when project is ready.

project_description

---

## Project Scope

- [link_to_source](link_to_source)
  - scope_description

## Usage

### runtime_executable

```sh
./runtime_executable <args> [optional]
```

- runtime_executable_description

## Getting Started

### Prerequisites

- [Git](https://github.com/git-guides/install-git) (fully set-up)
- [Docker/Podman](https://docs.docker.com/engine/install/)
- [VS Code](https://code.visualstudio.com/download)
  - [Dev Containers](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) extension

> [!WARNING]
> Do not use GitHub Desktop! All interactions (files, git, runtime, etc.) should be done through the Dev Container within VS Code.

### Installation

1. Clone the repo

   ```sh
   git clone git@github.com:10x-Slackers/repo_name.git
   ```

2. Open the repository in VS Code

   ```sh
   code repo_name/
   ```

3. Click on the "Re-open in Dev Container" prompt
4. Install the pre-commit hooks

   ```sh
   pre-commit install
   ```

5. Start working!

## Local Deployment

The root `docker-compose.yml` runs the whole system on one machine, jobs
included: PostgreSQL, MinIO (S3), the coordinator, verifier and web app, a k3s
control plane and three k3s worker nodes standing in for lab PCs. Run these on
the host (not in the Dev Container), from the repo root:

```sh
docker compose up -d --build
scripts/load-worker-images.sh   # builds the worker images and loads them into the k3s workers
docker compose exec coordinator python scripts/seed_test_users.py   # optional test logins
```

Open http://localhost:8000. Seeded logins: `user@example.com` / `password123`
and `admin@example.com` / `admin12345`. A Test workload job (New job page) is
the quickest end-to-end check.

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

## Cloud Deployment (AWS)

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
  peers exist. See "Needed from member 1" in
  [`services/autoscaler/README.md`](services/autoscaler/README.md#needed-from-member-1).
- The worker images are published and Public on GHCR: the Publish worker
  images workflow pushes `ghcr.io/sitprojects/idlecloud-{ocr,python,pytorch}`
  on every merge to `main`. A new package is private, so an organisation owner
  must set it to Public once (Package settings, Change visibility). Workers
  download images without a login.

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
  fails if the GHCR package isn't Public, and keeps the current image while OCR
  jobs are running; run it again once they finish.
- Both scripts are safe to run again and keep existing Secrets.

### Page-splitting Lambdas

Only when `serverless/` changes, on the hub after `infra/k8s/deploy.sh`, with
the AWS Academy credentials in `runtime/aws-credentials`:

```sh
python -m serverless.deploy --bucket csc3104-idle-compute --web-url https://<web app domain>
```

`--web-url` is only needed the first time. See
[`serverless/README.md`](serverless/README.md#aws-deployment).

### Adding workers

- **Campus PCs (Windows):** sign in as an admin and open **Campus workers**
  (`/admin/workers`). Add the PC by name, download its setup zip and run
  `agent/install-worker.ps1 -Bundle <zip>` on the PC. Once the page shows it as
  joined, download its agent credentials and run the installer again with
  `-AgentCredentials <zip>`. No shell on the hub is needed. See
  [`agent/README.md`](agent/README.md#set-up-a-worker) and
  [`services/web/README.md`](services/web/README.md#campus-workers-admin).
- **Laptops** (VPN access only) use
  `sudo bash infra/wireguard/add-peer.sh laptop <name>` on the hub.
- **Burst EC2 workers** are launched and ended by the autoscaler. See
  [`services/autoscaler/README.md`](services/autoscaler/README.md).

Check the deployment with `kubectl -n idlecloud-system get pods` and
`kubectl get nodes`.

## Developer Tooling

- Dev Containers
  - Standardised developer environment
- Pre-Commit
  - Run linting and formatting for all files during git commit

## Repository Structure

The repository root corresponds to `idlecloud/` in the Campus Idle-Compute Cloud
implementation plan. These folders establish component ownership; service
implementations will be added separately.

```text
shared/                  # Shared contracts and Python helpers
  migrations/            # SQL migrations
infra/                   # Infrastructure
  aws/
  wireguard/
  k8s/
    base/
    overlays/aws/
agent/                   # Worker management
  windows-sensor/
  node-agent/
services/
  coordinator/           # Scheduling and checkpoint recovery
  verifier/              # Voting and trust scores
  web/                   # Website and public API
    templates/
    static/
  autoscaler/            # Cloud bursting
worker/
  ocr-task/              # OCR container
serverless/              # Lambda page pipeline
  splitter/
  renderer/
eval/
  data/                  # Reusable sample PDFs
  results/               # Experiment CSVs
  graphs/                # Report figures
docs/                    # Reports, diagrams, testing instructions
tests/
  unit/
  integration/
```

| Owner | Responsibilities | Folders |
| --- | --- | --- |
| Member 1 | AWS, WireGuard, K3s, deployment, and local application stack | `infra/` |
| Member 2 | Windows idle detection and Linux node management | `agent/` |
| Member 3 | Scheduling, checkpoints, and recovery | `services/coordinator/` |
| Member 4 | Majority voting, trust scores, and verified output | `services/verifier/` |
| Member 5 | Website, public API, and OCR workload | `services/web/`, `worker/ocr-task/` |
| Member 6 | Burst capacity and serverless page splitting | `services/autoscaler/`, `serverless/` |
| All members | Contracts, evaluation evidence, documentation, and tests | `shared/`, `eval/`, `docs/`, `tests/` |

Member 3 will author shared SQL migrations, with review by the whole team.
Component READMEs describe the responsibilities for each folder.

The existing `src/csc3104/` package, `pyproject.toml`, and `uv.lock` remain in place.
Python package migration and service entry points will be addressed when services
are implemented; this scaffold does not change imports or executable behavior.

The existing `.devcontainer/docker-compose.yml` provides the development
environment. The root `docker-compose.yml` runs the full local application stack
(see [Local Deployment](#local-deployment)).

Keep local environment settings in ignored `.env` files and commit only safe
`.env.example` or `.env.*.example` templates. Put generated WireGuard configs in
`infra/wireguard/generated/` and transient runtime files in root `runtime/`.
Evaluation sample PDFs, result CSVs, graphs, and documentation remain trackable.
