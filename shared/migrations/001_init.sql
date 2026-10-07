CREATE TYPE job_status     AS ENUM ('PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED');
CREATE TYPE task_status    AS ENUM ('PENDING', 'RUNNING', 'VOTING', 'VERIFIED', 'DISPUTED', 'FAILED');
CREATE TYPE replica_status AS ENUM ('SCHEDULED', 'RUNNING', 'EVICTED', 'LOST', 'DONE', 'FAILED');
CREATE TYPE node_state     AS ENUM ('IDLE', 'BUSY', 'OFFLINE');

CREATE TABLE nodes (
    name            TEXT PRIMARY KEY,                      -- Kubernetes node name
    trust_score     NUMERIC(4, 3) NOT NULL DEFAULT 0.5
                    CHECK (trust_score BETWEEN 0 AND 1),
    state           node_state NOT NULL DEFAULT 'OFFLINE',
    last_heartbeat  TIMESTAMPTZ,
    total_agree     INT NOT NULL DEFAULT 0,
    total_disagree  INT NOT NULL DEFAULT 0
);

-- Vetted job types offered by the platform. Users pick one; they never supply images.
CREATE TABLE job_types (
    name               TEXT PRIMARY KEY,                   -- e.g. 'ocr'
    description        TEXT NOT NULL DEFAULT '',
    image              TEXT NOT NULL,                      -- pinned by digest for deterministic output
    command            TEXT[],                             -- NULL = image's default entrypoint
    default_cpu        INT NOT NULL CHECK (default_cpu > 0),  -- millicores
    default_mem        INT NOT NULL CHECK (default_mem > 0),  -- MiB
    timeout_seconds    INT NOT NULL DEFAULT 3600,
    enabled            BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE jobs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_email        TEXT NOT NULL,
    job_type           TEXT NOT NULL REFERENCES job_types(name),
    params             JSONB NOT NULL DEFAULT '{}',        -- user-chosen options for this type
    status             job_status NOT NULL DEFAULT 'PENDING',
    task_count         INT NOT NULL CHECK (task_count > 0),
    replicas_per_task  INT NOT NULL DEFAULT 3 CHECK (replicas_per_task BETWEEN 1 AND 5),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at        TIMESTAMPTZ
);

CREATE TABLE tasks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    task_index   INT NOT NULL CHECK (task_index >= 0),     -- 0..task_count-1, passed as TASK_INDEX
    input        JSONB NOT NULL DEFAULT '{}',              -- per-task args, e.g. {"pages": [1, 10]}
    status       task_status NOT NULL DEFAULT 'PENDING',
    cpu_request  INT NOT NULL CHECK (cpu_request > 0),     -- millicores, e.g. 500
    mem_request  INT NOT NULL CHECK (mem_request > 0),     -- MiB, e.g. 256
    final_hash   CHAR(64),                                 -- SHA-256 hex of agreed output
    UNIQUE (job_id, task_index)
);

CREATE TABLE replicas (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    node_name     TEXT REFERENCES nodes(name),             -- NULL until scheduled
    status        replica_status NOT NULL DEFAULT 'SCHEDULED',
    attempt       INT NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    progress      INT NOT NULL DEFAULT 0
                  CHECK (progress BETWEEN 0 AND 100),      -- percent, reported by the program
    result_hash   CHAR(64),
    exit_code     INT,
    token_hash    CHAR(64),                                -- SHA-256 of REPLICA_TOKEN
    started_at    TIMESTAMPTZ,                             -- set when the Pod starts
    ended_at      TIMESTAMPTZ
);

CREATE TABLE events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    type       TEXT NOT NULL,                              -- e.g. 'replica_evicted'
    job_id     UUID REFERENCES jobs(id) ON DELETE CASCADE,
    task_id    BIGINT REFERENCES tasks(id) ON DELETE CASCADE,
    node_name  TEXT REFERENCES nodes(name) ON DELETE SET NULL,
    detail     JSONB
);

CREATE TABLE burst_slots (
    ip           INET PRIMARY KEY,
    node_name    TEXT,
    instance_id  TEXT,
    claimed_at   TIMESTAMPTZ
);
INSERT INTO burst_slots (ip) VALUES
    ('10.10.0.200'), ('10.10.0.201'), ('10.10.0.202'), ('10.10.0.203'), ('10.10.0.204');

CREATE INDEX tasks_status_idx      ON tasks (status);
CREATE INDEX replicas_task_idx     ON replicas (task_id);
CREATE INDEX replicas_status_idx   ON replicas (status);
CREATE INDEX replicas_node_idx     ON replicas (node_name) WHERE status = 'RUNNING';
CREATE INDEX events_job_time_idx   ON events (job_id, time);
