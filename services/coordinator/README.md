# Coordinator

Owner: member 3.

Provide the shared internal API, split jobs into tasks and replicas, and schedule
Kubernetes Jobs. Issue presigned storage URLs, track progress and node
heartbeats, and recover interrupted work from checkpoints. Shared contracts and
SQL migrations belong in root `shared/`.

`burst.py` holds member 6's read-only `GET /admin/burst` (admins only). It
reads the burst cost views for the web admin page. See the autoscaler README,
section "Admin page".

## Jobs whose tasks arrive in batches

`POST /jobs` may send only the first tasks of a job, with `task_count` set to the
final number and each task's `index`. `POST /jobs/{id}/tasks` adds more later
(OCR pages still being rendered). Indices the job already has are skipped, so
retries are harmless; an ended job answers 409. The verifier only completes a
job once all `task_count` tasks exist and are verified.

`GET /capacity?cpu=1&mem=256` counts how many more copies of that size the usable
workers (Ready, not cordoned, no owner-active or quarantine taint) can take, from
their allocatable CPU and memory less what unfinished task Pods ask for. The web
app sizes OCR tasks from it. It needs `list` on nodes (`coordinator-rbac.yml`).

## GPU job types

A job type with `gpu` set (`pytorch-gpu`, migration 014) needs an NVIDIA GPU
node. `k8s.build_replica_job_manifest` gives its copies:

- the `nvidia` runtime class and `NVIDIA_VISIBLE_DEVICES=all`, so the copy sees
  the node's GPUs, and `IDLECLOUD_REQUIRE_CUDA=1`, so the runner refuses to
  start the command without CUDA (`worker/runner/README.md`);
- a node selector on `idlecloud/gpu=nvidia`, which `infra/join-worker.sh` sets
  when the worker's GPU and NVIDIA container toolkit work, and, if the type sets
  `min_gpu_vram_gib`, a hard rule on `idlecloud/gpu-vram-gib`;
- the Pod label `idlecloud/gpu-job=true` and a hard anti-affinity on it per
  node, so a node runs one GPU copy at a time. The copy gets all of the node's
  GPUs, so the unit is the node, not the card.

Without a free GPU node a copy waits in Pending; the time limit counts from
when a node starts it, so waiting costs nothing. `GET /capacity?gpu=true`
counts free GPU nodes the same way (`&gpu_vram=<GiB>` for a minimum). No
NVIDIA device plugin is needed: the labels and the runtime class are enough
for one GPU copy per node.

## Campus workers

`workers.py` lets admins add and remove campus PCs from the web admin pages,
under `/admin/workers` (admins only). The enrollment helper on the hub
(`infra/enroll`, reached over the socket `coordinator.yml` mounts, client in
`enroll.py`) adds the WireGuard peer and issues the join token and node-agent
credentials, and keeps them; nothing about workers is stored in the database.
Each worker's `state` and `last_heartbeat` come from the `nodes` table once its
node agent sends heartbeats. Every change and download is recorded as an event
(`worker_added`, `worker_setup_downloaded`, ...) with the admin's email, before
the helper acts or the zip is handed out: if the event can't be written,
nothing happens. A change that then fails adds `<event>_failed` with its status.

- `POST /admin/workers` `{"name"}` adds one; adding it again finishes any step
  that failed before.
- `GET /admin/workers/{name}/setup.zip`, before the PC joins, is the zip for
  `install-worker.ps1 -Bundle`. 409 once it has joined or its join token
  expired (`POST .../join-token` for a new one).
- After joining, `POST .../heartbeat-token` and then
  `GET .../agent-credentials.zip` give the zip for `-AgentCredentials`.
- `DELETE /admin/workers/{name}` drains and deletes the node and removes its
  access and peer.

The zips name the coordinator for the node agent's heartbeats: the Service's
ClusterIP, or `NODE_AGENT_COORDINATOR_URL`. With neither, `setup.zip` answers
503 rather than hand out a zip whose node agent could never send a heartbeat.
Helper errors pass through as 400, 404 or 409; a failed command, or an answer
not in the expected shape, is 502, and a helper that can't be reached 503.


