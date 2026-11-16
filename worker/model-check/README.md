# Model checker image

Scores a trained model for the platform (GPU plan, phase 3). It runs as a
check copy on a node the platform controls (`idlecloud/checker=true`), never on
the PC that trained the model, so the score on the job page can't be faked by
the lending PC. Image: `ghcr.io/sitprojects/idlecloud-model-check:main`
(`job_types.check_image`, migration 017), published by the "Publish worker
images" workflow; make the package Public once like the others.

```
docker build -f worker/model-check/Dockerfile -t ghcr.io/sitprojects/idlecloud-model-check:main .
```

## What it checks

1. `result.json` of the training attempt must hash to what the training copy
   reported (stored in the database), and `output.tar.gz` to the
   `archive_sha256` in it. So the model scored is exactly the one the owner
   downloads; the web app refuses a download that doesn't match either.
2. `output/model.onnx` is read from the archive in memory. Nothing is unpacked
   to disk, so paths, links and special files in the archive do nothing.
3. The evaluation file is an `.npz` with `x` (numbers) and `y` (1-D integer
   labels), at most 10,000 samples, loaded with `allow_pickle=False`.
4. The model must have one `tensor(float)` input and return
   `(samples, classes)` logits. Top-1 accuracy is measured with onnxruntime on
   one CPU core, in batches of 256 (one at a time for a model exported with a
   fixed batch of 1).

Limits: result.json 64 KB, archive 200 MB, model.onnx 100 MB (single file:
export with `external_data=False`), evaluation file 100 MB.

## Outcomes

It reports to `POST /internal/replicas/{id}/check`:

- `scored`: the accuracy, sample count, SHA-256 of model.onnx and of the
  evaluation file, and the evaluator version (`model-check/1`);
- `not_scored` with a reason such as `no_model`, `contract`, `model_invalid`,
  `eval_invalid`, `archive_mismatch` or `missing`.

Anything else, such as a network error, ends the copy with exit code 1. The
verifier then starts another check, up to three; a checker failure never
counts against the owner.

Why ONNX: an ONNX file is a graph, not code, so loading it runs nothing the
owner wrote, and the checker needs only numpy and onnxruntime (no PyTorch, no
GPU).
