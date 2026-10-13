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

Step 2 builds the launch template and a manual launch command. Step 3 adds the
controller in `controller/`, which calls the same functions on its own.

| File | What it does |
| --- | --- |
| `burst/config.py` | Instance type, slots 200 to 204, secret names, lifetime limit |
| `burst/user_data.sh.tmpl` | Boot script: WireGuard, wait for the hub, join K3s |
| `burst/user_data.py` | Fills the boot script for one slot and checks the inputs |
| `burst/slot_store.py` | Reads the slot's `wg0.conf` and the K3s token |
| `burst/launch_template.py` | Creates the launch template and a security group with no inbound rules |
| `burst/launch.py` | `up`, `down` and `list` commands |
| `controller/settings.py` | Machine sizes and prices, budget, AWS quotas, timings |
| `controller/decide.py` | The scaling rules and bin packing, as pure functions |
| `controller/cluster.py` | Reads task pods and nodes, cordons burst nodes |
| `controller/slots.py` | Claims and frees rows in the `burst_slots` table |
| `controller/__main__.py` | The loop: observe, decide, act, every 30 seconds |
| `Dockerfile`, `k8s/autoscaler.yml` | Image and Deployment with its permissions |

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

Known limit: the slot's WireGuard private key and the K3s token travel in EC2
user data. Anyone with `ec2:DescribeInstanceAttribute` in the AWS account can
read them. That is fine for a lab account the group controls. Fetching them at
boot instead would need an IAM role, which burst workers deliberately do not have.

## Launching a burst worker

Run from the repo root. AWS keys go in `runtime/aws-credentials`, the same file
`serverless/` uses. Copy `.env.aws.example` to `.env.aws` and fill in
`K3S_VERSION` first.

```sh
python -m services.autoscaler.burst.launch_template
python -m services.autoscaler.burst.launch up --slot 200 [--type t3.micro]
python -m services.autoscaler.burst.launch list
python -m services.autoscaler.burst.launch down --slot 200
```

`launch_template` only needs to run once, and again when the settings change.
A real launch needs member 1's Secrets and hub (see above).

## How the autoscaler decides (Step 3)

Every 30 seconds the loop takes a snapshot of the task pods in `idlecloud-jobs`,
the nodes, the `burst_slots` table, the burst instances in EC2 and the account's
total EC2 use. The rules in `controller/decide.py` turn that snapshot into actions.
How many workers run, and which size, follows the workload. Nothing is fixed.

**When to burst.** A pod the scheduler marks Unschedulable means no campus PC
can take it right now. One such pod waits 2 minutes, in case a campus PC frees
up. A bigger backlog will not be absorbed by campus PCs, so the wait shrinks
with it (120 s divided by the number of stuck pods, at least 30 s).

**How many, and which size.** The stuck pods are packed onto machines with
first-fit decreasing bin packing, using each pod's CPU and memory requests.
Two replicas of one task never share a machine, matching the coordinator's
anti-affinity. Pods go onto workers that are still booting first, so the same
pods never cause two launches. For 90 seconds after a worker turns Ready, its
free room still counts, because the scheduler can take a few tries to place
the waiting pods. After that, the scheduler's verdict is trusted again. Each new machine is the cheapest size that fits,
from `t3.micro` up to `t3.large` (the largest AWS Academy allows). All the
machines the plan needs launch in the same tick and boot in parallel.

**Limits that are worked out, not fixed.**

| Limit | Where it comes from |
| --- | --- |
| Hourly budget | `BURST_BUDGET_USD_PER_HOUR`. Default: 3 of the cheapest machine (about US$0.04/h), the least that still runs one 3-replica task fully in the cloud |
| AWS quota | Every running instance in the region is counted against AWS Academy's 9 instances and 32 vCPUs |
| WireGuard slots | Free rows in `burst_slots` (5 today) |

If the budget cannot cover a pod, only that pod waits. The plan is rebuilt
without it, so smaller pods still get machines. Every pod that has to wait is
logged with the reason.

