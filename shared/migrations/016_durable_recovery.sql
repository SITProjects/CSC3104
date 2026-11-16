-- Durable recovery for long jobs (GPU plan, phase 2).
--
-- failures counts what went wrong with a copy apart from its PC's owner coming
-- back: a lost node, a launch error, a node without a working GPU. A copy fails
-- once failures reaches RETRY_ATTEMPTS. Owner evictions don't count, so a long
-- job on PCs that get used now and then keeps going; job_types.max_age_hours
-- and MAX_ATTEMPTS still stop it in the end.
--
-- bad_nodes are nodes this copy must not go back to (it found no working GPU
-- there). The scheduler adds them to the nodes a copy avoids.
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS failures INTEGER NOT NULL DEFAULT 0;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS bad_nodes TEXT[] NOT NULL DEFAULT '{}';

-- The newest checkpoint archive the runner published and reported
-- (POST /internal/replicas/{id}/checkpoint): which attempt's key holds it, its
-- SHA-256 and size, the training step the script wrote next to it if any, and
-- when. The job page shows it as "last saved", and eviction events copy it so
-- lost work can be measured.
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS checkpoint_attempt INTEGER;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS checkpoint_sha256 VARCHAR(64);
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS checkpoint_bytes BIGINT;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS checkpoint_step BIGINT;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS checkpoint_at TIMESTAMPTZ;

-- checkpoint_max_mib caps the checkpoint folder the runner uploads.
-- max_age_hours, when set, fails a job still unfinished that long after it
-- was created, so a job that keeps getting evicted can't wait for ever.
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS checkpoint_max_mib INTEGER NOT NULL DEFAULT 100;
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS max_age_hours INTEGER;
ALTER TABLE job_types DROP CONSTRAINT IF EXISTS job_types_checkpoint_max_mib_check;
ALTER TABLE job_types ADD CONSTRAINT job_types_checkpoint_max_mib_check
    CHECK (checkpoint_max_mib BETWEEN 1 AND 2048);
ALTER TABLE job_types DROP CONSTRAINT IF EXISTS job_types_max_age_hours_check;
ALTER TABLE job_types ADD CONSTRAINT job_types_max_age_hours_check
    CHECK (max_age_hours IS NULL OR max_age_hours > 0);

-- A model and optimizer state can be a few hundred MB
UPDATE job_types SET checkpoint_max_mib = 500, max_age_hours = 24
WHERE name = 'pytorch-gpu';
