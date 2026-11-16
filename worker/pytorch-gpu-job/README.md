# PyTorch GPU script image

The approved image for the `pytorch-gpu` job type (migration 014): Python 3.12
with CUDA PyTorch, numpy, pandas and scikit-learn, and the generic job runner
(`worker/runner`) as its entrypoint. It is the CPU image
(`worker/pytorch-job`) with the CUDA build of PyTorch.

Build from the repo root:

```
docker build -f worker/pytorch-gpu-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch-gpu:main .
```

On main, the "Publish worker images" workflow pushes it to GHCR as `:main` and
`:sha-<commit>`. Like the other worker packages, `idlecloud-pytorch-gpu` must
be made Public once by an org admin, because workers pull without registry keys.
It is several GB, so pull it on a GPU worker when it joins
(`infra/join-worker.sh` does) rather than at the first job.

## What a GPU worker needs

- An NVIDIA GPU of compute capability 7.5 or later (T4, any RTX card) and a
  driver new enough for CUDA 13 (580 or later). The image brings the CUDA
  libraries; the driver comes from the worker.
- The NVIDIA container toolkit, installed before the K3s agent starts, so K3s
  registers the `nvidia` runtime. K3s already ships the `nvidia` RuntimeClass.
- The node label `idlecloud/gpu=nvidia` (and `idlecloud/gpu-vram-gib`), which
  `infra/join-worker.sh` sets when `nvidia-smi` works.

The coordinator gives GPU copies the `nvidia` runtime class and
`NVIDIA_VISIBLE_DEVICES=all`, keeps them on labelled nodes, and runs at most
one GPU copy per node. Before the user's command starts, the runner checks that
PyTorch can use a CUDA device and logs which one. If it can't, the command is
not started and the copy ends with exit code 69, so a broken GPU setup shows up
at once instead of as training that quietly runs on the CPU.

## Writing a script for it

A user's script reads its files from the current folder and saves what it
returns in `$OUTPUT_DIR`. To survive the PC's owner coming back, it saves its
progress to `$CHECKPOINT_DIR` by time, not only at the end of an epoch, and
loads it at start. The runner uploads that folder when it changes (checked
every 60 s); a moved job restarts from the last upload.

`examples/train_gpu.py` shows the pattern: a small convolutional digits
classifier that saves the model, optimizer, position in the epoch and random
state every `--save-seconds`, and stops with an error if there is no GPU.

```
python train_gpu.py --epochs 40 --save-seconds 30
```

Its log starts with `cuda: True` and the GPU's name; `metrics.json` in the
download shows where a resumed run carried on from.
