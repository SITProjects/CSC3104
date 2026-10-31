# Verification and trust

Owner: member 4.

Compare replica result hashes by majority vote, handle disputed tasks, update
node trust scores and labels, and assemble verified page text into final job
output. Common normalization helpers belong in root `shared/`.

## Spot checks instead of three copies

Running every task three times costs three times the compute. Extra copies
are only needed where a PC isn't trusted yet, so trusted PCs are checked at
random instead (`spot_check.py`). The idea comes from BOINC's adaptive
replication, in a simpler form with our own trust rules.

- Every task starts with one copy (the web app's `REPLICAS_PER_TASK`, default 1).
- When it finishes, a result from a trusted PC (score 0.8 or more) is accepted.
  A random share of them, `SPOT_CHECK_RATE` (default 0.1), gets a second copy
  on another PC first. A result from any other PC always gets the second copy.
  A result accepted on its own earns its PC no trust, because nothing was
  compared.
- Two copies must agree. If they don't, one tie-breaker copy runs, so three
  PCs can settle one bad result. Later disputes add two copies, up to five.
- A PC that loses a vote drops to 0.5 at most, so even a PC at 1.0 loses its
  trusted status at once and is fully checked again until it earns it back.
- In the same transaction, its results that were accepted on trust in jobs
  still running are reopened with one more copy (`task_rechecked`). A job that
  already finished has published its output, so it is not reopened.

With most PCs trusted, a task needs about 1 + `SPOT_CHECK_RATE` copies (1.1)
instead of 3, before disputes, failures and re-checks. That is still more than
running once. The trade-off is risk: a trusted PC's wrong result is accepted
unchecked 90% of the time, and finished jobs are not repaired. A PC that fakes
a share f of its results is caught at least once within n tasks with
probability 1 - (1 - rate × f)^n. Results from PCs that aren't trusted yet
also take longer, because the second copy runs after the first.

Set `REPLICAS_PER_TASK=3` on the web app to start with three copies again, for
example to compare compute use. The stricter trust penalty stays in both cases.
Events: `task_spot_check` (second copy for a single result) and
`task_rechecked` (reopened result).

## Job types without voting

A job type with `verify = false` in `job_types` (migration 010, the `python`
type) is not voted on. Its single run is accepted if it succeeded, and fails
the task if it didn't, without more copies or trust changes
(`controller.accept_single_run`). Evicted copies are still run again by the
coordinator first. The coordinator refuses such a job if it asks for more than
one copy per task.

## Ending jobs

- OCR jobs are completed by `output.py` once every task is verified and the
  text is assembled into `jobs/{id}/output/result.txt`.
- `finish.py` ends everything else. A job with a task that failed for good
  (no majority at five copies, or unusable data) becomes FAILED, and its
  unfinished copies are cancelled so the coordinator removes their Pods. Jobs
  of other types, such as the test workload, complete once every task is
  verified. Without this, those jobs stayed RUNNING and counted towards their
  owner's limit of unfinished jobs.

## Deploy

`infra/k8s/base/verifier.yml` runs one verifier in `idlecloud-system` on the AWS
control plane (`idlecloud/role=control`), built from `services/verifier/Dockerfile`
as `idlecloud/verifier`. `verifier-rbac.yml` gives its service account only what
trust sync needs: get and patch nodes. The AWS overlay (`verifier-s3.yml`) switches
it to real S3 through the instance role. `infra/k8s/deploy.sh` builds the image and
rolls it out with the coordinator and web app. Locally, `docker-compose.yml` runs
the same image.
