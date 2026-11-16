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

## GPU workers

A worker with an NVIDIA GPU takes GPU job types (`pytorch-gpu`, migration 014)
as well as ordinary ones. Before running `join-worker.sh` on it:

1. Install an NVIDIA driver that supports CUDA 13 (580 or later) and check that
   `nvidia-smi` lists the GPU. On EC2, an AWS Deep Learning Base GPU AMI
   (Ubuntu) comes with one.
2. Install the NVIDIA container toolkit (`nvidia-container-toolkit`). It must
   be there before the K3s agent starts, or K3s won't register the `nvidia`
   runtime. If the agent is already running, restart it with
   `sudo systemctl restart k3s-agent`.

`join-worker.sh` then labels the node `idlecloud/gpu=nvidia` and
`idlecloud/gpu-vram-gib=<GiB>`, and pulls the GPU worker image (several GB)
so the first job doesn't wait for it. Without the toolkit it joins without the
labels and says so. K3s already ships the `nvidia` RuntimeClass the
coordinator uses; check with `kubectl get runtimeclass nvidia`.

The coordinator runs at most one GPU copy per node, so a node counts as one
GPU slot however many GPUs it has (`GET /capacity?gpu=true`). A GPU copy with
no free GPU node waits in Pending; it doesn't fail. A copy that lands where
PyTorch sees no CUDA device ends at once with exit code 69 and a log line
saying so (`worker/runner/README.md`).

An EC2 GPU instance standing in for a lab PC joins like a laptop
(`add-peer.sh` with an address below .200), not in a burst slot: the
autoscaler ends empty burst workers, and an address below .200 gives it the
owner-active taint, so it only takes work while its node agent says idle.

## Task Pod network rules

`k8s/base/jobs-networkpolicy.yml` lets task Pods reach only cluster DNS, the
coordinator's API and the public internet (S3 and anything a user's script
downloads). It blocks the API server, Postgres, the WireGuard network, private
LAN addresses and the EC2 metadata address, and lets nothing connect in. It is
part of the AWS deployment only; the local stack leaves it out.

Policies only add allowed traffic, so test it on the real cluster after a
deploy. The first two checks must print a status code; every other one must
print "blocked" (curl exit code 28 is a timeout, anything else means the
connection got through):

```sh
kubectl -n idlecloud-jobs run netcheck --rm -it --restart=Never \
  --image=curlimages/curl:8.11.1 \
  --overrides='{"spec":{"nodeSelector":{"idlecloud/role":"worker"},"tolerations":[{"operator":"Exists"}]}}' \
  -- sh -c '
  curl -sS -m 5 -o /dev/null -w "coordinator %{http_code}\n" http://coordinator.idlecloud-system.svc.cluster.local:8001/healthz
  curl -sS -m 5 -o /dev/null -w "s3 %{http_code}\n" https://s3.amazonaws.com
  check() { curl -s -k -m 5 -o /dev/null "$2"; [ $? -eq 28 ] && echo "$1 blocked" || echo "$1 REACHABLE"; }
  check "API server" https://kubernetes.default.svc
  check "metadata" http://169.254.169.254/latest/meta-data/
  check "hub" https://10.10.0.1:6443
  check "Postgres" http://postgres.idlecloud-system.svc.cluster.local:5432'
```

Any line printing REACHABLE means the policy isn't enforced on that worker.
