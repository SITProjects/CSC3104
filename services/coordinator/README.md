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

## Copies of a task run on different PCs

Voting only means something if the copies ran on different machines. Kubernetes'
pod anti-affinity keeps copies apart only while they run, so a copy started after
a sibling finished could reuse its node. The scheduler therefore launches a
task's copies one after another (a copy waits while an earlier sibling has no
node yet) and adds a hard rule keeping each new copy off every node its siblings
used, finished or not (`crud.sibling_nodes`, `k8s.build_replica_job_manifest`).
A task with more copies than usable workers waits until enough workers join.
