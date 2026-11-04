"""The My jobs filters: which of a user's jobs the page should show."""

from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

MAX_SEARCH_CHARS = 100
# The statuses the coordinator gives a job, and how the site names them
JOB_STATUSES = {
    "PENDING": "Queued",
    "RUNNING": "Running",
    "COMPLETED": "Completed",
    "FAILED": "Failed",
    "CANCELLED": "Cancelled",
}
MAX_WHOLE_NUMBER = 1_000_000


def parse_whole(value: str | None) -> int | None:
    """A whole number typed into a filter box, or None if it is blank or not one."""
    value = (value or "").strip()
    if not value.isascii() or not value.isdigit():
        return None
    return min(int(value), MAX_WHOLE_NUMBER)


def within(value, low, high) -> bool:
    """Whether a number or day is between low and high (either may be None)."""
    if low is None and high is None:
        return True
    if value is None:
        return False  # a row without this value can't be in a range
    return (low is None or value >= low) and (high is None or value <= high)


@dataclass(frozen=True)
class JobFilter:
    """
    Status and submitted dates are filtered by the coordinator. The rest (a search
    in the job's ID or file name, a range of task counts and of minutes taken) it
    can't filter, so matches() applies them to the jobs the page loaded.
    """

    statuses: list[str] = field(default_factory=list)
    start: date | None = None
    end: date | None = None
    text: str = ""
    tasks_min: int | None = None
    tasks_max: int | None = None
    took_min: int | None = None  # minutes
    took_max: int | None = None

    @property
    def in_app(self) -> bool:
        """Whether any filter the coordinator can't apply is set."""
        ranges = (self.tasks_min, self.tasks_max, self.took_min, self.took_max)
        return bool(self.text) or any(v is not None for v in ranges)

    @property
    def active(self) -> bool:
        return bool(self.statuses or self.start or self.end or self.in_app)

    def matches(self, row: dict) -> bool:
        """Whether a job list row passes the filters the coordinator can't apply."""
        if self.text:
            names = f"{row['id']} {row.get('filename') or ''} {row['job_type']}"
            if self.text.lower() not in names.lower():
                return False
        if not within(row.get("task_count"), self.tasks_min, self.tasks_max):
            return False
        seconds = row.get("took_seconds")
        minutes = None if seconds is None else seconds // 60
        return within(minutes, self.took_min, self.took_max)

    def query(self) -> str:
        """The filter as a query string, without the page offset."""
        pairs = [("status", s) for s in self.statuses]
        pairs += [
            (name, value)
            for name, value in (
                ("from", self.start.isoformat() if self.start else None),
                ("to", self.end.isoformat() if self.end else None),
                ("q", self.text or None),
                ("tasks_min", self.tasks_min),
                ("tasks_max", self.tasks_max),
                ("took_min", self.took_min),
                ("took_max", self.took_max),
            )
            if value is not None
        ]
        return urlencode(pairs)
