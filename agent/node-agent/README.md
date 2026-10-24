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

Every number is in `config.py` and can be overridden with an environment variable.

## Run

Inside Ubuntu (WSL2). Requires Python 3.11+ (Ubuntu 24.04 ships 3.12):

```bash
cd agent/node-agent
pip install -r requirements.txt
NODE_AGENT_DEMO=1 python server.py
```

Then start the Windows sensor. Check the decision with
`curl localhost:9100/state`.

Without a cluster, add `NODE_AGENT_TAINT=0` and the agent only logs its
decisions.

## Owner-active taint

On BUSY the agent adds `idlecloud/owner-active=true:NoExecute` to its own node,
so Kubernetes evicts the task pods and sends no new ones. On IDLE it removes the
taint. It starts BUSY, so a node is tainted until the sensor proves it is idle.
A failed change is retried every 2 seconds, and the taint is re-checked every
60 seconds. Other taints on the node, such as the verifier's quarantine, are
kept.

The agent needs its own Kubernetes identity. The K3s join token is not enough,
and K3s does not let a node change its own taints. Once the worker shows in
`kubectl get nodes`, run this from a machine with admin kubectl:

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

The server listens on `127.0.0.1` only. Anything that can reach the port can say
the PC is idle, so do not expose it on the WireGuard network.

The server keeps idle connections open for 30 seconds (`NODE_AGENT_KEEP_ALIVE_S`).
Keep this above the sensor's 5-second interval. At uvicorn's default of 5 seconds,
about 1 sensor reading in 3 fails with "Server disconnected without sending a
response".
