# Worker management

Owner: member 2.

Turns a Windows PC into a worker that is lent out when idle and taken back
within seconds when its owner returns. K3s workers must run Linux, so the work
is split in two halves that talk over `localhost:9100`:

| Folder | Runs on | Job |
|---|---|---|
| [`windows-sensor/`](windows-sensor/README.md) | Windows host | Reads keyboard/mouse idle time and host CPU, shows a tray icon |
| [`node-agent/`](node-agent/README.md) | Ubuntu in WSL2 | Decides IDLE or BUSY, taints the node, sends heartbeats |

`install-worker.ps1` sets up a PC in one step. The eviction-speed evaluation is
in [`eval/eviction/`](../eval/eviction/README.md).

## Set up a worker

### Before you start

- Ubuntu 24.04 in WSL2: `wsl --install -d Ubuntu-24.04`, then open it once and
  create the user.
- `wg0.conf` and `agent-token` for this PC, from
  `/etc/wireguard/peers/<name>/` on the hub after member 1 runs
  `add-peer.sh worker <name>`. The token lasts 10 minutes, so run the installer
  soon after.
- The sensor .exe. It is not in Git, so build it once on any Windows machine
  (see the
  [sensor README](windows-sensor/README.md#build-the-exe-developers-only)) and
  copy it to each PC. The installer looks in `windows-sensor\dist\`, or pass
  its path with `-SensorExe`.

### 1. Run the installer (Windows)

In Windows PowerShell, from the repo, as the PC's normal user (not admin):

```powershell
powershell -ExecutionPolicy Bypass -File .\agent\install-worker.ps1 `
    -NodeName pc1 -WgConfig .\wg0.conf -TokenFile .\agent-token `
    -CoordinatorUrl http://<cluster-ip>:8001
```

If `wsl -l` names the distro something other than `Ubuntu-24.04` (often just
`Ubuntu`), add `-Distro <name>`.

`-CoordinatorUrl` is where the node agent sends heartbeats. Find the ClusterIP
with `kubectl -n idlecloud-system get svc coordinator`. Without it the agent
still taints the node, but sends no heartbeats.

It:

1. Turns on systemd in WSL2 (`[boot] systemd=true` in `/etc/wsl.conf`). K3s,
   WireGuard and kubelet graceful shutdown need it.
2. Copies the sensor to `%LOCALAPPDATA%\idlecloud\` and adds two Task Scheduler
   tasks under `\IdleCloud\`, both started at login:
   - `Sensor` runs the sensor .exe.
   - `WSL` keeps a hidden `wsl.exe` open. WSL stops its VM soon after the last
     `wsl.exe` exits, even with systemd services running, which would take the
     K3s agent down with it.
3. Installs the node agent in Ubuntu as the systemd service
   `idlecloud-node-agent`, so it starts at boot and is restarted if it stops.
   It waits for its credentials from step 2 below.
4. Runs `infra/join-worker.sh` inside Ubuntu to join the cluster over
   WireGuard, then starts both tasks.

Running it again skips the join if the K3s agent is already active and only
refreshes the sensor, tasks and node agent. Add `-Rejoin` to join again (needs
a new token).

Check on an admin machine that the node is `Ready`:

```bash
kubectl get nodes
```

### 2. Give the node agent its credentials (admin machine)

The node joins with the `idlecloud/owner-active` taint, so it gets no tasks
until the node agent runs and sees the PC is idle. The agent needs its own
Kubernetes identity. On a machine with admin kubectl:

```bash
agent/node-agent/make-kubeconfig.sh pc1
```

Copy the two files it writes to the worker as
`/etc/idlecloud/node-agent.kubeconfig` and `/etc/idlecloud/coordinator.token`.
See [the node-agent README](node-agent/README.md#owner-active-taint) for what
they allow and the exact commands.

The node agent service starts by itself within 10 seconds of the files being
in place, and at every boot after that.

### 3. Check it works

- `systemctl status idlecloud-node-agent` in Ubuntu shows `active (running)`.
- `curl localhost:9100/state` in Ubuntu shows a recent `last_reading`.
- The tray icon is grey while you use the PC, and turns green after the idle
  wait (5 minutes, or 30 seconds with `NODE_AGENT_DEMO=1`).
- `kubectl describe node pc1` shows the `idlecloud/owner-active` taint while
  the PC is BUSY, and not while it is IDLE.

### If something is wrong

In Ubuntu, `journalctl -u idlecloud-node-agent -n 30` and
`curl localhost:9100/state` show what the agent is doing.

- **`403 Forbidden` in the log:** `NODE_NAME` in `/etc/idlecloud/node-agent.env`
  is not the name given to `make-kubeconfig.sh`. The credentials only work for
  that one node. Fix it and `sudo systemctl restart idlecloud-node-agent`.
- **Stays BUSY with no input:** check `reason` and `cpu_percent` in
  `/state`. The PC is only lent while host CPU is under 20%, which a busy
  laptop may never reach. Add `NODE_AGENT_IDLE_CPU_PERCENT=40` to
  `node-agent.env` and restart.
- **No admin kubectl at hand:** the agent's own credentials can read its node:
  `sudo k3s kubectl --kubeconfig /etc/idlecloud/node-agent.kubeconfig get node <name> -o jsonpath='{.spec.taints}'`.
  `node.kubernetes.io/unreachable` taints there mean the K3s agent, not the
  node agent, has lost the server; check `systemctl status k3s-agent wg-quick@wg0`.

## If WSL2 does not work

`install-worker.ps1` only supports WSL2. The plan's fallback, Ubuntu in
VirtualBox, is not covered by these scripts.
