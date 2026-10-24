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
from decision import IdleDetector, NodeState, Reading, Transition
from fastapi import FastAPI
from pydantic import BaseModel, Field
from taint import NodeTainter, connect

log = logging.getLogger("node-agent")

# Every handler and the loops are async, so they all run on one event loop
# thread and never touch the detector at the same time. Keep them async.
detector = IdleDetector()
# Set on every state change to wake keep_taint_in_sync
state_changed = asyncio.Event()


def handle(change: Transition | None) -> None:
    """React to a state change."""
    if change is None:
        return
    log.info("%s -> %s (%s)", change.old, change.new, change.reason)
    state_changed.set()
    # TODO step 5: write the change to the evaluation log file.


async def keep_taint_in_sync(tainter: NodeTainter) -> None:
    """Make the node's owner-active taint match the detector: tainted when BUSY,
    untainted when IDLE.

    Runs apart from handle() so a slow or unreachable API server never holds up
    the sensor, and a failed change is retried instead of lost. If it were lost,
    the cluster would keep sending pods here while the owner is typing. The
    detector starts BUSY, so the first pass taints the node before it is lent.
    """
    applied: NodeState | None = None
    while True:
        # Clear before reading the state, so a change during the API call wakes
        # the next wait straight away
        state_changed.clear()
        wanted = detector.state
        if wanted is not applied:
            try:
                changed = await asyncio.to_thread(
                    tainter.set_owner_active, wanted is NodeState.BUSY
                )
            except Exception:
                log.exception(
                    "could not set taint for %s, retrying in %.0fs",
                    wanted,
                    config.TAINT_RETRY_S,
                )
                await asyncio.sleep(config.TAINT_RETRY_S)
                continue
            applied = wanted
            if changed:
                log.info(
                    "%s taint on %s",
                    "added" if wanted is NodeState.BUSY else "removed",
                    tainter.node_name,
                )
        try:
            await asyncio.wait_for(state_changed.wait(), config.TAINT_RESYNC_S)
        except TimeoutError:
            applied = None  # check the node again, someone may have edited it


async def taint_on_exit(tainter: NodeTainter) -> None:
    """Best effort: leave the node tainted when the agent stops, because with
    no agent nobody would notice the owner coming back."""
    try:
        await asyncio.wait_for(
            asyncio.to_thread(tainter.set_owner_active, True), config.TAINT_RETRY_S
        )
    except Exception:
        log.exception("could not taint %s on exit", tainter.node_name)


async def watch_for_silence() -> None:
    while True:
        await asyncio.sleep(config.TICK_S)
        # Keep the watchdog alive if handle() fails, or stale-sensor detection stops
        try:
            handle(detector.tick(time.monotonic()))
        except Exception:
            log.exception("tick failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "node agent started: IDLE after %.0fs without input%s",
        config.IDLE_INPUT_S,
        " (demo mode)" if config.DEMO_MODE else "",
    )
    tasks = [asyncio.create_task(watch_for_silence())]
    tainter = None
    if config.TAINT_ENABLED:
        # Fails here, at start-up, if the kubeconfig is missing or broken
        tainter = NodeTainter(connect(config.KUBECONFIG), config.NODE_NAME)
        tasks.append(asyncio.create_task(keep_taint_in_sync(tainter)))
        log.info("managing the owner-active taint on node %s", config.NODE_NAME)
    else:
        log.warning("NODE_AGENT_TAINT=0: decisions are only logged, node not tainted")
    yield
    for task in tasks:
        task.cancel()
    if tainter is not None:
        await taint_on_exit(tainter)


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