## Copies of a task run on different PCs

Voting only means something if the copies ran on different machines. Kubernetes'
pod anti-affinity keeps copies apart only while they run, so a copy started after
a sibling finished could reuse its node. The scheduler therefore launches a
task's copies one after another (a copy waits while an earlier sibling has no
node yet) and adds a hard rule keeping each new copy off every node its siblings
used, finished or not (`crud.sibling_nodes`, `k8s.build_replica_job_manifest`).

With fewer usable workers than copies, or after a copy's PC was lost, a copy
can have no allowed worker left. Housekeeping waits `PLACEMENT_TIMEOUT_S`
(default 300 s, long enough for a PC to come back or a burst worker to join),
then fails it with a `replica_unplaceable` event. The copy still counts towards
the vote total, so the task passes only if two different PCs agree; otherwise
it is disputed or fails, and the job ends instead of hanging. The rule is never
relaxed to reuse a PC, since one PC would then get two votes.

## Long jobs: retries, checkpoints and maximum age

A GPU training job can run for hours on PCs whose owners come and go, so a copy
is not failed for being evicted (migration 016):

- A replica counts `failures`: a lost node or Pod, a launch error, or a GPU
  copy whose runner found no usable GPU (exit 69). The copy fails at
  `RETRY_ATTEMPTS` (3). An owner eviction is not a failure, and neither is a
  Pod the node agent stopped locally (exit 143, `pod_watcher.decide`).
- A GPU copy with exit 69 is moved instead of failed: its node goes into
  `replicas.bad_nodes`, which the scheduler avoids along with the siblings'
  nodes. On the last retry the exit stands as the copy's result, so the log
  reaches the user.
- Evictions still end in two ways: a copy fails at `MAX_ATTEMPTS` (50) attempts,
  and housekeeping fails a job older than its type's `max_age_hours` (24 for
  `pytorch-gpu`, none for the others) with a `job_expired` event.
- Every attempt has its own S3 keys, `jobs/{job}/replicas/{replica}/attempts/{n}/`
  for `checkpoint.tar.gz`, `output.tar.gz` and `result.json`, so an old
  attempt's late upload can't replace a newer one's. The web app and the
  verifier read the old key (straight under the replica) for older jobs.
- `POST /internal/replicas/{id}/checkpoint` records each checkpoint the runner
  publishes: attempt, SHA-256, size and the training step from
  `idlecloud.json`. The next attempt's manifest lists that archive first, the
  job page shows "saved 4 min ago, step 1200", and `replica_evicted` events
  carry the last save's time and step so lost work can be measured.
- `checkpoint_max_bytes` in the manifest is the job type's `checkpoint_max_mib`
  (100, or 500 for `pytorch-gpu`).

## Check copies: scoring trained models

A check copy (`replicas.check_of`, migration 017) scores the model of a
finished training copy. The verifier creates it (`services/verifier/README.md`);
the coordinator:

- launches it with `k8s.build_check_job_manifest`: the job type's
  `check_image`, 1 core, 1 GiB, 10 minutes, only on nodes labelled
  `idlecloud/checker=true` (`infra/k8s/deploy.sh` labels the hub), with none of
  the GPU or sibling rules;
- gives its manifest a `check` section: presigned GETs for the training
  attempt's `result.json` and `output.tar.gz`, the hash the training copy
  reported for `result.json`, and the task's `eval_file` (stored by the web app
  at `jobs/{job}/eval/eval.npz`, which the training copy is never given);
- takes its outcome on `POST /internal/replicas/{id}/check` (`CheckIn`): the
  status, top-1 accuracy, sample count, model and evaluation file digests,
  evaluator version, or the reason it wasn't scored. A check copy can't use
  `/result`, and a training copy can't use `/check`;
- never fails it for waiting on a checker node (`fail_unplaced`), and never
  expires a job whose only unfinished task is SCORING.

`jobs.min_score` (0 to 1, from the web form's target accuracy) is only shown on
the job page as met or not met.
