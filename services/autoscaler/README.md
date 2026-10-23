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

We own burst workers, so each one starts fully trusted (plan step 5). This
prepares them to act as tie-breakers for disputed tasks. A worker joins with
`idlecloud/trust=high`. When the autoscaler records a new machine on a slot, it
also sets that node's `trust_score` to 1.0 in the same transaction. Node names
are reused per slot, so this stops a new machine inheriting an old one's score.
Only the score changes. The agree and disagree counters, state and heartbeat
stay as the verifier and coordinator left them. If this write fails, the launch
stays held and is recorded again next tick, with no new EC2 launch.

The coordinator schedules the extra replicas of DISPUTED tasks like any
other replica. Two things outside the autoscaler are still open:

- The autoscaler's database user needs INSERT and UPDATE on `nodes`.
- The coordinator's Jobs prefer `trust=high` for every replica, so ordinary
  replicas also prefer burst workers. They need a stronger preference for
  campus PCs (for example `idlecloud/pool NotIn [cloud]` at weight 100 and
  trust at weight 20), or burst workers stay busy and cost more.

Step 2 builds the launch template and a manual launch command. Step 3 adds the
controller in `controller/`, which calls the same functions on its own.

| File | What it does |
| --- | --- |
| `burst/config.py` | Instance type, slots 200 to 204, secret names, lifetime limit |
| `burst/user_data.sh.tmpl` | Boot script: WireGuard, graceful shutdown settings, join K3s, shutdown check |
| `burst/user_data.py` | Fills the boot script for one slot and checks the inputs |
| `burst/slot_store.py` | Reads the slot's `wg0.conf` and the K3s token |
| `burst/launch_template.py` | Creates the launch template and a security group with no inbound rules |
| `burst/launch.py` | `up`, `down` and `list` commands |
| `burst/market.py` | Spot or On-Demand for each launch (`BURST_MARKET`), and what a launch answer means |
| `controller/settings.py` | Machine sizes and prices, budget, AWS quotas, timings |
| `controller/model.py` | The records the loop fills and the rules read: pods, nodes, slots, instances, actions |
| `controller/decide.py` | The scaling rules, as pure functions |
| `controller/packing.py` | Bin packing: which new machines the waiting pods need |
| `controller/lifetime.py` | The 2 hour lifetime limit, worked out from the EC2 launch time |
| `controller/cluster.py` | Reads task pods and nodes, cordons burst nodes |
| `controller/slots.py` | Claims and frees rows in the `burst_slots` table |
| `controller/pending.py` | Holds a launch's slot until its instance is found and saved |
| `controller/usage.py` | Saves burst machines and the task pods that ran on them, for cost |
| `controller/__main__.py` | The loop: observe, decide, act, every 30 seconds |
| `Dockerfile`, `k8s/autoscaler.yml` | Image and Deployment with its permissions |

## Needed from member 1

1. Five WireGuard peers on the hub for `10.10.0.200` to `10.10.0.204`, each with
   `PersistentKeepalive = 25` and no `DNS` line.
2. A Secret `burst-wg-slots` in `idlecloud-system` with keys `slot-200` to
   `slot-204`. Each value is the full `wg0.conf` for that peer.
3. A Secret `k3s-agent-token` in `idlecloud-system` with key `token`.
4. The K3s server version, for `K3S_VERSION`.
5. `ec2:CreateTags` on the control plane's role (LabRole), for the lifetime
   warning. Without it the worker still ends on time, only without the warning.
6. The autoscaler's database user needs INSERT and UPDATE on `nodes`, and
   SELECT, INSERT and UPDATE on `burst_machines` and `burst_runs`. It has them
   already if it applies the migrations itself.

## Security choices

Burst nodes have no IAM role, because workers never hold AWS keys. IMDSv2 is
required with a hop limit of 1, so pods cannot reach the metadata service. The
security group has no inbound rules, since all cluster traffic comes through the
WireGuard tunnel the node opens itself. The autoscaler ends every worker at
120 minutes (see "Lifetime limit"), and each instance also terminates itself
after 120 minutes of uptime, even if the autoscaler is down.

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

