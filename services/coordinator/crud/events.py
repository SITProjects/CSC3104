from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import Event


async def create_event(
    session: AsyncSession,
    event_type: str,
    job_id: UUID,
    task_id: int,
    node_name: str | None = None,
    details: str | None = None,
) -> None:
    """
    Creates an event for a given replica.
    """
    # Assuming you have an Event model defined in your database
    event = Event(
        type=event_type,
        job_id=job_id,
        task_id=task_id,
        node_name=node_name,
        detail=details,
    )
    session.add(event)
    await session.flush()
