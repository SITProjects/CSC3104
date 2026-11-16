-- GPU jobs: a user's PyTorch script trains on the NVIDIA GPU of an idle worker.
--
-- gpu says a job type's copies need a GPU node: the coordinator gives their
-- Pods the nvidia runtime class, keeps them on nodes labelled
-- idlecloud/gpu=nvidia (infra/join-worker.sh) and runs at most one GPU copy per
-- node. min_gpu_vram_gib, when set, also keeps them off nodes whose
-- idlecloud/gpu-vram-gib label is smaller.
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS gpu BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS min_gpu_vram_gib INTEGER;
ALTER TABLE job_types DROP CONSTRAINT IF EXISTS job_types_min_gpu_vram_gib_check;
ALTER TABLE job_types ADD CONSTRAINT job_types_min_gpu_vram_gib_check
    CHECK (min_gpu_vram_gib IS NULL OR (gpu AND min_gpu_vram_gib > 0));

-- worker/pytorch-gpu-job: the runner (worker/runner) with CUDA PyTorch, numpy,
-- pandas and scikit-learn. Like the CPU type, the command comes from each
-- task's input and the image's entrypoint (the runner) is used. Run once and
-- not compared (verify = false): two GPU runs of the same training need not
-- give the same bytes.
INSERT INTO job_types (
    name,
    description,
    image,
    command,
    default_cpu,
    default_mem,
    timeout_seconds,
    enabled,
    verify,
    gpu,
    min_gpu_vram_gib
) VALUES (
    'pytorch-gpu',
    'Your own Python script with PyTorch on an NVIDIA GPU (CUDA), numpy, pandas and scikit-learn, run once on an idle PC',
    'ghcr.io/sitprojects/idlecloud-pytorch-gpu:main',
    NULL,
    2,     -- cores
    4096,  -- MiB
    14400, -- 4 h per attempt
    true,
    false,
    true,
    NULL   -- any GPU node
)
ON CONFLICT (name) DO NOTHING;
