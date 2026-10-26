FROM python:3.12-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV UV_PYTHON_DOWNLOADS=0 \
    UV_LINK_MODE=copy

WORKDIR /app

# Cache dependency installation separately from application changes.
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
# The verifier reuses the coordinator's database helpers (create_event, create_replicas)
COPY services/coordinator/ ./services/coordinator/
COPY services/verifier/ ./services/verifier/

USER 10001:10001

# No HTTP server, so no HEALTHCHECK: it only polls the database and Kubernetes
CMD ["python", "-m", "services.verifier.runner"]
