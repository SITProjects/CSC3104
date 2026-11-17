# The checker image: scores a trained model (model.onnx) on the owner's
# evaluation file, on a node the platform controls (idlecloud/checker=true).
# Build from the repo root:
#   docker build -f worker/model-check/Dockerfile -t ghcr.io/sitprojects/idlecloud-model-check:main .
# The tag must match job_types.check_image (shared/migrations/017_model_check.sql).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY worker/model-check/requirements.txt /opt/idlecloud/requirements.txt
RUN pip install --no-cache-dir -r /opt/idlecloud/requirements.txt

COPY worker/model-check/evaluate.py /opt/idlecloud/evaluate.py

# The Pod runs as user 65534 with a read-only root filesystem; everything is
# kept in memory
USER 65534:65534

ENTRYPOINT ["python", "/opt/idlecloud/evaluate.py"]
