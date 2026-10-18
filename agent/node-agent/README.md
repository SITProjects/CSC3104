# Linux node agent

Owner: member 2.

Receive Windows sensor readings, decide whether the worker is idle or busy,
apply or remove the owner-active Kubernetes taint, and send coordinator
heartbeats. Run inside the worker's Linux environment.

## Rules

- Input in the last 10 seconds: BUSY right away.
- IDLE only after 5 minutes with no input (30 seconds with `NODE_AGENT_DEMO=1`)
  and host CPU under 20% for 3 readings in a row.
- No sensor reading for 30 seconds: BUSY.
- High CPU never turns an IDLE node BUSY, because our own task pods raise the
  host's CPU too.

Every number is in `config.py` and can be overridden with an environment variable.

## Run

Inside Ubuntu (WSL2). Requires Python 3.11+ (Ubuntu 24.04 ships 3.12):

```bash
cd agent/node-agent
pip install -r requirements.txt
NODE_AGENT_DEMO=1 python server.py
```

Then start the Windows sensor. Check the decision with
`curl localhost:9100/state`.

The server listens on `127.0.0.1` only. Anything that can reach the port can say
the PC is idle, so do not expose it on the WireGuard network.

The server keeps idle connections open for 30 seconds (`NODE_AGENT_KEEP_ALIVE_S`).
Keep this above the sensor's 5-second interval. At uvicorn's default of 5 seconds,
about 1 sensor reading in 3 fails with "Server disconnected without sending a
response".
