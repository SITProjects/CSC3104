"""Node agent tuning. Each value can be overridden with an environment variable.

Set NODE_AGENT_DEMO=1 to shorten the idle wait from 5 minutes to 30 seconds.
"""

import os


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


DEMO_MODE = os.environ.get("NODE_AGENT_DEMO", "") == "1"

# ---------- idle / busy rules ----------

# Input this recent means the owner is back: BUSY right away
BUSY_INPUT_S = _float("NODE_AGENT_BUSY_INPUT_S", 10)
# No input for this long (plus low CPU, below) before we lend the PC out. The gap
# between this and BUSY_INPUT_S stops the node flipping back and forth.
IDLE_INPUT_S = _float("NODE_AGENT_IDLE_INPUT_S", 30 if DEMO_MODE else 300)
# Host CPU must stay under this for IDLE_CPU_READINGS readings in a row
IDLE_CPU_PERCENT = _float("NODE_AGENT_IDLE_CPU_PERCENT", 20)
IDLE_CPU_READINGS = int(os.environ.get("NODE_AGENT_IDLE_CPU_READINGS", "3"))
# No sensor reading for this long means BUSY. Safe by default.
STALE_S = _float("NODE_AGENT_STALE_S", 30)

# ---------- server ----------

# Loopback only: anything that can reach this port can claim the PC is idle, so it
# must not be exposed on the WireGuard or cluster network. WSL2 forwards Windows
# localhost to it, which is all the sensor needs.
HOST = os.environ.get("NODE_AGENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("NODE_AGENT_PORT", "9100"))
# How often to check for a silent sensor
TICK_S = _float("NODE_AGENT_TICK_S", 1)
# Keep idle connections open longer than the sensor's 5 s interval. With uvicorn's
# default of 5 s, the server closes the connection just as the sensor reuses it,
# and about 1 reading in 3 fails with "Server disconnected".
KEEP_ALIVE_S = int(os.environ.get("NODE_AGENT_KEEP_ALIVE_S", "30"))
