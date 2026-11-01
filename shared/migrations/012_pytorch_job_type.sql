-- worker/pytorch-job: the runner (worker/runner) with CPU-only PyTorch, numpy,
-- pandas and scikit-learn. The command comes from each task's input, so the
-- type's command is left NULL and the image's entrypoint (the runner) is used.
INSERT INTO job_types (
    name,
    description,
    image,
    command,
    default_cpu,
    default_mem,
    timeout_seconds,
    enabled,
    verify
) VALUES (
    'pytorch',
    'Your own Python script with PyTorch (CPU), numpy, pandas and scikit-learn, run once on an idle PC',
    'ghcr.io/sitprojects/idlecloud-pytorch:main',
    NULL,
    2,    -- cores
    2048, -- MiB
    3600,
    true,
    false
)
ON CONFLICT (name) DO NOTHING;
