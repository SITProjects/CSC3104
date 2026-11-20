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
- The NVIDIA container toolkit, installed before `infra/join-worker.sh` runs.
  The script points the node's `idlecloud-gpu` runtime handler at
  `nvidia-container-runtime` and labels the node `idlecloud/gpu=nvidia` (and
  `idlecloud/gpu-vram-gib`).

AMD GPU workers run the same jobs from the ROCm build of this image,
`worker/pytorch-rocm-job`: their K3s pulls it in place of this one with the
same tag. A job never says which it gets.

The coordinator gives GPU copies the `idlecloud-gpu` runtime class and
`NVIDIA_VISIBLE_DEVICES=all` (and `AMD_VISIBLE_DEVICES=all`), keeps them on
labelled nodes, and runs at most one GPU copy per node. Before the user's
command starts, the runner checks that PyTorch can use a GPU and logs which
one. If it can't, the command is not started and the copy ends with exit code
69, so a broken GPU setup shows up at once instead of as training that quietly
runs on the CPU.

## Writing a script for it

A user's script reads its files from the current folder and saves what it
returns in `$OUTPUT_DIR`. To survive the PC's owner coming back, it saves its
progress to `$CHECKPOINT_DIR` by time, not only at the end of an epoch, and
loads it at start. The runner uploads that folder when it changes (checked
every 60 s); a moved job restarts from the last upload.

The image has a small helper for this, `idlecloud_ckpt`. It writes the state
whole (a new file, then a rename), saves by time, and records the step, which
the job page shows as "saved 4 min ago, step 1200". `load()` maps the state to
this node's device, so a job can carry on on a different GPU:

```python
from idlecloud_ckpt import Checkpointer

ckpt = Checkpointer(every_seconds=300)
state = ckpt.load()
if state:
    model.load_state_dict(state["model"])
    optimizer.load_state_dict(state["optimizer"])
    step = state["step"]
...
if ckpt.due():
    ckpt.save(
        {
            "model": model.state_dict(),
            "optimizer": optimizer.state_dict(),
            "step": step,
        },
        step=step,
    )
```

The checkpoint folder may hold up to 500 MB. A job still unfinished 24 hours
after it was submitted fails, however often it was moved.

`examples/train_gpu.py` shows the pattern: a small convolutional digits
classifier that saves the model, optimizer, position in the epoch and random
state every `--save-seconds`, and stops with an error if there is no GPU.

```
python train_gpu.py --epochs 40 --save-seconds 30
```

Its log starts with `cuda: True` and the GPU's name; `metrics.json` in the
download shows where a resumed run carried on from.

## Getting the model scored

If the job has an evaluation file (the "Score the model" fields on the New job
page), the platform scores the trained model after training, on its own
checker (`worker/model-check`), not on the PC that trained it. For that, save
the model as `output/model.onnx` with one float input and class logits out:

```python
torch.onnx.export(
    model.cpu().eval(),
    (example_batch.cpu(),),
    os.path.join(os.environ["OUTPUT_DIR"], "model.onnx"),
    input_names=["x"],
    output_names=["logits"],
    dynamo=True,
    external_data=False,  # one file: the checker reads only model.onnx
    dynamic_shapes=({0: torch.export.Dim("batch")},),
)
```

The image has `onnx` and `onnxscript` for this. The evaluation file is an
`.npz` with `x` (samples, in the shape the model takes) and `y` (integer
labels), at most 10,000 samples. The job page then shows the top-1 accuracy,
the sample count, the model's SHA-256 and, if you set one, whether the target
accuracy was met. A model that can't be exported is still returned; the job is
just "not scored", with the reason.

Examples:

- `examples/train_gpu.py` exports `model.onnx` at the end.
  `examples/make_eval_digits.py` writes its `eval.npz` (the digits it held out).
- `examples/gpu_test.py` is a first job to try on a new GPU worker. It
  reports the GPU, benchmarks it against the CPU, then trains an MLP on
  synthetic data with checkpoints. `--samples` sets the RAM it needs (about
  600000 fit the default 4096 MiB, 900000 fit 6144 MiB), and `--amp` turns on
  FP16 mixed precision, which only pays off on GPUs with tensor cores.
- `examples/random_model.py` is a test fixture: random weights and a log that
  claims 99.9%. The platform's score is about 10%, and no PC's trust changes.
