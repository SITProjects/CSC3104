# Infrastructure

Owner: member 1.

- `aws/`: AWS setup scripts and infrastructure configuration templates.
- `wireguard/`: networking setup and safe peer configuration templates. Generated
  configurations containing keys belong in ignored `wireguard/generated/`.
- `k8s/base/`: shared Kubernetes manifests.
- `k8s/overlays/aws/`: Kustomize configuration for AWS deployment.

Member 1 also owns the root `docker-compose.yml` application stack and
image/deployment workflows. The existing dev-container Compose file remains the
development environment configuration.

## Enrollment helper

`infra/enroll/` is a small root service on the hub, `idlecloud-enroll`. The
web admin pages add and remove campus workers through it: it adds their
WireGuard peers and issues K3s join tokens and node-agent credentials, so
nobody needs a shell on the hub for that. Only the coordinator talks to it, on
`/run/idlecloud/enroll.sock`, which only root and the `idlecloud-enroll` group
(GID 10050, given to the coordinator Pod) may use. `infra/k8s/deploy.sh`
installs and updates it with `infra/enroll/install.sh`.

`infra/wireguard/add-peer.sh` stays for laptops and burst slots. It takes the
same lock and uses the same peer table, so the two never hand out one address
twice.

- Settings: `/etc/idlecloud/enroll.env` (`HUB_ENDPOINT`, `JOIN_TOKEN_TTL`, ...;
  `infra/enroll/config.py` lists them). The first install reads the hub's public
  IP from EC2 instance metadata, or pass `--endpoint <ip>:<port>`.
- Peer files stay where `add-peer.sh` always kept them,
  `/etc/wireguard/peers/<name>/`. The peer table moves to
  `/etc/wireguard/peers.md`.
- Logs: `journalctl -u idlecloud-enroll`.

## Local stack

The root `docker-compose.yml` runs the whole system on one machine, jobs included:

```sh
docker compose up -d --build
scripts/load-worker-images.sh   # once, and after changing worker/ocr-task
docker compose exec coordinator python scripts/seed_test_users.py   # optional
```

Open http://localhost:8000 (seeded logins: `user@example.com` / `password123`,
`admin@example.com` / `admin12345`).

| Service | Stands in for |
| --- | --- |
| `k3-server` | the AWS hub's k3s control plane (runs no Pods) |
| `k3-worker-1..3` | three lab PCs (nodes `local-pc-1..3`, labelled `idlecloud/role=worker`) |
| `minio` | S3 |
| `web` with `LOCAL_RENDER=true` | the page-rendering Lambda |
| `coordinator`, `verifier`, `postgres` | themselves |

- A task's copies must run on different nodes. A result from a PC that isn't
  trusted yet needs a second copy, and a disagreement adds a tie-breaker, so
  three workers are enough for normal runs (see `services/verifier/README.md`).
  Each worker reports this machine's full CPU and memory, so Kubernetes can
  place more work than the machine really has.
- Worker Pods reach the coordinator and MinIO at fixed IPs on the `cluster`
  network (`172.29.0.10` and `172.29.0.11`), because Pods can't resolve compose
  service names.
- The worker nodes have their own image store: `scripts/load-worker-images.sh`
  builds `ghcr.io/sitprojects/idlecloud-ocr:main` and the other worker images
  and loads them into each. A recreated worker volume needs
  it again.
- A Test workload job (New job page) is the quickest check: its copies
  report the same fixed result, so it should reach Completed in under a minute.
  Its image, `python:3.12-slim`, is pulled from Docker Hub on first use.
- Inspect the cluster with `docker compose exec k3-server kubectl get nodes`
  (or `get pods -n idlecloud-jobs`).
- `docker compose down` stops everything; `down -v` also deletes the database,
  MinIO data and the k3s state.
