-- IF NOT EXISTS: databases that ran this as shared/migrations/007_web.sql already have it.
-- Web app (member 5). An upload exists before its job: the coordinator only
-- learns about the job once every page is rendered, under the same id.
CREATE TABLE IF NOT EXISTS uploads (
    job_id           UUID PRIMARY KEY,                    -- becomes jobs.id once submitted
    owner_email      TEXT NOT NULL,
    filename         TEXT NOT NULL,
    size_bytes       BIGINT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'SPLITTING'
                     CHECK (status IN ('SPLITTING', 'SUBMITTED', 'FAILED', 'CANCELLED')),
    page_count       INT,                                 -- from the first pages-ready callback
    error            TEXT,                                -- shown to the user when FAILED
    webhook_url      TEXT,                                -- POSTed once the job ends
    webhook_sent_at  TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per pages-ready callback, so a retried callback is ignored.
CREATE TABLE IF NOT EXISTS upload_batches (
    job_id      UUID NOT NULL REFERENCES uploads(job_id) ON DELETE CASCADE,
    page_start  INT NOT NULL,
    page_end    INT NOT NULL CHECK (page_end >= page_start),
    PRIMARY KEY (job_id, page_start)
);

-- Public API keys. Only the SHA-256 of a key is stored.
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash     CHAR(64) PRIMARY KEY,
    owner_email  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS uploads_owner_idx ON uploads (owner_email, created_at DESC);
CREATE INDEX IF NOT EXISTS uploads_webhook_idx ON uploads (job_id)
    WHERE webhook_url IS NOT NULL AND webhook_sent_at IS NULL;
