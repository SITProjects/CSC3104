"""Burst workers and estimated cost per job for the web admin page (member 6).

Reads the cost views from migration 004. The web app calls this endpoint and
never reads the database itself. Prices are estimates in USD. An unknown price
stays null and never becomes zero.
"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.auth import AdminUser
from shared.db import session_manager

router = APIRouter(prefix="/admin", tags=["admin"])
DbSession = Annotated[AsyncSession, Depends(session_manager.get_session)]


class BurstMachineCost(BaseModel):
    instance_id: str
    slot: int
    node_name: str
    machine: str
    market: str
    usd_per_hour: Decimal | None
    launched_at: datetime
    ended_at: datetime | None
    running: bool
    total_usd: Decimal | None
    attributed_usd: Decimal | None
    unattributed_usd: Decimal | None
    has_estimated_runs: bool
    has_unknown_price: bool
    state: str | None
    trust_score: Decimal | None
    cpu_percent: float | None
    last_heartbeat: datetime | None


class BurstJobCost(BaseModel):
    job_id: UUID
    job_type: str
    status: str
    created_at: datetime
    runs: int
    usd: Decimal | None
    has_estimated_runs: bool
    has_unknown_price: bool


class BurstOverview(BaseModel):
    observed_at: datetime
    machine_count: int
    job_count: int
    machines: list[BurstMachineCost]
    jobs: list[BurstJobCost]


@router.get("/burst")
async def burst_overview(
    admin: AdminUser,
    session: DbSession,
    machine_limit: int = Query(20, ge=1, le=100),
    machine_offset: int = Query(0, ge=0),
    job_limit: int = Query(20, ge=1, le=100),
    job_offset: int = Query(0, ge=0),
) -> BurstOverview:
    """List recorded burst workers and estimated cost for every job, admins only.

    Workers still recorded as running come first, followed by recent history.
    Node names are reused per slot, so node data shows only for the instance
    now on the slot. Trust is reset when that instance is recorded. State, CPU
    and heartbeat show only once it has sent a heartbeat after its launch.
    Jobs with no observed burst runs have zero cost. Partly priced jobs keep
    their known subtotal and has_unknown_price. Fully unpriced jobs stay NULL.
    """
    async with session.begin():
        summary = (
            (
                await session.execute(
                    text(
                        "SELECT now() AS observed_at,"
                        " (SELECT count(*) FROM burst_machines) AS machine_count,"
                        " (SELECT count(*) FROM jobs) AS job_count"
                    )
                )
            )
            .mappings()
            .one()
        )
        machines = (
            (
                await session.execute(
                    text(
                        "SELECT m.*, n.trust_score,"
                        # The node row outlives its machine. Only a heartbeat
                        # sent after this machine launched describes it.
                        " CASE WHEN n.last_heartbeat >= m.launched_at THEN n.state END"
                        " AS state,"
                        " CASE WHEN n.last_heartbeat >= m.launched_at THEN n.cpu_percent END"
                        " AS cpu_percent,"
                        " CASE WHEN n.last_heartbeat >= m.launched_at THEN n.last_heartbeat END"
                        " AS last_heartbeat,"
                        " COALESCE(r.has_estimated_runs, false) AS has_estimated_runs,"
                        " (m.usd_per_hour IS NULL OR"
                        " COALESCE(r.has_unknown_price, false)) AS has_unknown_price"
                        " FROM burst_machine_cost m"
                        " LEFT JOIN burst_slots s ON s.instance_id = m.instance_id"
                        " AND s.node_name = m.node_name AND m.running"
                        " LEFT JOIN nodes n ON n.name = s.node_name"
                        " LEFT JOIN ("
                        "   SELECT instance_id, bool_or(estimated) AS has_estimated_runs,"
                        "   bool_or(usd IS NULL) AS has_unknown_price"
                        "   FROM burst_run_cost GROUP BY instance_id"
                        " ) r ON r.instance_id = m.instance_id"
                        " ORDER BY m.running DESC, m.launched_at DESC, m.instance_id"
                        " LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": machine_limit, "offset": machine_offset},
                )
            )
            .mappings()
            .all()
        )
        jobs = (
            (
                await session.execute(
                    text(
                        "SELECT j.id AS job_id, j.job_type, j.status, j.created_at,"
                        " COALESCE(c.runs, 0) AS runs,"
                        " CASE WHEN c.job_id IS NULL THEN 0 ELSE c.usd END AS usd,"
                        " COALESCE(c.has_estimated_runs, false) AS has_estimated_runs,"
                        " COALESCE(c.has_unknown_price, false) AS has_unknown_price"
                        " FROM jobs j LEFT JOIN burst_job_cost c ON c.job_id = j.id"
                        " ORDER BY j.created_at DESC, j.id"
                        " LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": job_limit, "offset": job_offset},
                )
            )
            .mappings()
            .all()
        )
    return BurstOverview(
        **summary,
        machines=[BurstMachineCost.model_validate(row) for row in machines],
        jobs=[BurstJobCost.model_validate(row) for row in jobs],
    )