**Scale down.** A burst node with no task pods for 5 minutes is cordoned,
terminated and its slot freed. When a campus PC has free CPU and memory for
at least the smallest pod on a burst node, and nothing is waiting, one burst
node per tick is cordoned so it takes no new pods. Its
running pods finish, then it is retired. If pods get stuck again first, the
draining nodes are reopened instead of launching new ones.

**Repairs.** A slot whose instance has ended on its own (lifetime limit or a
failed boot) is freed. An instance running without a slot row is terminated.
A worker that has not joined after 10 minutes is retired.

**Two copies never clash.** Each tick runs in one database transaction and first
takes a Postgres advisory lock, so only one copy acts at a time. A slot is
claimed with `SELECT ... FOR UPDATE SKIP LOCKED`. If the AWS launch fails, the
transaction rolls back and the slot stays free. The "empty since" time and the
draining flag are node annotations, so a restarted autoscaler carries on.

**Known limits.** T3 machines run on CPU credits. A long, CPU-heavy job slows
to the baseline speed once its credits run out. Booting takes about 2 to 3
minutes, because WireGuard and K3s are installed at boot.

## Running the autoscaler

In the dev container, with `DATABASE_URL` and the dev kubeconfig set:

```sh
AUTOSCALER_DRY_RUN=1 python -m services.autoscaler.controller
```

Dry run reads the cluster and the database and logs what it would do, without
touching AWS, nodes or slots. Drop `AUTOSCALER_DRY_RUN=1` to act for real. It
creates the `burst_slots` table itself if the coordinator has not run yet.
Timings, sizes and the budget can be changed with the variables in
`controller/settings.py`, for example `BURST_BUDGET_USD_PER_HOUR=0.30`.

In the cluster, build the image from the repo root and apply the manifest:

```sh
docker build -f services/autoscaler/Dockerfile -t idlecloud/autoscaler:0.1.0 .
kubectl apply -f services/autoscaler/k8s/autoscaler.yml
```

The manifest lists the Secrets it needs. It runs on the control-plane node and
uses that instance's AWS role, so no AWS keys are stored in the cluster.

## Spot reclaim handling (Step 4)

AWS gives a Spot machine a two-minute warning before taking it back. The
**Spot watcher** (`spotwatch/`) is a small pod on every burst node. Every 5
seconds it asks the instance metadata service (IMDSv2) for a warning. On one, it
adds the owner-active taint `idlecloud/owner-active=true:NoExecute` to its node,
the same taint member 2's agent uses when a PC's owner returns. Kubernetes then
evicts the task pods, and member 3's checkpoint and requeue logic resumes them
elsewhere. It also writes `idlecloud/reclaim-at` (the deadline) and
`idlecloud/reclaim-source` (`spot` or `injected`) on the node, and logs one JSON
line with the timings.

It reads two sources and handles both the same way:

- `spot/instance-action`, the real AWS Spot notice
- the EC2 tag `reclaim-at`, an injected warning for testing. AWS Academy allows
  no Spot instances, so this is how the same path is exercised there.

Every 10 seconds it also sends the coordinator a heartbeat for its node
(`IDLE`, or `BUSY` once a warning is seen), since burst nodes have no campus
node agent.

A reclaim is final. Once the node is marked, the watcher stops checking and
never touches the taint again. Removing the taint by hand does not cancel the
reclaim, because the machine still goes at the deadline.

Injected warnings only work on workers launched from the current launch
template version, which makes instance tags readable through metadata. Updating
the template does not change workers that are already running. The watcher logs
at start whether tags are readable.

It uses only the Python standard library and runs from a ConfigMap on a small
public Python image, so new burst nodes pull no project image.

**Security.** It reads metadata through the node's network (hop limit 1 keeps
task pods away from it). RBAC lets it get and patch only the burst nodes
`burst-200` to `burst-204`. A ValidatingAdmissionPolicy narrows that to the node
the pod runs on, using the node name the API server signs into the pod's
token (Kubernetes 1.32+). On that node it may only add the exact taint
`idlecloud/owner-active=true:NoExecute` and set the three `idlecloud/reclaim-*`
annotations. Everything else on the node must stay exactly as it was.

Apply with kustomize, which builds the ConfigMap from `watcher.py`:

```sh
kubectl apply -k services/autoscaler/spotwatch
```
