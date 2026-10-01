FROM mcr.microsoft.com/devcontainers/python:1-3.12-bookworm

# Pin uv independently of Python; Python packages are installed as the vscode user.
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /uvx /usr/local/bin/

RUN rm -f /etc/apt/sources.list.d/yarn.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        postgresql-client \
        libpq-dev \
        build-essential \
        curl \
        ca-certificates \
        jq \
        iputils-ping \
        iproute2 \
        dnsutils \
        python3-venv \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN python3 -m venv /opt/pre-commit \
    && /opt/pre-commit/bin/pip install --no-cache-dir pre-commit

ENV PATH="/opt/pre-commit/bin:${PATH}"

WORKDIR /csc3104
