# Linux node agent

Owner: member 2.

Receive Windows sensor readings, decide whether the worker is idle or busy,
apply or remove the owner-active Kubernetes taint, and send coordinator
heartbeats. Run inside the worker's Linux environment.

## Rules

- Input in the last 10 seconds: BUSY right away.
- IDLE only after 5 minutes with no input (30 seconds with `NODE_AGENT_DEMO=1`)
  and host CPU under 20% for 3 readings in a row.
- No sensor reading for 30 seconds: BUSY.
- High CPU never turns an IDLE node BUSY, because our own task pods raise the
  host's CPU too.
- Owner schedule: never lent from 08:00 to 09:00 local time. The node goes BUSY
  at 08:00 even if the PC is idle, and can go IDLE again from 09:00.

Every number is in `config.py` and can be overridden with an environment variable.

## Owner schedule

Set the hours with `NODE_AGENT_BLOCKED`, as `HH:MM-HH:MM` windows separated by
commas. The default is `08:00-09:00`, and empty turns it off:

```bash
NODE_AGENT_BLOCKED="08:00-09:00,13:00-14:00" python server.py
NODE_AGENT_BLOCKED="" python server.py     # no blocked hours
```

A window that ends before it starts crosses midnight (`22:00-02:00`). A badly
written window stops the agent at start-up, so the PC is never lent by mistake.
The windows use WSL's local time. Check it with `date`, and if it is not the
PC's timezone, set `TZ`, for example `TZ=Asia/Singapore`.

## Run

