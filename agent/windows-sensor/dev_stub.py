"""
Throwaway stand-in for the node agent's /state endpoint, for testing the sensor
before the real node agent (step 3) exists.

    uvicorn dev_stub:app --port 9100

TODO: remove this file once the real node agent (agent/node-agent) is up.
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class SensorState(BaseModel):
    idle_seconds: float
    cpu_percent: float


@app.post("/state")
def receive_state(state: SensorState) -> dict[str, bool]:
    print(f"idle={state.idle_seconds}s cpu={state.cpu_percent}%")
    return {"ok": True}
