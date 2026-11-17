# The "pytorch-gpu" job type's approved image: the shared runner plus CUDA
# PyTorch and common data libraries. Build from the repo root, so the runner is
# copied in:
#   docker build -f worker/pytorch-gpu-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch-gpu:main .
# The tag must match the image in shared/migrations/014_pytorch_gpu_job_type.sql.
#
# No CUDA base image is needed: the torch wheel brings the CUDA libraries, and
# the NVIDIA container runtime on the worker mounts the driver in.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# The libraries go into the system Python, the one the runner and user commands
# (python train.py) use. uv is only needed while building.
COPY worker/pytorch-gpu-job/requirements.txt /opt/idlecloud/requirements.txt
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache -r /opt/idlecloud/requirements.txt

COPY worker/runner/runner.py /opt/idlecloud/runner.py
# The checkpoint helper user scripts import (from idlecloud_ckpt import Checkpointer)
COPY worker/pytorch-gpu-job/idlecloud_ckpt.py /opt/idlecloud/lib/idlecloud_ckpt.py
ENV PYTHONPATH=/opt/idlecloud/lib

# The Pod runs as user 65534 and the runner works in a temporary folder.
USER 65534:65534

ENTRYPOINT ["python", "/opt/idlecloud/runner.py"]