On a worker, `install-worker.ps1` installs the agent as the systemd service
`idlecloud-node-agent` (see [Service](#service)). Run it by hand only to
develop or test it.

Inside Ubuntu (WSL2), from a checkout of the repo. Requires Python 3.11+
(Ubuntu 24.04 ships 3.12). Ubuntu 24.04 refuses `pip install` outside a virtual
environment, so make one first:

```bash
cd agent/node-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On a worker, stop the service first, since both use port 9100. With the
kubeconfig and token in `/etc/idlecloud/`:

```bash
sudo systemctl stop idlecloud-node-agent
COORDINATOR_URL=http://<cluster-ip>:8001 python3 server.py
sudo systemctl start idlecloud-node-agent   # when done
```

[Coordinator heartbeats](#coordinator-heartbeats) shows how to find the
ClusterIP. For a demo, add `NODE_AGENT_DEMO=1` for the 30-second idle wait.
Without a cluster, add `NODE_AGENT_TAINT=0` and the agent only logs its
decisions.

The sensor starts at login on a worker. Check the decision with
`curl localhost:9100/state`.

### Service

`install-service.sh` copies the agent to `/opt/idlecloud/node-agent/` with its
own venv, and installs `idlecloud-node-agent.service`. `install-worker.ps1`
runs it; to run it by hand, from this folder:

```bash
sudo bash install-service.sh <node-name> [http://<cluster-ip>:8001]
```

Run it again after changing the code, to copy it over and restart the agent.

- Starts at boot and is restarted 10 seconds after it stops. Without the
  kubeconfig it exits at start-up, so it keeps retrying until
  `make-kubeconfig.sh`'s files are copied over, then runs.
- Settings go in `/etc/idlecloud/node-agent.env`, one `NAME=value` per line
  (`NODE_NAME`, `COORDINATOR_URL`, `NODE_AGENT_DEMO=1`, ...). Re-running the
  script keeps them, except the ones passed to it. After a change:
  `sudo systemctl restart idlecloud-node-agent`.
- Logs: `journalctl -u idlecloud-node-agent -f`. The evaluation log is
  `/var/log/idlecloud/node-agent-events.jsonl`.
- On stop it taints the node before it exits. It is ordered after WireGuard
  and K3s, so at shutdown it stops while the API server can still be reached.

While the agent is not running, the node keeps its taint and gets no task pods,
and the tray shows "node agent not running". The exception is an agent that
dies without stopping cleanly (power cut, WSL killed) while IDLE: the node stays
untainted until the service starts again.

## Owner-active taint

On BUSY the agent adds `idlecloud/owner-active=true:NoExecute` to its own node,
so Kubernetes evicts the task pods and sends no new ones. On IDLE it removes the
taint. It starts BUSY, so a node is tainted until the sensor proves it is idle.
A failed change is retried every 2 seconds, and the taint is re-checked every
60 seconds. Other taints on the node, such as the verifier's quarantine, are
kept.

The agent needs its own Kubernetes identity. The K3s join token is not enough,
and K3s does not let a node change its own taints. Workers added from the admin
pages get it in their setup zip (made by the enrollment helper, `infra/enroll`).
Otherwise, once the worker shows in `kubectl get nodes`, run this from a
machine with admin kubectl:

```bash
agent/node-agent/make-kubeconfig.sh <node-name>
```

It creates a ServiceAccount whose ClusterRole can only `get` and `patch` that
one node (`resourceNames`), and writes `node-agent-<node-name>.kubeconfig`.
It also applies `node-agent-policy.yml`, an admission policy that lets the agent
add or remove only the owner-active taint. RBAC cannot limit what a patch
changes, and the token sits on the lab PC, so without the policy its owner could
remove the verifier's quarantine taint or set `idlecloud/trust=high`.
Copy it to the worker:

```bash
sudo install -D -m 600 node-agent-<node-name>.kubeconfig /etc/idlecloud/node-agent.kubeconfig
```

If the Kubernetes node name is not the lowercased hostname, set `NODE_NAME`.

## Coordinator heartbeats

Every 10 seconds the agent posts its state and the Windows host's CPU load to
`/internal/nodes/<node>/heartbeat`. After 30 seconds without one, the
coordinator marks the node OFFLINE and requeues its replicas.

`make-kubeconfig.sh` also writes `coordinator-<node-name>.token`. This token is
for the shared `node-agent-heartbeat` ServiceAccount, which has no RBAC rules.
It is bound to this Node, so the coordinator accepts heartbeats from it for this
node only. The API server does not accept it, because it was issued for the
coordinator's audience. It lasts a year (`HEARTBEAT_DURATION`). Run the script
again to renew it. Copy it to the worker:

```bash
sudo install -D -m 600 coordinator-<node-name>.token /etc/idlecloud/coordinator.token
```

The agent runs on the host, not in a pod, so cluster DNS does not work. Point it
at the coordinator Service's ClusterIP, which kube-proxy routes from the node:

```bash
kubectl -n idlecloud-system get svc coordinator   # on an admin machine
COORDINATOR_URL=http://<cluster-ip>:8001 python server.py
```

Without `COORDINATOR_URL` the agent sends no heartbeats, and the coordinator
sees the node as OFFLINE.

## Evaluation log

Every state change and every taint change is appended as one JSON line to
`node-agent-events.jsonl` next to `server.py` (the service writes to
`/var/log/idlecloud/` instead), wherever the agent is started
from (`NODE_AGENT_EVENT_LOG`; empty turns it off). The file is not in Git; the
eviction trials copy it into `eval/results/`.

```json
{"time": "2026-10-24T09:00:01.120+00:00", "node": "pc1", "event": "state", "old": "IDLE", "new": "BUSY", "reason": "input 2s ago", "idle_seconds": 2.0}
{"time": "2026-10-24T09:00:01.310+00:00", "node": "pc1", "event": "taint", "added": true}
```

The key press happened `idle_seconds` before the `state` line, so key press to
taint is that gap plus the time to the `taint` line.

## Graceful shutdown

`infra/join-worker.sh` sets `shutdownGracePeriod: 30s` in
`/var/lib/rancher/k3s/agent/etc/kubelet.conf.d/50-graceful-shutdown.conf`. When
the PC is switched off, task pods get SIGTERM and 30 seconds to save a
checkpoint. It must be in this config file: `--kubelet-arg=shutdown-grace-period`
is not a kubelet flag and stops the kubelet starting. This needs systemd in WSL2
(`[boot] systemd=true`). Check it with `systemd-inhibit --list | grep kubelet`,
which should show a `delay` lock.

## Server

The server listens on `127.0.0.1` only. Anything that can reach the port can say
the PC is idle, so do not expose it on the WireGuard network.

The server keeps idle connections open for 30 seconds (`NODE_AGENT_KEEP_ALIVE_S`).
Keep this above the sensor's 5-second interval. At uvicorn's default of 5 seconds,
about 1 sensor reading in 3 fails with "Server disconnected without sending a
response".
