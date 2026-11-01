# PyTorch script image

Owner: member 6.

The approved image for the `pytorch` job type (migration 012): Python 3.12 with
CPU-only PyTorch, numpy, pandas and scikit-learn, and the generic job runner
(`worker/runner`) as its entrypoint. GPU support is future work.

Build from the repo root:

```
docker build -f worker/pytorch-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch:main .
```

`scripts/load-worker-images.sh` builds it and loads it into the local k3s
workers. On main, the "Publish worker images" workflow pushes it to GHCR as
`:main` and `:sha-<commit>`. Like the Python package, `idlecloud-pytorch` must be
made Public once by an org admin, because workers pull without registry keys.

The job type asks for the `:main` tag, not a digest. These jobs are run once and
not compared, so copies don't need identical images. A worker that already has
an older `:main` keeps using it, so set a `@sha256:` digest in `job_types` when a
change must reach every PC.

A user's script reads its files from the current folder. Files it saves in
`output/` (`$OUTPUT_DIR` in scripts) come back in the download, with the log.
Set PyTorch's thread count to match the CPU cores asked for. The example's
`--threads` defaults to 2, matching this job type's default of 2 cores.

`examples/train_digits.py` is a demo of checkpoints: it trains a small digits
classifier one epoch at a time and saves the model and Adam optimizer state.
It carries on from the last saved epoch if the job moves to another PC.
Recovery uses the latest usable upload from an earlier attempt and is not
guaranteed to include the last epoch saved locally. Upload it and run:

```
python train_digits.py --epochs 30 --seconds-per-epoch 2
```

Then press a key on the worker running it to evict the job. It continues on
another PC, and its `metrics.json` shows `resumed_from_epoch`. The download
also contains `model.pt`, the trained model's state dict.
