-- Where one job's time went, from upload to completed, in seconds since upload.
--
-- Run it with the job id (from the job page's URL):
--   hub:   kubectl -n idlecloud-system exec -i postgres-0 -- psql -U admin -d csc3104 -v job=<job-id> < eval/job_timeline.sql
--   local: docker compose exec -T postgres psql -U admin -d csc3104 -v job=<job-id> < eval/job_timeline.sql
--
-- "uploaded" comes from the job's params (recorded by the web app since tasks
-- start per rendered batch); older jobs start the clock at "job created".

WITH j AS (
    SELECT * FROM jobs WHERE id = :'job'
),
copies AS (
    SELECT min(r.started_at) AS first_launch,
           max(r.ended_at)   AS last_finish,
           count(*)          AS copies,
           count(DISTINCT r.node_name) AS nodes
    FROM replicas r JOIN tasks t ON t.id = r.task_id
    WHERE t.job_id = :'job'
),
start AS (
    SELECT coalesce((params->>'uploaded_at')::timestamptz, created_at) AS t0 FROM j
),
stages AS (
    SELECT 1 AS n, 'uploaded'                         AS stage, (j.params->>'uploaded_at')::timestamptz AS at FROM j
    UNION ALL SELECT 2, 'job created (first batch ready)', j.created_at   FROM j
    UNION ALL SELECT 3, 'first copy launched',             first_launch   FROM copies
    UNION ALL SELECT 4, 'last copy finished',              last_finish    FROM copies
    UNION ALL SELECT 5, 'completed',                       j.finished_at  FROM j
)
SELECT stage,
       to_char(at, 'HH24:MI:SS.MS') AS at,
       round(extract(epoch FROM at - (SELECT t0 FROM start))::numeric, 1) AS seconds
FROM stages
ORDER BY n;

-- The job's shape: how it was split, and how widely it ran
WITH j AS (
    SELECT * FROM jobs WHERE id = :'job'
),
copies AS (
    SELECT count(*) AS copies, count(DISTINCT r.node_name) AS nodes
    FROM replicas r JOIN tasks t ON t.id = r.task_id
    WHERE t.job_id = :'job'
)
SELECT j.status,
       j.params->>'page_count'     AS pages,
       j.task_count                AS tasks,
       j.params->>'pages_per_task' AS pages_per_task,
       c.copies,
       c.nodes                     AS distinct_nodes,
       round(extract(epoch FROM j.finished_at
                     - coalesce((j.params->>'uploaded_at')::timestamptz, j.created_at))::numeric, 1) AS total_seconds
FROM j, copies c;
