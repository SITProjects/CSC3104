
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

CREATE TABLE jobs (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_email  TEXT NOT NULL,
    status       job_status NOT NULL DEFAULT 'PENDING',
    page_count   INT NOT NULL CHECK (page_count > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at  TIMESTAMPTZ
);

CREATE TABLE tasks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id       BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    page_start   INT NOT NULL CHECK (page_start >= 1),     -- 1-based, inclusive
    page_end     INT NOT NULL,                             -- inclusive
    status       task_status NOT NULL DEFAULT 'PENDING',
    cpu_request  INT NOT NULL CHECK (cpu_request > 0),     -- millicores, e.g. 500
    mem_request  INT NOT NULL CHECK (mem_request > 0),     -- MiB, e.g. 256
    final_hash   CHAR(64),                                 -- SHA-256 hex
    CHECK (page_end >= page_start)
);

CREATE TABLE replicas (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id       BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    node_name     TEXT REFERENCES nodes(name),             -- NULL until scheduled
    status        replica_status NOT NULL DEFAULT 'SCHEDULED',
    attempt       INT NOT NULL DEFAULT 1 CHECK (attempt >= 1),
    pages_done    INT NOT NULL DEFAULT 0 CHECK (pages_done >= 0),
    result_hash   CHAR(64),
    token_hash    CHAR(64),                                -- SHA-256 of REPLICA_TOKEN
    started_at    TIMESTAMPTZ,                             -- set when the Pod starts
    ended_at      TIMESTAMPTZ
);

CREATE TABLE events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    time       TIMESTAMPTZ NOT NULL DEFAULT now(),
    type       TEXT NOT NULL,                              -- e.g. 'replica_evicted'
    job_id     BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
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

CREATE INDEX tasks_job_idx         ON tasks (job_id);
CREATE INDEX tasks_status_idx      ON tasks (status);
CREATE INDEX replicas_task_idx     ON replicas (task_id);
CREATE INDEX replicas_status_idx   ON replicas (status);
CREATE INDEX replicas_node_idx     ON replicas (node_name) WHERE status = 'RUNNING';
CREATE INDEX events_job_time_idx   ON events (job_id, time);