The running cost comes from EC2, not only from our own records. Every live
burst instance counts by its instance id, including one not in `burst_slots`,
a second one on the same slot, and one being terminated, until a later check
shows it gone. A claim in `burst_slots` is matched to EC2 by instance id too.
A second instance on a slot keeps the slot blocked, is logged as a warning and
is terminated after 2 minutes. Each instance is priced by its real size from a
table of all T3 sizes. A worker we launched whose size is not visible yet is
priced as the dearest size we launch. A size outside the T3 table has no known
price, so nothing more launches while it runs.

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

To inject a warning on the worker in slot 200:

```sh
python -m services.autoscaler.burst.launch reclaim --slot 200
```

It tags the one running instance on that slot with a UTC deadline two minutes
ahead. It refuses if the slot has no running instance or more than one, if the
instance already has a warning, or if the instance cannot read its tags.
`launch list` shows the deadline.

Not changing an existing warning is best effort. The check and the tag write
are two separate EC2 calls, so two runs at once can still overwrite the
deadline. Only one person should run it at a time.

**What the autoscaler does with a warning.** It reads the deadline from the
EC2 tag and from the node annotation, and uses the earlier one. Each burst node
labels itself with its EC2 instance id (`idlecloud/instance-id`) when it joins.
A node annotation only counts when that label names the instance on the slot,
so a warning left on an old node can never end a new machine.

Until the deadline the worker is "reclaiming". It gives no room to waiting
pods, and it is never reopened, drained or retired early (only the lifetime
limit may cordon it, see "Lifetime limit"). It still counts
against the budget, because it still costs money. At the deadline the
autoscaler terminates that instance, removes its node and frees its slot. The
check runs every 30 seconds, so the machine can go up to 30 seconds late.

**Removing a node safely.** When the autoscaler retires, reclaims or kills an
orphan, it ends only that instance by its id, never every machine on the slot.
Then, and when it frees a slot whose instance is gone, it removes the node and
its K3s node password only if they belong to that instance. Before retiring, it
cordons the node only if the node is that instance's at that moment. Every node
change the autoscaler makes (cordon, drain, reopen, empty markers) is pinned to
the node's uid, so it never lands on a replacement node with the same name. It checks this at the
moment of removal, not from the start of the tick. A labelled node must name
the instance. An older node without the label is removed only when no other
instance holds or claims the slot. K3s can create a new worker's password
before that worker's node exists, so the password is deleted only if the same
owned node (same uid) is seen both before and after reading it. If there is no
node, the node changes, or it is already gone at delete time, the password is
kept. `launch` clears a leftover password by name before it starts a new worker
on the slot. Both deletes carry a uid precondition. This needs `get` on the
five node-password secrets (`k8s/autoscaler.yml`).

K3s itself also deletes a node's password by name when the node is deleted. If
a replacement's password ever appeared in that short gap, K3s would remove it,
and the worker creates it again the next time it authenticates.

Nothing ends a warned instance before its deadline. An orphan or a second
instance on a slot with a warning is kept until the deadline, then ended. When
the worker beside it is retired, the warned one keeps the slot and keeps
counting against the budget until it ends.

An injected warning ends the machine even if the watcher failed or the node is
NotReady, because the tag is on the instance itself. A real Spot warning
reaches the autoscaler only through the watcher's annotation. If the watcher
never published it, AWS still takes the machine, and the normal cleanup frees
its slot once EC2 shows it gone.

Injected warnings only work on workers launched from the current launch
template version, which makes instance tags readable through metadata. Updating
the template does not change workers that are already running. The watcher logs
at start whether tags are readable.

