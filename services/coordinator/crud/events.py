from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import Event


async def create_event(
    session: AsyncSession,
    event_type: str,
    job_id: UUID,
    task_id: int,
    node_name: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """
    Records an event in the events table, e.g. a replica eviction.

    args:
        session: The database session for this request.
        event_type: What happened, e.g. "replica_evicted".
        job_id: The job it concerns.
        task_id: The task it concerns.
        node_name: The node involved, if known.
        detail: Extra information, stored in the JSONB detail column.
    """
    event = Event(
        type=event_type,
        job_id=job_id,
        task_id=task_id,
        node_name=node_name,
        detail=detail,
    )
    session.add(event)
    await session.flush()


async def list_events(
    session: AsyncSession,
    job_id: UUID | None,
    node_name: str | None,
    limit: int,
    offset: int,
) -> list[Event]:
    """
    Returns events, newest first.

    args:
        session: The database session for this request.
        job_id: Only this job's events, or None for any job.
        node_name: Only this node's events, or None for any node.
        limit: The most events to return.
        offset: How many of the newest events to skip.
    """
    stmt = select(Event).order_by(Event.time.desc(), Event.id.desc())
    if job_id is not None:
        stmt = stmt.where(Event.job_id == job_id)
    if node_name is not None:
        stmt = stmt.where(Event.node_name == node_name)
    return list((await session.scalars(stmt.limit(limit).offset(offset))).all())
