-- Generic jobs: a user runs their own command on one idle PC.
--
-- verify says whether a job type's results are checked by voting and spot
-- checks. OCR and the test workload keep it. A generic job runs once: a
-- successful run is accepted and a failed one fails the task, because two runs
-- of a user's program (ML training, for example) need not give the same bytes.
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS verify BOOLEAN NOT NULL DEFAULT true;

-- worker/python-job: the runner (worker/runner) with numpy, pandas and
-- scikit-learn. The command comes from each task's input, so the type's command
-- is left NULL and the image's entrypoint (the runner) is used.
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
    'python',
    'Your own Python script with numpy, pandas and scikit-learn, run once on an idle PC',
    'ghcr.io/sitprojects/idlecloud-python:main',
    NULL,
    1,    -- cores
    1024, -- MiB
    3600,
    true,
    false
)
ON CONFLICT (name) DO NOTHING;