**Rolling this out.** The instance id label comes from the boot script, so only
workers launched after this change have it. A worker launched earlier from the
current template can read tags but has no label. On such a worker an injected
tag still ends the machine at the deadline, but the watcher's annotation is
ignored, and its node is removed only when no other instance holds the slot.
Before testing reclaim, end every running burst worker (`launch down --slot N`)
so the autoscaler launches labelled ones.

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

### Graceful shutdown on burst workers

A burst machine can power off for three reasons: AWS takes back a Spot
worker, the autoscaler terminates it, or it reaches its 2 hour lifetime. The
boot script turns on the kubelet's graceful node shutdown, so pods get time to
stop first (`burst/user_data.sh.tmpl`, values in `burst/config.py`):

- Task pods get up to 30 s, then critical pods get up to 10 s
  (`shutdownGracePeriod: 40s`, `shutdownGracePeriodCriticalPods: 10s`). These
  are upper limits. A pod can finish sooner, and its own
  `terminationGracePeriodSeconds` (30 s for task pods) can shorten it. They do
  not promise that a checkpoint is saved.
- The Spot watcher is `system-node-critical`, so it stops in the last phase,
  after task pods. This only orders shutdown. Its heartbeats say BUSY only
  after a reclaim notice, so a lifetime shutdown or a plain terminate can still
  report IDLE.
- The settings go in `/etc/idlecloud/kubelet.conf.d/50-graceful-shutdown.conf`,
  passed with `--kubelet-arg config-dir=...`. K3s copies it into its own
  kubelet drop-in folder. Checked with a real K3s v1.34.1 agent: the kubelet
  logged 30 s for priority 0 and 10 s for critical priority.
- The kubelet delays power-off with a logind delay lock. logind allows at most
  `InhibitDelayMaxSec`. Ubuntu's unattended-upgrades sets 30 s in
  `/usr/lib/systemd/logind.conf.d/unattended-upgrades-logind-maxdelay.conf`,
  which is read after the kubelet's own setting. With that cap the kubelet's
  shutdown manager does not start at all. The boot script writes a file with
  the same name in `/etc` (45 s), which replaces it, reloads logind and checks
  the live value before K3s starts.
- After the join, the boot script waits up to 90 s for logind's live limit and
  the kubelet's shutdown lock in delay mode. It logs
  `graceful shutdown check: PASS` or `FAIL` in `/var/log/idlecloud-burst.log`.
  A FAIL does not stop the worker. It only means pods are not given the extra
  time.

**Checking it on a real burst worker.** On the node:

```sh
grep "graceful shutdown check" /var/log/idlecloud-burst.log
busctl get-property org.freedesktop.login1 /org/freedesktop/login1 \
  org.freedesktop.login1.Manager InhibitDelayMaxUSec   # t 45000000
systemd-inhibit --list   # a kubelet line with WHAT shutdown, MODE delay
```

**Live tests.** Stop the autoscaler first, so it does not react
(`kubectl -n idlecloud-system scale deploy/idlecloud-autoscaler --replicas=0`),
and scale it back afterwards. Use a fresh worker for each test.

On the worker under test, run two test pods in `idlecloud-jobs`, pinned to it
with `nodeName`, both with `terminationGracePeriodSeconds: 60` (longer than
either phase, so the kubelet's phase limit is what ends them):

- an ordinary pod
- a pod with `priorityClassName: system-node-critical`

Each prints its Unix time every second, prints `TERM <time>` when SIGTERM
arrives, and then deliberately keeps running. Follow both logs from your own
machine and save them there (`kubectl logs -f <pod> > file`), so the
timestamps survive the worker.

Pass, for a shutdown through logind:

- the ordinary pod prints TERM, then keeps printing for about 30 s
- the critical pod prints TERM about 30 s after the ordinary pod's TERM, then
  keeps printing for about 10 s
- Fail: printing stops within a few seconds of TERM, or no TERM at all. A log
  stream that drops early is inconclusive, not a pass.

Run these separately, because they test different things:

1. **Power-off.** `sudo systemctl poweroff` on the worker. This is the logind
   path itself.
2. **Plain EC2 terminate.** `aws ec2 terminate-instances --instance-ids <id>`.
   AWS tries a graceful OS shutdown, which this test checks on our workers.
   Do not use `launch down` here: it also deletes the node at once, which
   changes what can be observed during shutdown.
3. **`launch down --slot N`**, as an end-to-end check of the command only.
4. **Injected reclaim** (`launch reclaim --slot N`, autoscaler running). This
   tests eviction at the warning: the taint makes the pods get SIGTERM at the
   warning, and each pod's own grace period applies (30 s for task pods, 60 s
   for the test pods), not logind. It does not prove the shutdown handling
   above.

