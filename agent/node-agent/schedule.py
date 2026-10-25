"""Owner-set schedule: times of day when this PC is never lent out, whatever the
sensor says. Pure logic only, like decision.py.

Windows are written "HH:MM-HH:MM", comma separated, in the PC's local time, for
example "09:00-10:00,13:00-14:00". A window that ends before it starts crosses
midnight ("22:00-02:00"). Each window covers its start minute and stops just
before its end minute.
"""

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def __contains__(self, t: time) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # crosses midnight

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


@dataclass(frozen=True)
class Schedule:
    windows: tuple[Window, ...] = ()

    def blocked_at(self, now: datetime) -> str | None:
        """The reason the PC must not be lent at `now`, or None if it may be."""
        t = now.time()
        for window in self.windows:
            if t in window:
                return f"owner schedule {window}"
        return None

    def __str__(self) -> str:
        return ", ".join(str(w) for w in self.windows) or "none"


def parse(text: str) -> Schedule:
    """Parse "HH:MM-HH:MM,...". Empty means no blocked windows. Raises
    ValueError on anything else, so a typo fails at start-up instead of
    silently lending the PC during the owner's hours."""
    windows = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            start, end = (time.fromisoformat(p.strip()) for p in part.split("-"))
        except ValueError:
            raise ValueError(f"bad schedule window {part!r}, expected HH:MM-HH:MM")
        if start == end:
            raise ValueError(f"schedule window {part!r} is empty")
        windows.append(Window(start, end))
    return Schedule(tuple(windows))
