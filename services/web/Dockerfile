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
# The HMAC verify() and, for LOCAL_SPLIT, the renderer's own page code
COPY serverless/__init__.py ./serverless/__init__.py
COPY serverless/common/ ./serverless/common/
COPY serverless/splitter/ ./serverless/splitter/
COPY services/__init__.py ./services/__init__.py
COPY services/web/ ./services/web/

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).close()"]

CMD ["python", "-m", "uvicorn", "services.web.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
