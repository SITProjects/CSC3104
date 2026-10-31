# Generic job runner

Owner: member 6.

`runner.py` runs a user's own command for a generic job type and reports back
like any worker. Each approved image copies it in and uses it as the entrypoint
(see `worker/python-job`). It uses only the Python standard library, so any
image with Python 3.12 can include it.

## What it does

1. Gets the replica manifest from the coordinator. The task input holds the
   command and files: `{"command": ["python", "train.py"], "files": ["inputs/train.py"]}`.
2. Downloads the files into a new temporary folder and runs the command there,
   with `OUTPUT_DIR` (the `output` folder in it) and `HOME` set. The command
   does not get the replica token. A file named `output`, or one that can't be
   fetched, stops the run before the command starts, and the log says why.
3. Packs `output/` and `log.txt` (the command's output, first 5 MB) into
   `output.tar.gz` and uploads it. An output folder over 100 MB is left out and
   the log says so.
4. Uploads `result.json` (`exit_code` and the archive's SHA-256) and reports its
   hash with the command's exit code.

A job type with `verify = false` (migration 010) is decided by that exit code:
0 completes the job, anything else fails it, and the download still holds the
log.

## When the PC's owner returns

The runner passes SIGTERM to the command's process group, waits up to 20 s,
then kills it, reports the copy evicted and exits. The coordinator runs the copy
again from the start on another PC, up to the usual attempt limit. Saving and
restoring a checkpoint folder is the next step (milestone 2 of the generic jobs
plan).

## Limits

A command that runs past the job type's time limit (`timeout_seconds`, 1 hour
for `python`) is killed by Kubernetes, so that copy returns no log. The job then
fails like any other failed run.

The command runs as user 65534 with the Pod's CPU, memory and time limits, and
no service account token. Approved images keep unknown libraries off the PCs,
but the command itself is still the user's own code. Job Pods have no network
policy yet, so it can reach the same network as the PC.
