-- /internal/jobs/{id}/cancel marks unfinished tasks and replicas CANCELLED.
-- job_status already has it.
ALTER TYPE task_status    ADD VALUE IF NOT EXISTS 'CANCELLED';
ALTER TYPE replica_status ADD VALUE IF NOT EXISTS 'CANCELLED';
