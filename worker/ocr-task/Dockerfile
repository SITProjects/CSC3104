# Build from the repo root, so the shared helpers can be copied in:
#   docker build -f worker/ocr-task/Dockerfile -t ghcr.io/sitprojects/idlecloud-ocr:main .
# The tag must match the image in shared/migrations/013_ocr_ghcr_image.sql.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# one Tesseract thread per page, so the Pod stays within its CPU request
ENV OMP_THREAD_LIMIT=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# pinned, so every replica of a task runs identical code for voting (same as uv.lock)
# (uv is only needed while building)
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache \
    pytesseract==0.3.13 Pillow==12.3.0 python-dotenv==1.2.3

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app

# keep the shared.* package path, so imports match the repo
COPY shared/__init__.py shared/checkpoint.py shared/normalized_text.py ./shared/
COPY worker/ocr-task/main.py ./main.py

USER 10001:10001

ENTRYPOINT ["python", "main.py"]
