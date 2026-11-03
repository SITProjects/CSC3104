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
