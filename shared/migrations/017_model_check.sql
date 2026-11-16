-- Platform scoring of trained models (GPU plan, phase 3).
--
-- A check is a copy of a task with check_of set to the training copy whose
-- model it scores. It runs the job type's check_image on a node labelled
-- idlecloud/checker=true (the hub or a CPU burst worker), never on a lending
-- PC, so the PC that trained the model can't fake its score.
--
-- SCORING is the task status while a check runs or waits for a checker node.
ALTER TYPE task_status ADD VALUE IF NOT EXISTS 'SCORING';

ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_of BIGINT REFERENCES replicas (id);
-- The check's outcome: 'scored', or 'not_scored' with check_reason (the model
-- or the evaluation file broke the contract). The score is top-1 accuracy
-- (0 to 1) on check_samples samples; the digests are SHA-256 of model.onnx and
-- of the evaluation file; check_evaluator names the checker's version.
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_status VARCHAR(16);
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_score DOUBLE PRECISION;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_samples INTEGER;
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_model_sha256 VARCHAR(64);
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_eval_sha256 VARCHAR(64);
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_evaluator VARCHAR(64);
ALTER TABLE replicas ADD COLUMN IF NOT EXISTS check_reason TEXT;
ALTER TABLE replicas DROP CONSTRAINT IF EXISTS replicas_check_status_check;
ALTER TABLE replicas ADD CONSTRAINT replicas_check_status_check
    CHECK (check_status IS NULL OR (check_of IS NOT NULL AND check_status IN ('scored', 'not_scored')));
ALTER TABLE replicas DROP CONSTRAINT IF EXISTS replicas_check_score_check;
ALTER TABLE replicas ADD CONSTRAINT replicas_check_score_check
    CHECK (check_score IS NULL OR (check_score >= 0 AND check_score <= 1));
CREATE INDEX IF NOT EXISTS replicas_check_of_idx ON replicas (check_of) WHERE check_of IS NOT NULL;

-- The accuracy the owner wants. The job page shows whether the score met it;
-- it never changes the job's own outcome.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS min_score DOUBLE PRECISION;
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_min_score_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_min_score_check
    CHECK (min_score IS NULL OR (min_score >= 0 AND min_score <= 1));

-- The image that scores this type's models; NULL means its models aren't scored
ALTER TABLE job_types ADD COLUMN IF NOT EXISTS check_image TEXT;
UPDATE job_types SET check_image = 'ghcr.io/sitprojects/idlecloud-model-check:main'
WHERE name = 'pytorch-gpu';
