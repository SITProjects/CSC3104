from typing import Any
from uuid import UUID

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
        details: Extra information, stored in the JSONB detail column.
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
