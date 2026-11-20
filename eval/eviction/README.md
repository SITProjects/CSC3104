# Eviction speed

Claim: owners get their PC back fast. Measures the time from a key press on a
lent worker to (1) the owner-active taint being applied and (2) the task pod
stopping, over 20 trials, plotted as a box plot.

## How it is measured

| Moment | Source | Clock |
|---|---|---|
| Key press | node-agent event log: BUSY line time minus `idle_seconds` | worker |
| Taint applied | node-agent event log: `taint` line | worker |
| Taint seen, pod stopped | `run_trials.py`, polling every 50 ms | EC2 |

Key press to taint uses only the worker clock, and taint to pod stopped only
the EC2 clock, so the two clocks never need to agree exactly.

The test pod exits as soon as it gets SIGTERM, so this times the eviction
itself. The real OCR worker takes longer on purpose: it finishes and saves its
current page first.

## Run

1. **Worker, WSL:** start the node agent. The higher CPU limit stops background
   apps from holding the PC BUSY between trials. Clear the old log first, so it
   only holds this run. Stop the agent's service first, since both use port
   9100, and start it again when the trials are done
   (`sudo systemctl start idlecloud-node-agent`). This uses the venv from the
   [node-agent README](../../agent/node-agent/README.md#run):
   ```bash
   sudo systemctl stop idlecloud-node-agent
   cd agent/node-agent
   source .venv/bin/activate
   mv node-agent-events.jsonl node-agent-events.old.jsonl 2>/dev/null
   NODE_AGENT_DEMO=1 NODE_AGENT_BLOCKED="" NODE_AGENT_IDLE_CPU_PERCENT=40 python3 server.py
   ```
   The Windows sensor must be running too. On a worker set up with
   `install-worker.ps1` it starts at login. Otherwise run `python sensor.py` in
   `agent\windows-sensor` (see the
   [sensor README](../../agent/windows-sensor/README.md#build-the-exe-developers-only)
   for its venv).

2. **EC2:** run the trials (about 1 minute each):
   ```bash
   python3 -m venv ~/eval-venv && source ~/eval-venv/bin/activate
   pip install kubernetes
   python run_trials.py --node <node-name> --trials 20
   ```
   Each trial waits for the PC to be lent, starts a pod, then prints
   `PRESS A KEY ON THE WORKER NOW`. Press one key on the worker, then leave the
   PC alone until the next prompt. A trial where the PC is taken back early is
   retried. Results are written to `eval/results/eviction_k8s.csv` after each
   trial, so a stopped run keeps its finished trials. The script refuses to
   start if that file already exists: move or delete it (and clear the event
   log as in step 1) before running again.

3. **Copy both files into `eval/results/`:** `eviction_k8s.csv` from EC2, and
   the worker's `agent/node-agent/node-agent-events.jsonl` as
   `eval/results/eviction_events.jsonl`.

4. **Analyse** (anywhere with the repo's Python environment):
   ```bash
   python eval/eviction/analyse.py
   ```
   Writes `eval/results/eviction_speed.csv` and `eval/graphs/eviction_speed.png`,
   and prints the median, IQR and range of each interval.

## Reading the result

Key press to taint is spread between about 0 and 5 seconds. That is the sensor's
5-second reading interval: a key press waits for the next reading. A shorter
interval would evict faster but send more readings.

`taint_to_delete_s` is always 0.0: the deletion happens within one 50 ms poll
of the taint.
