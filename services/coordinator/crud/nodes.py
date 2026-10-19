from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Node, NodeState


async def mark_offline(session: AsyncSession, older_than: datetime) -> None:
    """Mark stale heartbeats offline without overwriting a concurrent heartbeat."""
    await session.execute(
        update(Node)
        .where(Node.last_heartbeat < older_than, Node.state != NodeState.OFFLINE)
        .values(state=NodeState.OFFLINE)
    )


async def ensure_node(session: AsyncSession, node_name: str) -> None:
    """
    Ensures that a node exists in the database.

    args:
        session: The database session for this request.
        node_name: The name of the node to ensure exists.
    """
    await session.execute(
        insert(Node)
        .values(name=node_name)
        .on_conflict_do_nothing(index_elements=[Node.name])
    )


async def update_node_state(
    session: AsyncSession, node_name: str, state: str, cpu_percent: float
) -> None:
    """
    Updates the state of a node in the database.

    args:
        session: The database session for this request.
        node_name: The name of the node to update.
        state: The new state of the node.
        cpu_percent: Whole-machine CPU use the node reported.
    """
    now = datetime.now(UTC)
    await session.execute(
        insert(Node)
        .values(
            name=node_name,
            state=state,
            last_heartbeat=now,
            cpu_percent=cpu_percent,
        )
        .on_conflict_do_update(
            index_elements=[Node.name],
            set_={"state": state, "last_heartbeat": now, "cpu_percent": cpu_percent},
        )
    )
