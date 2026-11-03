"""Pure helpers for the admin dashboard: shaping the coordinator's nodes and
events for the page. Nothing here talks to the network, so it is easy to test.
"""

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

# The site shows every time in Singapore time, which has no daylight saving.
SGT = timezone(timedelta(hours=8), "SGT")

NODE_STATES = ("IDLE", "BUSY", "OFFLINE")

# detail keys worth showing in the event log, in this order, with their labels
EVENT_DETAILS = (
    ("replica_id", "replica"),
    ("attempt", "attempt"),
    ("exit_code", "exit"),
    ("worker", "worker"),
    ("by", "by"),
)
# Warn this long before a worker's heartbeat token runs out
HEARTBEAT_WARN = timedelta(days=30)


def sg_time(when: str | datetime | None, fmt: str = "%d %b %Y %H:%M SGT") -> str:
    """
    A time or ISO timestamp in Singapore time, by default '25 Oct 2026 19:00 SGT'.
    A naive time is taken as UTC. '' if there is none.
    """
    if not when:
        return ""
    then = datetime.fromisoformat(when) if isinstance(when, str) else when
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return then.astimezone(SGT).strftime(fmt)


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
        "time": sg_time(event["time"], "%d %b %H:%M:%S"),
        # "replica_evicted" -> "evicted", shown as a status badge
        "kind": event["type"].removeprefix("replica_").replace("_", " "),
        "message": detail.get("message") or "",
        "facts": [
            f"{label} {detail[key]}"
            for key, label in EVENT_DETAILS
            if detail.get(key) is not None
        ],
    }


def worker_row(worker: dict[str, Any], now: datetime) -> dict[str, Any]:
    """
    One campus worker, as the workers pages show it: where it is in joining,
    and what an admin should do next.
    """
    joined = worker["joined"]
    if joined is None:
        status = "Unknown"  # the cluster could not be asked
    elif not joined:
        status = "Waiting to join"
    else:
        # IDLE, BUSY or OFFLINE once its node agent sends heartbeats
        status = worker.get("state") or "No heartbeats"
    join_expires = worker.get("join_token_expires_at")
    join_expired = bool(join_expires) and datetime.fromisoformat(join_expires) <= now
    beat_expires = worker.get("heartbeat_token_expires_at")
    beat_soon = (
        bool(beat_expires)
        and datetime.fromisoformat(beat_expires) - now <= HEARTBEAT_WARN
    )
    return {
        **worker,
        "status": status,
        "heartbeat_ago": ago(worker.get("last_heartbeat"), now) or "never",
        "join_token": (
            None
            if joined
            else "Expired"
            if join_expired or not join_expires
            else f"Valid until {sg_time(join_expires)}"
        ),
        "join_expired": not joined and (join_expired or not join_expires),
        "needs_credentials": bool(joined) and not beat_expires,
        "heartbeat_until": sg_time(beat_expires, "%d %b %Y"),
        "heartbeat_soon": beat_soon,
    }
