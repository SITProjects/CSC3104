# The "pytorch" job type's approved image: the shared runner plus CPU PyTorch
# and common data libraries. Build from the repo root, so the runner is copied in:
#   docker build -f worker/pytorch-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch:main .
# The tag must match the image in shared/migrations/012_pytorch_job_type.sql.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY worker/pytorch-job/requirements.txt /opt/idlecloud/requirements.txt
RUN pip install -r /opt/idlecloud/requirements.txt

COPY worker/runner/runner.py /opt/idlecloud/runner.py

# The Pod runs as user 65534 and the runner works in a temporary folder.
USER 65534:65534

ENTRYPOINT ["python", "/opt/idlecloud/runner.py"]
