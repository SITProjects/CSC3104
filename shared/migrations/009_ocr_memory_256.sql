-- OCR copies measured under 100 MiB at peak on dense 300 DPI pages, so 512 MiB
-- reserved far more than they use. 256 MiB keeps headroom and lets a t3.micro
-- (about 590 MiB free) run two copies instead of one. The web app sends each
-- job's resources itself (services/web/jobs.py); this keeps the type's default
-- in step.
UPDATE job_types SET default_mem = 256 WHERE name = 'ocr';
