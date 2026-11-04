"""The columns of the admin tables that have a filter in their header."""

from services.web.jobfilter import JOB_STATUSES
from services.web.tablefilter import Column


def _amount(value) -> float | None:
    return None if value is None else float(value)


USER_COLUMNS = (
    Column("id", "Account ID", "text", lambda u: str(u["id"]), search_label="ID"),
    Column("email", "Email", "text", lambda u: u["email"], search_label="Email"),
    Column(
        "role",
        "Role",
        "choice",
        lambda u: u["role"],
        choices={"ADMIN": "Admin", "USER": "User"},
    ),
    Column("jobs", "Jobs", "range", lambda u: u["job_count"]),
    Column("joined", "Joined", "date", lambda u: u["created_at"], align="right"),
)

JOB_COLUMNS = (
    Column(
        "job",
        "Job",
        "text",
        lambda j: f"{j['id']} {j['job_type']}",
        search_label="ID or type",
    ),
    Column(
        "owner", "Owner", "text", lambda j: str(j["owner_id"]), search_label="Owner ID"
    ),
    Column("status", "Status", "choice", lambda j: j["status"], choices=JOB_STATUSES),
    Column("tasks", "Tasks", "range", lambda j: j["task_count"]),
    Column("submitted", "Submitted", "date", lambda j: j["created_at"]),
    Column(
        "took",
        "Time taken",
        "range",
        lambda j: None if j["took_seconds"] is None else j["took_seconds"] // 60,
        note="In minutes.",
    ),
    Column(
        "cost",
        "Burst cost (USD)",
        "range",
        lambda j: _amount(j["cost"]["usd"]) if j["cost"] else None,
        decimal=True,
        note="In USD. Jobs with no burst cost are left out.",
        align="right",
    ),
    Column("logs", "Logs", hidden_label=True),
)

_USD = "In USD."
BURST_COLUMNS = (
    Column(
        "worker",
        "Worker / instance",
        "text",
        lambda m: f"{m.get('node_name') or ''} {m['instance_id']}",
        search_label="Node name or instance ID",
    ),
    Column(
        "type",
        "Type / market",
        "text",
        lambda m: f"{m['machine']} {m.get('market') or ''}",
        search_label="Machine type or market",
    ),
    Column(
        "state",
        "State / trust",
        "choice",
        lambda m: "active" if m["running"] else "ended",
        choices={"active": "Active", "ended": "Ended"},
    ),
    Column(
        "launched",
        "Lifetime",
        "date",
        lambda m: m["launched_at"],
        note="The day it launched, Singapore time.",
    ),
    Column(
        "rate",
        "Rate / hour",
        "range",
        lambda m: _amount(m["usd_per_hour"]),
        decimal=True,
        note=_USD,
    ),
    Column(
        "total",
        "Total",
        "range",
        lambda m: _amount(m["total_usd"]),
        decimal=True,
        note=_USD,
    ),
    Column(
        "jobs",
        "Jobs",
        "range",
        lambda m: _amount(m["attributed_usd"]),
        decimal=True,
        note=_USD,
    ),
    Column(
        "unattributed",
        "Unattributed",
        "range",
        lambda m: _amount(m["unattributed_usd"]),
        decimal=True,
        note=_USD,
        align="right",
    ),
)
