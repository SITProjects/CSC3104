"""Node agent server. Receives the Windows sensor's readings on localhost:9100 and
decides whether this worker is IDLE or BUSY.

Run inside Ubuntu (WSL2), from this folder:
    python server.py
Set NODE_AGENT_DEMO=1 for the 30-second idle wait.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import config
import uvicorn
from decision import IdleDetector, Reading, Transition
from fastapi import FastAPI
from pydantic import BaseModel, Field

log = logging.getLogger("node-agent")

# Every handler and the tick loop are async, so they all run on one event loop
# thread and never touch the detector at the same time. Keep them async.
detector = IdleDetector()


def handle(change: Transition | None) -> None:
    """React to a state change."""
    if change is None:
        return
    log.info("%s -> %s (%s)", change.old, change.new, change.reason)
    # TODO step 4: add the owner-active taint on BUSY, remove it on IDLE.
    # TODO step 5: write the change to the evaluation log file.


async def watch_for_silence() -> None:
    while True:
        await asyncio.sleep(config.TICK_S)
        handle(detector.tick(time.monotonic()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "node agent started: IDLE after %.0fs without input%s",
        config.IDLE_INPUT_S,
        " (demo mode)" if config.DEMO_MODE else "",
    )
    watcher = asyncio.create_task(watch_for_silence())
    yield
    watcher.cancel()


app = FastAPI(title="idlecloud node agent", lifespan=lifespan)


class SensorReading(BaseModel):
    idle_seconds: float = Field(ge=0)
    cpu_percent: float = Field(ge=0, le=100)


class AgentState(BaseModel):
    state: str
    reason: str
    last_reading: SensorReading | None
    reading_age_s: float | None


@app.post("/state")
async def receive_state(reading: SensorReading) -> AgentState:
    handle(
        detector.observe(
            Reading(reading.idle_seconds, reading.cpu_percent), time.monotonic()
        )
    )
    return snapshot()


@app.get("/state")
async def current_state() -> AgentState:
    """Latest decision, for checking by hand and for the step 4/5 code."""
    return snapshot()


def snapshot() -> AgentState:
    last = detector.last_reading
    return AgentState(
        state=detector.state,
        reason=detector.reason,
        last_reading=(
            SensorReading(idle_seconds=last.idle_seconds, cpu_percent=last.cpu_percent)
            if last
            else None
        ),
        reading_age_s=(
            round(time.monotonic() - detector.last_reading_at, 1)
            if detector.last_reading_at is not None
            else None
        ),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        timeout_keep_alive=config.KEEP_ALIVE_S,
    )
