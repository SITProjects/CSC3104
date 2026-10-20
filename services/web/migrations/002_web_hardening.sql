-- IF NOT EXISTS: databases that ran this as shared/migrations/008_web_hardening.sql already have it.
-- Web app: retry failed webhooks, and remember how each job was split.
ALTER TABLE uploads
    ADD COLUMN IF NOT EXISTS webhook_attempts INT NOT NULL DEFAULT 0,   -- failed deliveries so far
    ADD COLUMN IF NOT EXISTS pages_per_task   INT NOT NULL DEFAULT 10;  -- the split used when it was submitted
