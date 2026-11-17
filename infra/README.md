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

A worker with a discrete NVIDIA or AMD GPU takes GPU job types
(`pytorch-gpu`, migrations 014 and 015) as well as ordinary ones. Before
running `join-worker.sh` on it, install the vendor's driver and container
toolkit:

- **NVIDIA:**
  1. Install a driver that supports CUDA 13 (580 or later) and check that
     `nvidia-smi` lists the GPU. On EC2, an AWS Deep Learning Base GPU AMI
     (Ubuntu) comes with one.
  2. Install `nvidia-container-toolkit`.
- **AMD:**
  1. Check that the `amdgpu` driver is loaded and `/dev/kfd` exists. Ubuntu's
     kernel has it, and ROCm itself isn't needed on the PC.
  2. Install `amd-container-toolkit`, which provides `amd-container-runtime`.

`join-worker.sh` then:

- finds the GPU. It checks `nvidia-smi` first, then the `amdgpu` driver's
  KFD topology. Integrated graphics (AMD APUs such as the Radeon 780M) are
  skipped, so a PC with only an APU joins as a CPU worker, and one with an
  APU and a discrete NVIDIA card joins as an NVIDIA worker;
- labels the node `idlecloud/gpu=<nvidia|amd>` and
  `idlecloud/gpu-vram-gib=<GiB>` (the smallest GPU's memory);
- writes `/var/lib/rancher/k3s/agent/etc/containerd/config-v3.toml.tmpl`,
  K3s's own containerd config plus the `idlecloud-gpu` runtime handler, which
  points at the vendor's runtime. The coordinator runs every GPU copy in that
  runtime class (`k8s/base/gpu-runtimeclass.yml`);
- on AMD nodes:
  - writes `/etc/rancher/k3s/registries.yaml`, which makes K3s pull
    `idlecloud-pytorch-rocm` wherever a copy names `idlecloud-pytorch-gpu`,
    with the same tag. K3s applies rewrites only to a mirror endpoint
    different from the registry's default, so the file lists ghcr.io as
    `https://ghcr.io:443`;
  - starts the agent with `--disable-default-registry-endpoint`, so a failed
    ROCm pull can't fall back to the CUDA image;
  - adds a udev rule that lets every user open `/dev/kfd` and the AMD render
    nodes. Task Pods run as uid 65534 and receive these devices with their
    host owner and mode (`root:render 0660` on Ubuntu);
- pulls the GPU image (several GB) through the CRI, so the first job doesn't
  wait for it and an AMD node gets the ROCm build.

Without the toolkit, the script joins without the labels and says so. It
won't overwrite a containerd template or `registries.yaml` it didn't write.
Two settings change what it picks:

- `IDLECLOUD_GPU_VENDOR=nvidia|amd|none` picks the vendor on a PC with both,
  or joins without GPU jobs.
- `IDLECLOUD_ALLOW_INTEGRATED_GPU=1` accepts an APU, for testing only. Its
  shared memory then counts as its VRAM.

Pass either through sudo, e.g. `sudo IDLECLOUD_GPU_VENDOR=amd bash
infra/join-worker.sh ...`. Run the script again after adding, removing or
changing a GPU, or its toolkit. Workers that joined before this setup only
have K3s's own `nvidia` handler, so they must run the script again before
they can start GPU copies.

**Windows workers (WSL2):** `agent/install-worker.ps1` runs this script
inside WSL, where the GPU is the Windows driver's. It has no `/dev/nvidia*`
or `/dev/kfd`, only `/dev/dxg` and the driver's libraries in `/usr/lib/wsl`.
So under WSL, the script does this instead:

- **NVIDIA:** finds the GPU through `/usr/lib/wsl/lib/nvidia-smi`.
- **AMD:** takes the GPU from `IDLECLOUD_WSL_GPU=amd` and
  `IDLECLOUD_WSL_GPU_VRAM_GIB`, which `install-worker.ps1` sets after
  checking the Windows display adapters. APUs are skipped by PCI device ID.
- **Runtime:** points `idlecloud-gpu` at
  `/usr/local/lib/idlecloud/wsl-gpu-runtime`, for both vendors. That's a
  wrapper around runc. When a container is created, it adds `/dev/dxg` (mode
  0666), mounts `/usr/lib/wsl` read-only, and puts `/usr/lib/wsl/lib` first in
  `LD_LIBRARY_PATH`. Docker Desktop and the NVIDIA toolkit do the same under
  WSL. The CUDA build of PyTorch then finds the driver's `libcuda`. The ROCm
  image carries AMD's `librocdxg`, which ROCm uses in place of `/dev/kfd`.
  No vendor container toolkit is needed, and `jq` is installed for the
  wrapper.
- **AMD image rewrite:** same as on Linux. There's no udev rule.

ROCm under WSL only works on the Radeon cards AMD lists for it, mostly RX 7000
and 9000. A copy on an unsupported card ends with exit code 69 and moves to
another node.

Checks on a GPU worker:

```sh
kubectl get nodes -L idlecloud/gpu,idlecloud/gpu-vram-gib  # on the hub
sudo grep -A3 idlecloud-gpu /var/lib/rancher/k3s/agent/etc/containerd/config.toml
sudo k3s crictl images | grep idlecloud-pytorch       # AMD: the ROCm build under the -gpu name
```

The coordinator runs at most one GPU copy per node, so a node counts as one
GPU slot however many GPUs it has (`GET /capacity?gpu=true`). A GPU copy with
no free GPU node waits in Pending; it doesn't fail. A copy that lands where
PyTorch sees no GPU ends at once with exit code 69 and a log line saying so
(`worker/runner/README.md`).

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
