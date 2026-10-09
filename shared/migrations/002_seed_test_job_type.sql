-- Seed a simple test workload without overwriting an existing job type.
INSERT INTO job_types (
    name,
    description,
    image,
    command,
    default_cpu,
    default_mem,
    timeout_seconds,
    enabled
) VALUES (
    'test',
    'Simple test workload',
    'busybox:1.37.0',
    ARRAY['sh', '-c', 'echo "Test job completed"'],
    1, -- in millicores --
    1, -- in Mi --
    60,
    true
)
ON CONFLICT (name) DO NOTHING;
