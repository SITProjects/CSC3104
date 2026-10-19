-- Whole-machine CPU use from the latest node heartbeat, for the admin page and
-- the evaluation. NULL until the node's first heartbeat.
ALTER TABLE nodes ADD COLUMN cpu_percent REAL;
