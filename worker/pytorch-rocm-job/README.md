# PyTorch GPU script image, ROCm build

The image AMD GPU workers run for the `pytorch-gpu` job type. It is
`worker/pytorch-gpu-job` with the ROCm build of PyTorch (`2.15.0+rocm7.14`)
in place of the CUDA one. It has the same runner, libraries, pins and
checkpoint helper, so a job behaves the same on either vendor.

Jobs never name this image. The coordinator names
`ghcr.io/sitprojects/idlecloud-pytorch-gpu:<tag>` for every GPU copy. An AMD
node's K3s has a `registries.yaml` rewrite, written by `infra/join-worker.sh`,
so it pulls `ghcr.io/sitprojects/idlecloud-pytorch-rocm:<tag>` instead. That
means this image needs every tag the CUDA image has. The publish workflow
pushes both as `:main` and `:sha-<commit>` from the same commit.

Build from the repo root:

```
docker build -f worker/pytorch-rocm-job/Dockerfile -t ghcr.io/sitprojects/idlecloud-pytorch-rocm:main .
```

The build ends by importing torch and checking that it is a ROCm build.

The image is several GB. The workflow frees runner disk space before building
it. It checks anonymous access by resolving the image, not by pulling it.
Like the other worker packages, `idlecloud-pytorch-rocm` must be made Public
once by an org admin after its first publish. Until then, AMD workers can't
pull it and the workflow's access check fails.

## What an AMD GPU worker needs

- A discrete AMD GPU that this PyTorch build supports. That covers RDNA2 and
  later Radeon cards (gfx1030 and up) and Instinct cards. Integrated graphics
  don't count (see `infra/README.md`).
- The `amdgpu` kernel driver with `/dev/kfd`. Ubuntu's own kernel has it. ROCm
  itself is not needed on the PC, because the image brings it.
- The AMD container toolkit (`amd-container-toolkit`), installed before
  `infra/join-worker.sh` runs. The script points the node's `idlecloud-gpu`
  runtime handler at `amd-container-runtime`. That runtime passes `/dev/kfd`
  and the GPU's render node into the container (`AMD_VISIBLE_DEVICES=all`).

The ROCm wheel loads `libnuma`, `libelf`, `libdrm` and `libdrm_amdgpu` from
the system, which is why the Dockerfile installs them.
Phase 0 found this set on a Radeon 780M under Podman.
