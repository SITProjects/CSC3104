"""
Filters for the admin tables: one dropdown in each column header.

A page lists its columns (Column), makes a TableFilter from the request's query
string, and calls matches() on each row. The coordinator can't filter these tables,
so the page loads the newest rows it returns at once and filters them itself.
"""

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlencode

from services.web.admin import SGT
from services.web.jobfilter import MAX_SEARCH_CHARS, parse_whole, within


@dataclass(frozen=True)
class Column:
    """
    One table column. kind says what its dropdown asks for: "text" (a search),
    "choice" (tick some of choices), "range" (a minimum and maximum) or "date"
    (a from and to day, Singapore time); None means the column has no filter.
    get reads the column's value from a row: a string for text and choice, a number
    for range, a time or ISO timestamp for date.
    """

    key: str  # the column's name in the query string
    label: str
    kind: str | None = None
    get: Callable[[dict], Any] = lambda row: None
    choices: Mapping[str, str] = field(default_factory=dict)  # value -> label
    decimal: bool = False  # a range of amounts like 0.25, not of whole numbers
    note: str = ""  # a line under the boxes, e.g. "In minutes."
    search_label: str = "Search"
    align: str = "left"  # which side of the header the dropdown lines up with
    hidden_label: bool = False  # a column with no visible title, e.g. "Logs"


def parse_amount(value: str | None) -> float | None:
    """A non-negative number typed into a filter box, or None if it isn't one."""
    try:
        number = float((value or "").strip())
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def sg_day(value: str | datetime | None) -> date | None:
    """The Singapore day of a time or ISO timestamp (a naive time is UTC)."""
    if not value:
        return None
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(SGT).date()


def _bounds(col: Column):
    """How a range or date column's two boxes are read, and named in the query."""
    if col.kind == "range":
        return (parse_amount if col.decimal else parse_whole), "_min", "_max"
    return parse_day, "_from", "_to"


class TableFilter:
    """What the user asked to see in a table, read from the request's query string."""

    def __init__(self, columns: tuple[Column, ...], params):
        """params is the request's query_params (anything with get and getlist)."""
        self.columns = columns
        self.values = {c.key: self._read(c, params) for c in columns if c.kind}

    @staticmethod
    def _read(col: Column, params):
        """A column's filter: a str, a list of choices, or a (low, high) pair."""
        if col.kind == "text":
            return (params.get(col.key) or "").strip()[:MAX_SEARCH_CHARS]
        if col.kind == "choice":
            chosen = params.getlist(col.key)
            return [value for value in col.choices if value in chosen]
        read, low, high = _bounds(col)
        return read(params.get(col.key + low)), read(params.get(col.key + high))

    def is_active(self, col: Column) -> bool:
        value = self.values.get(col.key)
        if isinstance(value, tuple):
            return any(v is not None for v in value)
        return bool(value)

    @property
    def active(self) -> bool:
        return any(self.is_active(c) for c in self.columns)

    def matches(self, row: dict) -> bool:
        return all(
            self._passes(col, self.values[col.key], row)
            for col in self.columns
            if self.is_active(col)
        )

    @staticmethod
    def _passes(col: Column, value, row: dict) -> bool:
        got = col.get(row)
        if col.kind == "text":
            return value.lower() in str(got or "").lower()
        if col.kind == "choice":
            return got in value
        if col.kind == "date":
            got = sg_day(got)
        return within(got, *value)

    def query(self) -> str:
        """The filter as a query string, without the page offset."""
        pairs: list[tuple[str, Any]] = []
        for col in self.columns:
            if not self.is_active(col):
                continue
            value = self.values[col.key]
            if col.kind == "text":
                pairs.append((col.key, value))
            elif col.kind == "choice":
                pairs += [(col.key, v) for v in value]
            else:
                _, low, high = _bounds(col)
                pairs += [
                    (col.key + suffix, v.isoformat() if col.kind == "date" else v)
                    for suffix, v in zip((low, high), value, strict=True)
                    if v is not None
                ]
        return urlencode(pairs)


def page_of(rows: list, offset: int, size: int) -> tuple[list, bool]:
    """One page of rows, and whether there are more after it."""
    return rows[offset : offset + size], len(rows) > offset + size
