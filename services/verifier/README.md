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
