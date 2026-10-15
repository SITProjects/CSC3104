-- Seed the OCR workload (member 5's worker/ocr-task image) without overwriting an
-- existing job type. Build and load the image under this exact tag; pin it by
-- digest once it is final, so every replica runs identical code for voting.
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
    'ocr',
    'Tesseract OCR over page images listed in each task''s input.files',
    'idlecloud/ocr:0.1.0',
    NULL, -- the image's own entrypoint
    1,    -- cores
    512,  -- MiB
    1800,
    true
)
ON CONFLICT (name) DO NOTHING;