### Starting the replacement early

A new worker takes 2 to 3 minutes to boot, longer than the 2-minute warning.
So the autoscaler does not wait for the evicted pods to come back as stuck
pods. The moment a worker is warned, it counts the replicas that worker was
running as demand and plans room for them at once (`controller/rescue.py`
and `decide._rescue_demand`):

- Every tick it remembers which replicas (by the `idlecloud/replica-id` pod
  label) each node ran, and which instance that node ran on, because the
  watcher's taint may evict them before the next tick. When an instance is
  warned, it keeps the replicas of that instance only, even if its node is
  already deleted. They are kept until 2 minutes after the deadline
  (`AUTOSCALER_RESCUE_KEEP_S`).
- A replica stops counting once it has a pod on a healthy node, a pod waiting
  for a node, or needs no more work: its pod succeeded, or the coordinator marks
  the replica DONE or FAILED, its task VERIFIED or FAILED, or its job COMPLETED,
  FAILED or CANCELLED. A new worker that reuses the old node name counts as
  healthy. A waiting pod is handled by the normal scale-up, but without the
  usual wait for a campus PC. Each replica counts once.
- Free room on campus PCs comes first, then healthy burst workers, then workers
  still booting, then new machines. Replicas of one task always go to different
  nodes. No burst worker is drained while rescued replicas still wait, so the
  room just planned for them is not taken away. The budget, quotas and free
  slots apply as usual, and the warned worker still counts against the budget
  until it is gone. With the minimum budget the replacement therefore waits
  until then.

This is only a speed-up, and it does not move any work by itself. Running an
evicted replica again is the coordinator's job (member 3). It requeues an
EVICTED or LOST replica as the next attempt with the same replica id, until the
retry limit marks it FAILED. The normal scale-up places the new pod, with or
without this step. If the
autoscaler restarts, the memory starts empty and only that normal path remains.
If a replica is never requeued, the early machine stays empty and is retired
after 5 minutes. The coordinator clears `replicas.node_name` on eviction, but
its `replica_evicted` and `replica_lost` events keep the node name. Those could
replace the in-memory record later.

### Spot or On-Demand

`BURST_MARKET` picks the market for each worker the autoscaler launches
(`burst/market.py`):

| Value | What happens |
| --- | --- |
| `on-demand` (default) | Never tries Spot |
| `spot` | Spot only. A refused launch fails and the next tick tries again |
| `auto` | Spot first. After a clear refusal the same launch runs once as On-Demand |

On-Demand stays the default until a live reclaim test shows that the
coordinator's requeue really resumes the evicted work. Until then, a real Spot
reclaim may lose the work that was running.

In `auto`, the chooser remembers why Spot was refused:

| AWS error on the Spot launch | Result |
| --- | --- |
| `AuthFailure.ServiceLinkedRoleCreationNotPermitted` | Spot needs a service-linked role these credentials cannot create. On-Demand for good |
| `UnauthorizedOperation` | The same request is dry-run as On-Demand. If that is allowed, Spot was the problem: On-Demand for good for this size. If not, the error is raised |
| `InsufficientInstanceCapacity`, `UnfulfillableCapacity` | No Spot capacity for this size. Spot is skipped for it for 30 minutes (`BURST_SPOT_COOLDOWN_S`) |
| `MaxSpotInstanceCountExceeded` | The Spot vCPU quota is full. All T3 sizes share it, so every size skips Spot for 30 minutes |
| Anything else | Nothing changes. The launch fails and the next tick tries again |

