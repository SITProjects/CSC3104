-- Estimated burst cost (member 6, plan step 5). The autoscaler writes these
-- tables. Member 5's admin page reads the views at the end.

-- One row per burst machine the autoscaler recorded on a slot.
CREATE TABLE burst_machines (
    instance_id   TEXT PRIMARY KEY,
    slot          INT NOT NULL,
    node_name     TEXT NOT NULL,                -- reused per slot, e.g. burst-200
    machine       TEXT NOT NULL,                -- EC2 instance type
    market        TEXT NOT NULL DEFAULT '',     -- spot or on-demand
    usd_per_hour  NUMERIC(10, 6),               -- rate used, instance plus EBS. NULL = unknown
    cpu_m         INT,                          -- millicores task pods can request. NULL = unknown
    launched_at   TIMESTAMPTZ NOT NULL,         -- EC2 LaunchTime once seen
    ended_at      TIMESTAMPTZ                   -- NULL while running
);

-- One row per task pod that ran on a burst machine. Each attempt is its own pod.
CREATE TABLE burst_runs (
    pod_uid       TEXT PRIMARY KEY,
    instance_id   TEXT NOT NULL REFERENCES burst_machines (instance_id),
    job_id        UUID,                         -- from pod labels. NULL = not known
    task_id       BIGINT,
    replica_id    BIGINT,
    attempt       INT,
    cpu_m         INT NOT NULL,                 -- CPU request
    started_at    TIMESTAMPTZ NOT NULL,         -- container start
    finished_at   TIMESTAMPTZ,                  -- container finish. NULL = never seen
    last_seen_at  TIMESTAMPTZ NOT NULL          -- last time the autoscaler saw the pod
);

CREATE INDEX burst_runs_job_idx      ON burst_runs (job_id);
CREATE INDEX burst_runs_instance_idx ON burst_runs (instance_id);

-- Cost of each run: its time on the machine times the machine's rate, times
-- the share of the machine's CPU it reserved. A run with no finish time is
-- counted up to when it was last seen, and marked estimated.
CREATE VIEW burst_run_cost AS
SELECT
    r.pod_uid, r.instance_id, r.job_id, r.task_id, r.replica_id, r.attempt,
    r.cpu_m, r.started_at, r.finished_at,
    r.finished_at IS NULL AS estimated,
    GREATEST(0, EXTRACT(EPOCH FROM (
        LEAST(COALESCE(r.finished_at, r.last_seen_at), COALESCE(m.ended_at, now()))
        - GREATEST(r.started_at, m.launched_at)
    )) / 3600)
    * m.usd_per_hour
    * LEAST(1, r.cpu_m::NUMERIC / NULLIF(m.cpu_m, 0)) AS usd
FROM burst_runs r
JOIN burst_machines m USING (instance_id);

-- Estimated burst cost per job. usd is NULL when no run has a known price.
CREATE VIEW burst_job_cost AS
SELECT
    job_id,
    COUNT(*)              AS runs,
    SUM(usd)              AS usd,
    bool_or(estimated)    AS has_estimated_runs,
    bool_or(usd IS NULL)  AS has_unknown_price
FROM burst_run_cost
WHERE job_id IS NOT NULL
GROUP BY job_id;

-- Each machine's whole cost, the part attributed to jobs, and the rest (boot,
-- idle, unrequested CPU, runs with no known job and work never seen). So
-- attributed_usd always matches the burst_job_cost rows for that machine.
CREATE VIEW burst_machine_cost AS
SELECT
    m.instance_id, m.slot, m.node_name, m.machine, m.market, m.usd_per_hour,
    m.launched_at, m.ended_at,
    m.ended_at IS NULL AS running,
    t.total_usd,
    COALESCE(a.attributed_usd, 0)               AS attributed_usd,
    t.total_usd - COALESCE(a.attributed_usd, 0) AS unattributed_usd
FROM burst_machines m
CROSS JOIN LATERAL (
    SELECT EXTRACT(EPOCH FROM (COALESCE(m.ended_at, now()) - m.launched_at)) / 3600
           * m.usd_per_hour AS total_usd
) t
LEFT JOIN (
    SELECT instance_id, SUM(usd) AS attributed_usd
    FROM burst_run_cost
    WHERE job_id IS NOT NULL
    GROUP BY instance_id
) a USING (instance_id);
