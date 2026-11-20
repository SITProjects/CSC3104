# Python script image

The approved image for the `python` job type (migration 010): Python 3.12 with
the libraries in `requirements.txt` (numpy, pandas, scikit-learn) and the
generic job runner (`worker/runner`) as its entrypoint.

Build from the repo root:

```
docker build -f worker/python-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-python:main .
```

`scripts/load-worker-images.sh` builds it and loads it into the local k3s
workers. On main, the "Publish worker images" workflow pushes it to GHCR as
`:main` and `:sha-<commit>`. Like the OCR package, `idlecloud-python` must be
made Public once by an org admin, because workers pull without registry keys.

The job type asks for the `:main` tag, not a digest. These jobs are run once and
not compared, so copies don't need identical images. A worker that already has
an older `:main` keeps using it, so set a `@sha256:` digest in `job_types` when a
change must reach every PC.

A user's script reads its files from the current folder and writes what it
wants back to `$OUTPUT_DIR`:

```python
import os
import pandas as pd

df = pd.read_csv("data.csv")
df.describe().to_csv(os.path.join(os.environ["OUTPUT_DIR"], "summary.csv"))
```

`examples/train_resumable.py` is a demo of checkpoints: it trains a
scikit-learn classifier one epoch at a time and carries on from the last saved
epoch if the job moves to another PC. Recovery uses the latest usable upload
from an earlier attempt; it is not guaranteed to include the last epoch saved
locally. Upload it and run
`python train_resumable.py --epochs 40 --seconds-per-epoch 3`, then press a key
on the worker running it. The job continues on another PC, and its
`metrics.json` shows `resumed_from_epoch`.
