"""The node agent's IDLE / BUSY rules. Pure logic only: readings and timestamps go
in, state changes come out. Nothing here talks to Kubernetes or the network, so
every rule can be tested with fake clocks.

Rules (from the implementation plan):
1. Input in the last BUSY_INPUT_S seconds means BUSY right away.
2. IDLE only after no input for IDLE_INPUT_S and CPU below IDLE_CPU_PERCENT for
   IDLE_CPU_READINGS readings in a row.
3. No reading for STALE_S seconds means BUSY.
4. Inside an owner-set schedule window the node is BUSY, and cannot go IDLE.

High CPU never makes an IDLE node BUSY. Once task pods run inside WSL2, the
Windows host sees that load too (as vmmem), so treating it as the owner being
back would evict our own work. Only keyboard and mouse input can do that.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

import config
from schedule import Schedule


class NodeState(StrEnum):
    IDLE = "IDLE"
    BUSY = "BUSY"


@dataclass(frozen=True)
class Thresholds:
    busy_input_s: float = config.BUSY_INPUT_S
    idle_input_s: float = config.IDLE_INPUT_S
    idle_cpu_percent: float = config.IDLE_CPU_PERCENT
    idle_cpu_readings: int = config.IDLE_CPU_READINGS
    stale_s: float = config.STALE_S


@dataclass(frozen=True)
class Reading:
    idle_seconds: float
    cpu_percent: float


@dataclass(frozen=True)
class Transition:
    old: NodeState
    new: NodeState
    reason: str


class IdleDetector:
    """Tracks one PC's state. `now` is a monotonic clock in seconds. `clock`
    gives the local wall-clock time, for the schedule."""

    def __init__(
        self,
        thresholds: Thresholds | None = None,
        schedule: Schedule | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.t = thresholds or Thresholds()
        self.schedule = schedule or Schedule()
        self.clock = clock
        # Start BUSY: we lend the PC only once the sensor has proven it is idle
        self.state = NodeState.BUSY
        self.reason = "starting up"
        self.last_reading: Reading | None = None
        self.last_reading_at: float | None = None
        self.low_cpu_streak = 0

    def observe(self, reading: Reading, now: float) -> Transition | None:
        """Apply one sensor reading."""
        previous = self.last_reading
        self.last_reading = reading
        self.last_reading_at = now

        if reading.cpu_percent < self.t.idle_cpu_percent:
            self.low_cpu_streak += 1
        else:
            self.low_cpu_streak = 0

        if blocked := self.schedule.blocked_at(self.clock()):
            return self._set(NodeState.BUSY, blocked)
        if reading.idle_seconds < self.t.busy_input_s:
            return self._set(NodeState.BUSY, f"input {reading.idle_seconds:.0f}s ago")
        # Idle time only goes down when there was input. Catches a key press the
        # rule above missed because a reading came late or the sensor skipped one.
        if previous is not None and reading.idle_seconds < previous.idle_seconds:
            return self._set(NodeState.BUSY, "input since last reading")

        if (
            self.state is NodeState.BUSY
            and reading.idle_seconds >= self.t.idle_input_s
            and self.low_cpu_streak >= self.t.idle_cpu_readings
        ):
            return self._set(
                NodeState.IDLE,
                f"no input for {reading.idle_seconds:.0f}s, "
                f"CPU under {self.t.idle_cpu_percent:.0f}% "
                f"for {self.low_cpu_streak} readings",
            )
        return None

    def tick(self, now: float) -> Transition | None:
        """Check for a silent sensor. Call this on a timer, not only when readings
        arrive: a dead sensor sends nothing, so nothing else would notice.
        Also starts a schedule window on time, between readings."""
        if blocked := self.schedule.blocked_at(self.clock()):
            return self._set(NodeState.BUSY, blocked)
        if self.last_reading_at is None:
            return None  # still BUSY from start-up
        silent_s = now - self.last_reading_at
        if silent_s <= self.t.stale_s:
            return None
        # Throw the old readings away so the PC must prove it is idle all over again
        self.last_reading = None
        self.last_reading_at = None
        self.low_cpu_streak = 0
        return self._set(NodeState.BUSY, f"no sensor reading for {silent_s:.0f}s")

    def _set(self, new: NodeState, reason: str) -> Transition | None:
        if new is self.state:
            return None
        change = Transition(self.state, new, reason)
        self.state = new
        self.reason = reason
        return change
