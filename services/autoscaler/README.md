# Cloud burst autoscaler

Owner: member 6.

Monitor queued work and available campus capacity, launch bounded EC2 burst
capacity, and retire unused workers. Manage burst slots and cost controls, and
coordinate Spot interruption handling with the existing eviction and checkpoint
flow. Infrastructure templates belong in root `infra/`.

The EC2 launch template is the one exception. `burst/launch_template.py` creates
it with boto3, because the autoscaler reuses the same settings when it launches
workers. Shared network and cluster setup, such as the WireGuard hub and peer
configs, stays in member 1's `infra/`.

A burst worker is an EC2 `t3.small` that joins the K3s cluster through WireGuard,
like a campus PC. It gets the labels `idlecloud/role=worker` and
`idlecloud/pool=cloud`, so the coordinator's Jobs run on it with no changes.

Step 2 (now) builds the launch template and a manual launch command. Step 3 adds
the controller that calls the same functions and claims rows in `burst_slots`.

| File | What it does |
| --- | --- |
| `burst/config.py` | Instance type, slots 200 to 204, secret names, lifetime limit |
| `burst/user_data.sh.tmpl` | Boot script: WireGuard, wait for the hub, join K3s |
| `burst/user_data.py` | Fills the boot script for one slot and checks the inputs |
| `burst/slot_store.py` | Reads the slot's `wg0.conf` and the K3s token |
| `burst/launch_template.py` | Creates the launch template and a security group with no inbound rules |
| `burst/launch.py` | `up`, `down` and `list` commands |

## Needed from member 1

1. Five WireGuard peers on the hub for `10.10.0.200` to `10.10.0.204`, each with
   `PersistentKeepalive = 25` and no `DNS` line.
2. A Secret `burst-wg-slots` in `idlecloud-system` with keys `slot-200` to
   `slot-204`. Each value is the full `wg0.conf` for that peer.
3. A Secret `k3s-agent-token` in `idlecloud-system` with key `token`.
4. The K3s server version, for `K3S_VERSION`.

## Security choices

Burst nodes have no IAM role, because workers never hold AWS keys. IMDSv2 is
required with a hop limit of 1, so pods cannot reach the metadata service. The
security group has no inbound rules, since all cluster traffic comes through the
WireGuard tunnel the node opens itself. Every instance terminates itself after
120 minutes, even if the autoscaler is down.

## Launching a burst worker

Run from the repo root. AWS keys go in `runtime/aws-credentials`, the same file
`serverless/` uses. Copy `.env.aws.example` to `.env.aws` and fill in
`K3S_VERSION` first.

```sh
python -m services.autoscaler.burst.launch_template
python -m services.autoscaler.burst.launch up --slot 200
python -m services.autoscaler.burst.launch list
python -m services.autoscaler.burst.launch down --slot 200
```

`launch_template` only needs to run once, and again when the settings change.
A real launch needs member 1's Secrets and hub (see above).
