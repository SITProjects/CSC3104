-- Job files can be deleted by their owner or after the retention period.
-- Keep the job and its statistics after its files have gone.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS files_deleted_at TIMESTAMPTZ;

-- Housekeeping visits finished jobs whose files have not been removed yet.
CREATE INDEX IF NOT EXISTS jobs_files_to_delete_idx ON jobs (finished_at)
    WHERE finished_at IS NOT NULL AND files_deleted_at IS NULL;
