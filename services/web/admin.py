"""Pure helpers for the admin dashboard: shaping the coordinator's nodes and
events for the page. Nothing here talks to the network, so it is easy to test.
"""

from datetime import UTC, datetime
from typing import Any

NODE_STATES = ("IDLE", "BUSY", "OFFLINE")

# detail keys worth showing in the event log, in this order, with their labels
EVENT_DETAILS = (
    ("replica_id", "replica"),
    ("attempt", "attempt"),
    ("exit_code", "exit"),
)


def utc_time(when: str | None) -> str:
    """'25 Oct 2026 11:00 UTC' for an ISO timestamp (naive means UTC); '' if None."""
    if not when:
        return ""
    then = datetime.fromisoformat(when)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return then.astimezone(UTC).strftime("%d %b %Y %H:%M UTC")


def ago(when: str | None, now: datetime) -> str:
    """'12s ago', '5m ago', '3h ago' or '2d ago' for an ISO timestamp; '' if None."""
    if not when:
        return ""
    then = datetime.fromisoformat(when)
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    seconds = max(int((now - then).total_seconds()), 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def node_row(node: dict[str, Any], now: datetime) -> dict[str, Any]:
    """One worker node, as the nodes table shows it."""
    votes = node["total_agree"] + node["total_disagree"]
    return {
        **node,
        "heartbeat_ago": ago(node["last_heartbeat"], now) or "never",
        "agree_rate": round(100 * node["total_agree"] / votes) if votes else None,
    }


def node_counts(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """How many nodes are in each state, every state present even if zero."""
    counts = dict.fromkeys(NODE_STATES, 0)
    for node in nodes:
        counts[node["state"]] = counts.get(node["state"], 0) + 1
    return counts


def event_row(event: dict[str, Any]) -> dict[str, Any]:
    """One event, as the log shows it: its kind, message and key details."""
    detail = event["detail"] or {}
    return {
        **event,
        "time": datetime.fromisoformat(event["time"]).strftime("%d %b %H:%M:%S"),
        # "replica_evicted" -> "evicted", shown as a status badge
        "kind": event["type"].removeprefix("replica_").replace("_", " "),
        "message": detail.get("message") or "",
        "facts": [
            f"{label} {detail[key]}"
            for key, label in EVENT_DETAILS
            if detail.get(key) is not None
        ],
    }
