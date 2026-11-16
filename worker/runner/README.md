# Generic job runner

Owner: member 6.

`runner.py` runs a user's own command for a generic job type and reports back
like any worker. Each approved image copies it in and uses it as the entrypoint
(see `worker/python-job`, `worker/pytorch-job` and `worker/pytorch-gpu-job`). It uses only the Python
standard library, so any image with Python 3.12 or newer can include it.

## What it does

1. Gets the replica manifest from the coordinator. The task input holds the
   command and files: `{"command": ["python", "train.py"], "files": ["inputs/train.py"]}`.
2. Downloads the files into a new temporary folder and runs the command there,
   with `OUTPUT_DIR` (the `output` folder in it), `CHECKPOINT_DIR` (the
   `checkpoint` folder) and `HOME` set. The command does not get the replica
   token. A file named `output` or `checkpoint`, or one that can't be fetched,
   stops the run before the command starts, and the log says why.
3. Packs `output/` and `log.txt` (the command's output, first 5 MB) into
   `output.tar.gz` and uploads it. An output folder over 100 MB is left out and
   the log says so.
4. Uploads `result.json` (`exit_code` and the archive's SHA-256) and reports its
   hash with the command's exit code.

A job type with `verify = false` (migration 010) is decided by that exit code:
0 completes the job, anything else fails it, and the download still holds the
log.

## GPU job types

For a job type with `gpu` set (migration 014) the coordinator sets
`IDLECLOUD_REQUIRE_CUDA=1`. Before the command starts, the runner then runs a
short PyTorch check on a CUDA device and logs the device's name and memory. If
PyTorch sees no CUDA device, the command is not started: the log says why and
the copy reports exit code 69, so the task fails with that log instead of a
script quietly training on the CPU. The image must have PyTorch.

## Checkpoints: carrying on after the PC's owner returns

A program that saves its progress in `$CHECKPOINT_DIR` can carry on where it
stopped when its copy moves to another PC. `worker/python-job/examples/train_resumable.py`
shows the pattern: save after each step, and load the saved state at start.

- While the command runs, the runner uploads the checkpoint folder as
  `checkpoint.tar.gz` every 60 s (`CHECKPOINT_INTERVAL_SECONDS`), only when it
  has changed. It skips a snapshot if files change while being packed and
  retries on the next interval. Recovery uses the last successful upload;
  the interval is not a guaranteed maximum amount of lost work.
- When the owner returns (SIGTERM), the command gets SIGTERM and up to 15 s to
  stop, then is killed. The runner gives the final save the remaining time
  within a 25 s shutdown budget, reserving up to 5 s for one eviction report.
  An upload still in progress cannot hold up the report. The Pod has 30 s in
  all, so the last save and report remain best effort.
- The coordinator runs the copy again on another PC, up to the usual attempt
  limit (3). Each attempt uploads to its own
  `jobs/{job}/replicas/{replica}/attempts/{attempt}/checkpoint.tar.gz` key, so a
  lost copy's late upload cannot replace a newer copy's progress. The manifest
  lists earlier attempts newest first; the runner tries them in that order,
  skipping missing or unusable archives. If none is usable, it starts fresh
  and the log says so.
- A program without checkpoints just starts again from the beginning.

Write checkpoint files whole, for example to a new file and then rename it.
A program that changes the folder all the time may never give a stable snapshot, so its periodic saves are skipped until it pauses. Save at steps, such as after each epoch.
The runner checks file metadata before and after packing and discards a
changed snapshot instead of publishing potentially torn state. A failed scan
or upload is logged and tried again later; the last good upload stays in S3.
The folder is limited to
100 MB. Unpacking refuses paths outside the folder and links that leave it.

Deploy the updated coordinator and worker image together: the coordinator
provides `checkpoint_archive_get_urls` and the attempt-specific upload URL.

## Limits

A command that runs past the job type's time limit (`timeout_seconds`, 1 hour
for `python`) is killed by Kubernetes, so that copy returns no log. The job then
fails like any other failed run.

The command runs as user 65534 with the Pod's CPU, memory and time limits, and
no service account token. Approved images keep unknown libraries off the PCs,
but the command itself is still the user's own code. Job Pods have no network
policy yet, so it can reach the same network as the PC.