"Anything else" covers expired credentials, a missing launch template, bad
parameters and throttling, where On-Demand would fail the same way. It also
covers `SpotMaxPriceTooLow`, because AWS leaves that Spot request open, so it
could still start next to an On-Demand fallback.

The fallback is safe only because it follows these specific refusals, which
mean AWS started nothing. An error alone does not prove that. A network error,
a timeout, an AWS server error, an unknown code or `SpotMaxPriceTooLow` leaves
the outcome unknown. That never falls back. The launch is held instead (see
"Launches with an unknown outcome" below).

The memory is kept in the process only. After a restart, one Spot launch may be
refused again before the chooser switches. A Spot worker is budgeted at the
On-Demand price. No maximum Spot price is set, so AWS never charges more than
that. The budget is a safe upper bound for compute, not the real cost per job.

The manual command `launch up --spot` still asks for Spot only, with no
fallback.

### Launches with an unknown outcome

A launch request can start a machine even when its answer is lost. If the
slot were simply freed, the next launch could take it and start a second
machine with the same WireGuard identity. So every launch is held in memory
(`controller/pending.py`) from just before its first request until its
instance id is committed to `burst_slots`.

- A first launch request is sent with boto3's automatic retries turned off
  (`aws.ONE_ATTEMPT`). Otherwise a timed-out attempt that started a machine,
  followed by a refused retry, would look like a clean refusal. An error from
  a client that may have retried is never trusted as a refusal. Replays of a
  held launch use the normal client, which is safe because no error ever
  settles a replay, and boto3's own retries reuse the same token.
- Every request carries its own `ClientToken`, and the launch template is
  pinned to its id and version at that moment, not `$Default`. A later
  `launch_template.py` run cannot change what a replay starts. The On-Demand
  fallback after a refusal is a new request with a new token.
- While held, its slot is never claimed again, even within the same tick.
  The planner counts its price in the budget until the EC2 burst listing shows
  it, then as that live instance, never twice. It counts one instance and its
  vCPUs in the account quota until the account listing itself shows it (the
  two listings are separate reads and can disagree). Its room for pods follows
  its real state: a whole machine while booting, what is left once its own
  node is Ready, and nothing while draining or reclaimed, or when EC2 has not
  listed its known instance for 2 minutes. Its own instance's reclaim warning
  counts too, even when it is a second instance on the slot. Each check runs
  on its own. It is never killed as an orphan.
  It is outside the boot and orphan timeouts. Its instance still ends at the
  lifetime limit like any worker, and the slot stays held after that.
- Each tick tries to settle it. If EC2 lists exactly one burst instance on that
  slot with that token, it is adopted. Otherwise the identical request is sent
  again with the same token. AWS then returns the instance the first request
  started, or starts it now. Either way there is one machine.
- A later error never settles it, not even a refusal. A refusal only shows that
  the replay started nothing, not that the first request did not. So a held
  launch is never released automatically. After 10 minutes, and every 10
  minutes after that (`AUTOSCALER_PENDING_WARN_S`), the log warns with its
  token. EC2 not listing an instance with that token is no proof that none
  started, because listings lag, however long the search stays empty. A
  restart drops the hold. It is a manual decision that accepts that risk, and
  the duplicate checks then only catch a second machine once EC2 lists it.
- AWS success is also held until the database commit. If the commit fails, the
  next tick writes the same instance id again, with no new request. If the
  commit worked but its reply was lost, the row already holds that instance,
  and that counts as recorded. A row holding a different instance is never
  overwritten.
- A just-recorded worker also counts in the account quota until the account
  listing shows it.

