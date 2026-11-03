# The "python" job type's approved image: the shared runner plus common data and
# ML libraries. Build from the repo root, so the runner can be copied in:
#   docker build -f worker/python-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-python:main .
# The tag must match the image in shared/migrations/010_python_job_type.sql.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# The libraries go into the system Python, the one the runner and user commands
# (python train.py) use. uv is only needed while building.
COPY worker/python-job/requirements.txt /opt/idlecloud/requirements.txt
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache -r /opt/idlecloud/requirements.txt

COPY worker/runner/runner.py /opt/idlecloud/runner.py

# The Pod runs as user 65534 and the runner works in a temporary folder.
USER 65534:65534

ENTRYPOINT ["python", "/opt/idlecloud/runner.py"]
