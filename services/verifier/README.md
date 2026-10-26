# Verification and trust

Owner: member 4.

Compare replica result hashes by majority vote, handle disputed tasks, update
node trust scores and labels, and assemble verified page text into final job
output. Common normalization helpers belong in root `shared/`.

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