Known limits. A held launch that can never succeed (for example, Spot capacity
that never returns) keeps its slot, budget and room until someone restarts the
autoscaler. The memory is in the process only. The autoscaler assumes one
running copy at a time. One replica, the `Recreate` strategy and the advisory
lock per tick make overlap unlikely, but do not prove it. After a restart, held
launches are forgotten. A machine one of them started is then handled as an
orphan once EC2 lists it, and its slot can be reused before that. Covering
restarts needs the token stored in `burst_slots`, a follow-up with member 3.
AWS does not document what a replay returns after the first request failed,
or how long a token stays valid. A live test should check both.

## Lifetime limit (Step 5a)

Cost control from the plan: no burst worker runs longer than 2 hours. The
autoscaler retires each worker in three stages, all worked out from the
instance's EC2 launch time (`controller/lifetime.py`). Nothing is stored, so a
restarted autoscaler keeps the same schedule.

| Time after launch | What happens |
| --- | --- |
| 110 min | The node is cordoned. Running pods carry on, nothing new starts. It is never reopened, even when pods are waiting, and its room is never counted for waiting or rescued pods. If it empties, it is retired straight away |
| 118 min | The instance gets the EC2 tag `reclaim-at` = launch time + 120 min, the same warning as `launch reclaim`. The Spot watcher then taints the node, writes its annotations and reports BUSY. Its replicas get replacement workers early, as for a Spot warning |
| 120 min | The reclaim rule terminates that exact instance, removes its own node and frees its slot |

Why it is safe:

- **Earliest deadline wins.** A Spot warning or an injected one that ends
  sooner is kept. A later tag is moved earlier, never later.
- **The end does not depend on the warning.** The 120 minute end comes from the
  launch time, so it still happens if the tag write fails, the watcher is
  not running or the node is gone.
- **Only the right machine.** The cordon only touches a node whose
  `idlecloud/instance-id` label names the aged instance, pinned to its uid.
  The end only targets that instance id. A young replacement on the same slot
  is never touched.
- **Every live burst instance is covered.** A second instance on a slot, an
  orphan and a launch still being recorded all end at their limit too.
- **Backstop.** The boot script's `shutdown -h +120` counts from boot, which is
  after the launch time, so the autoscaler normally acts first.

120 minutes is a target. A tick runs every 30 seconds plus the time it takes,
so the end can be up to one tick late, longer if AWS calls fail. Work evicted
at 118 minutes is requeued by the coordinator (member 3), like any evicted
replica. A live test has not measured this yet.

Settings: `BURST_MAX_LIFETIME_MIN` (minutes, default 120, also used by the boot
script) and `AUTOSCALER_RETIRE_CORDON_LEAD_S` (seconds, default 600). The
warning lead is the Spot warning's 120 s. The autoscaler refuses to start
unless 120 s <= cordon lead < lifetime, so a lifetime of 10 minutes or less
also needs a smaller cordon lead. The error names both settings.

**Live demo.** Run the autoscaler with `BURST_MAX_LIFETIME_MIN=15` and
`AUTOSCALER_RETIRE_CORDON_LEAD_S=300`, then start a worker with a long-running
task pod. Expect the cordon at 10 minutes, the `reclaim-at` tag and the
watcher's taint at 13 minutes, and termination at 15 minutes. Check with
`kubectl get node burst-200 -o yaml`, `launch list` and the autoscaler log.

## Estimated burst cost (Step 5c)

The admin page (`/admin/nodes` in the web app) shows burst workers and cost per
job. It reads them through the coordinator's admin-only `GET /admin/burst`,
which reads the three SQL views in `shared/migrations/004_burst_cost.sql`.
Every figure is an estimate.

| View | One row per | Columns of note |
| --- | --- | --- |
| `burst_machine_cost` | Burst machine | `running`, `total_usd`, `attributed_usd`, `unattributed_usd` |
| `burst_job_cost` | Job | `usd`, `runs`, `has_estimated_runs`, `has_unknown_price` |
| `burst_run_cost` | Task pod on a burst machine | `usd`, `estimated`, `attempt` |

