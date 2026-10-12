# Burst autoscaler image. Build context is the repo root:
#   docker build -f services/autoscaler/Dockerfile -t idlecloud/autoscaler:0.1.0 .
# Bump the tag in k8s/autoscaler.yml to match on every rebuild.
FROM python:3.12-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock .python-version README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app

COPY --from=builder /app/.venv /app/.venv

COPY core/ ./core/
COPY shared/ ./shared/
COPY services/__init__.py ./services/__init__.py
COPY services/autoscaler/__init__.py ./services/autoscaler/__init__.py
COPY services/autoscaler/burst/ ./services/autoscaler/burst/
COPY services/autoscaler/controller/ ./services/autoscaler/controller/

USER 10001:10001

CMD ["python", "-m", "services.autoscaler.controller"]
