# The ROCm build of the "pytorch-gpu" job type's image, for AMD GPU workers:
# the same runner, libraries and checkpoint helper as worker/pytorch-gpu-job,
# with the ROCm build of PyTorch. Build from the repo root:
#   docker build -f worker/pytorch-rocm-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch-rocm:main .
#
# Jobs never name this image. The coordinator names idlecloud-pytorch-gpu for
# every GPU copy, and an AMD node's K3s pulls this one with the same tag in its
# place (registries.yaml, written by infra/join-worker.sh). So the tags here
# must follow the CUDA image's.
#
# No ROCm base image is needed: the torch wheel brings the ROCm libraries, and
# the AMD container runtime on the worker passes /dev/kfd and the GPU's render
# node in (under WSL2: /dev/dxg, see librocdxg below). The wheel still loads a
# few system libraries, installed below.
FROM ghcr.io/astral-sh/uv:0.13.5 AS uv
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        libatomic1 libdrm-amdgpu1 libdrm2 libelf1 libnuma1 \
    && rm -rf /var/lib/apt/lists/*

# torch from PyTorch's ROCm index first, then everything else from PyPI with
# the CUDA image's pins (its torch line left out, so the CUDA wheel never comes
# in). The ROCm wheel is a few GB.
ARG TORCH_INDEX=https://download.pytorch.org/whl/rocm7.14
ARG TORCH_VERSION=2.15.0+rocm7.14
COPY worker/pytorch-gpu-job/requirements.txt /opt/idlecloud/requirements.txt
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv pip install --system --no-cache --index-url "$TORCH_INDEX" "torch==$TORCH_VERSION" \
    && grep -v '^torch==' /opt/idlecloud/requirements.txt > /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python -c "import torch; assert torch.version.hip, torch.__version__; print('torch', torch.__version__, 'HIP', torch.version.hip)"

# librocdxg: ROCm's way to the GPU under WSL2 (Windows workers), through
# /dev/dxg instead of /dev/kfd. The ROCm runtime loads it by itself when there
# is no /dev/kfd; on Linux workers it stays unused. Release 1.2.1 pairs with
# ROCm 7.14 (github.com/ROCm/librocdxg). It needs libdxcore.so from the
# Windows driver, which the node's WSL runtime mounts in (infra/join-worker.sh).
ARG ROCDXG_VERSION=1.2.1
ARG ROCDXG_SHA256=7889eef45a1132ed2dde88d8ea1356bf791ec9c05802a18940bc81b970e850e0
ADD https://github.com/ROCm/librocdxg/releases/download/v${ROCDXG_VERSION}/rocdxg-roct_${ROCDXG_VERSION}_amd64.deb /tmp/rocdxg.deb
RUN echo "$ROCDXG_SHA256  /tmp/rocdxg.deb" | sha256sum -c - \
    && dpkg -i /tmp/rocdxg.deb \
    && rm /tmp/rocdxg.deb \
    && test -e /opt/rocm/lib/librocdxg.so.1

COPY worker/runner/runner.py /opt/idlecloud/runner.py
# The checkpoint helper user scripts import (from idlecloud_ckpt import Checkpointer)
COPY worker/pytorch-gpu-job/idlecloud_ckpt.py /opt/idlecloud/lib/idlecloud_ckpt.py
ENV PYTHONPATH=/opt/idlecloud/lib

# The Pod runs as user 65534 and the runner works in a temporary folder.
USER 65534:65534

ENTRYPOINT ["python", "/opt/idlecloud/runner.py"]