**What is recorded.** When a launch is saved on a slot, the machine gets a row
in `burst_machines` in the same transaction. The row keeps the hourly rate the
budget uses (On-Demand price plus the EBS disk), so later price changes never
rewrite history. For Spot this is an upper bound, since AWS caps Spot at the
On-Demand price and Spot prices move over time. The launch time is corrected
from EC2 each tick. The end time is set when the slot is released, which is
when the autoscaler ends the machine or sees it gone.

Each tick, every task pod on a burst node is saved in `burst_runs` by its pod
uid, with its job, task, replica, attempt, CPU request and container start and
finish times. A requeued replica is a new pod, so each attempt keeps its own
history. Seeing a saved pod again only fills a missing finish time and moves
`last_seen_at`, by pod uid, so a finish still arrives after its node is gone or
reused. A new pod is only linked to a machine that was running when the pod
started, so an old pod is never charged to the new machine on its name.
If saving fails, the error is logged, the tick carries on and the same pods
are saved next tick.

**How cost is split.** A run costs its time on the machine, times the
machine's rate, times the share of the machine's allocatable CPU it requested.
This is cost allocated by requested CPU, not measured CPU use. A lone 1 CPU
run on a 2 CPU machine pays half the machine's cost for that time. A fully
packed machine is fully attributed. Boot, idle and unrequested CPU, runs with
no known job and any work the autoscaler never saw are `unattributed_usd`, so
`attributed_usd` always equals the jobs' costs on that machine. A run with no
finish time counts up to when it was last seen and is marked estimated. A
machine with no known rate shows NULL, not zero.

**Limits.** Only machines recorded on a slot are listed. Workers already
running when this was rolled out have no row. An orphan or a duplicate the
autoscaler ends is still in the budget but not in these views. The end time
comes from releasing the slot, so it is approximate and can be a tick late. A finished pod is deleted 10 minutes after it
ends, so if the autoscaler is down longer than that, the run is missing and
its cost shows as unattributed.

`unattributed_usd` can be negative. The CPU share uses the machine size from
settings, not the node's real allocatable CPU, and a run with no finish time
counts until it was last seen, which can be later than it really ended if its
pod status was stale. A negative value means the estimated records disagree,
not that idle time cost less than nothing. It is left unclamped, so attributed
plus unattributed still equals the total.

### Admin page (Step 5, final bullet)

How the pieces connect:

```
/admin/nodes page  ->  CoordinatorClient.burst_overview()  ->  GET /admin/burst  ->  004 views
(services/web)         (services/web/coordinator.py)          (services/coordinator/burst.py)
```

The web app never touches the database. It forwards the user's login token,
and the coordinator checks it with its usual admin check. No token or an
expired one gets 401. A normal user gets 403.

`GET /admin/burst` returns `observed_at`, `machine_count`, `job_count`,
`machines` and `jobs`. Money comes back as decimal strings, or null when the
price is unknown. The two lists page on their own with `machine_limit`,
`machine_offset`, `job_limit` and `job_offset`. Limits are 1 to 100, default 20.

- Workers: still running first, then the newest. Trust, state, CPU and the
  last heartbeat come from `nodes`, but only for the instance now on that slot.
  Node names are reused, so an old machine never shows its replacement's data.
  The node row also outlives a machine. Trust is reset when the new machine is
  recorded, but state, CPU and heartbeat show only after it sends a heartbeat
  of its own. Until then they are blank.
- Jobs: every job, newest first. A job with no burst runs shows 0, which means
  no burst run was seen, not proof that none ran. A job with some unpriced runs
  shows the known part and a warning. A job with no priced runs shows Unknown.
- "Running" means no end time is recorded yet. It is not a live EC2 check.

The coordinator's database user needs SELECT on the three views,
`burst_machines`, `jobs`, `burst_slots` and `nodes`. The web app needs no database access.

Dependency on member 5: the page reads the token from the `session` cookie.
The login page on main is still a placeholder, so it does not set that cookie
yet. Until it does, set a coordinator admin token as the cookie by hand to test.
