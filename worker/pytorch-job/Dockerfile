# The "pytorch" job type's approved image: the shared runner plus CPU PyTorch
# and common data libraries. Build from the repo root, so the runner is copied in:
#   docker build -f worker/pytorch-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch:main .
# The tag must match the image in shared/migrations/012_pytorch_job_type.sql.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# The libraries go into the system Python, the one the runner and user commands
# (python train.py) use. uv is only needed while building. unsafe-best-match
# picks each version across PyPI and the PyTorch index, as pip does; by default
# uv would take numpy and the rest only from the PyTorch index.
COPY worker/pytorch-job/requirements.txt /opt/idlecloud/requirements.txt
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --index-strategy unsafe-best-match \
    -r /opt/idlecloud/requirements.txt

COPY worker/runner/runner.py /opt/idlecloud/runner.py

# The Pod runs as user 65534 and the runner works in a temporary folder.
USER 65534:65534

ENTRYPOINT ["python", "/opt/idlecloud/runner.py"]
