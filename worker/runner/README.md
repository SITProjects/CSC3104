# Generic job runner

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
script quietly training on the CPU. The image must have PyTorch. ROCm builds
of PyTorch (`worker/pytorch-rocm-job`) answer through the same `torch.cuda`
API, so the check is the same on AMD GPUs.

The container sees every GPU on its node. If some are integrated (an AMD APU
next to a discrete card), the runner hides them from the command with
`HIP_VISIBLE_DEVICES` (`CUDA_VISIBLE_DEVICES` on NVIDIA) and logs it, so
`cuda:0` is a discrete GPU. A node with only an integrated GPU is never
labelled a GPU worker unless its owner asked for it (`infra/join-worker.sh`,
`IDLECLOUD_ALLOW_INTEGRATED_GPU=1`), and then the runner uses that GPU.

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
- The coordinator runs the copy again on another PC. Evictions don't use up
  its 3 retries (see "Long jobs" in `services/coordinator/README.md`). Each
  attempt uploads to its own
  `jobs/{job}/replicas/{replica}/attempts/{attempt}/checkpoint.tar.gz` key, so a
  lost copy's late upload cannot replace a newer copy's progress. The manifest
  lists the last reported archive first, then the other earlier attempts
  newest first; the runner tries them in that order,
  skipping missing or unusable archives. If none is usable, it starts fresh
  and the log says so.
- A program without checkpoints just starts again from the beginning.

Write checkpoint files whole, for example to a new file and then rename it.
A program that changes the folder all the time may never give a stable snapshot, so its periodic saves are skipped until it pauses. Save at steps, such as after each epoch.
The runner checks file metadata before and after packing and discards a
changed snapshot instead of publishing potentially torn state. A failed scan
or upload is logged and tried again later; the last good upload stays in S3.
The folder is limited to the job type's `checkpoint_max_mib` (100 MB, or
500 MB for `pytorch-gpu`), sent as `checkpoint_max_bytes` in the manifest.

After each upload the runner reports it to `POST /internal/replicas/{id}/checkpoint`
with its SHA-256 and size, and with the training step if the program wrote
`{"step": N}` to `$CHECKPOINT_DIR/idlecloud.json` (the GPU image's
`idlecloud_ckpt` helper does). A failed report is only logged: S3 has the
archive anyway.

## Progress

The command can move the job page's progress bar by writing how far along it
is, a number from 0 to 100, to `$PROGRESS_FILE` (`.idlecloud-progress` in the
work folder). Every 10 s (`PROGRESS_INTERVAL_SECONDS`) the runner reads it and,
when the whole-number value changed, sends it to
`POST /internal/replicas/{id}/progress`. Write it whole (a new file, then a
rename), e.g. with `idlecloud_ckpt.report_progress(42.5)` in the GPU image.

A command that doesn't write the file still moves the bar if it prints its
progress: the last `epoch`, `step` or `iter` line with `k/N` (or `k of N`)
counts, else the last tqdm bar's `NN%|`. This fallback only goes up, so a bar
that restarts each epoch can't pull it back, and stops at 99% until the copy
is done. The file wins whenever it exists.

Presigned URLs can stop working during a long job, when the hub's AWS
credentials rotate. An upload refused with 403 fetches the manifest again and
is retried once with the new URL.

A Pod stopped on its own node because the hub couldn't be reached (the node
agent's local stop) exits 143 like an eviction, and the coordinator counts it
as one when it sees the Pod. Unpacking refuses paths outside the folder and links that leave it.

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
