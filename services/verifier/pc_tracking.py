from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Event, Replica, Task

from .voting import dispute_exclusions


async def get_dispute_exclusions(
    session: AsyncSession, task: Task, replicas: Sequence[Replica]
) -> list[str]:
    """
    Collects current and historical PCs to exclude from extra replicas.

    Eviction and loss events preserve earlier assignments. An event for the exact
    replica and attempt can also recover a cleared final PC assignment.

    Missing current or historical PC names prevent creating extra replicas,
    because previously used PCs cannot be safely excluded. The verifier fails
    the unresolved task immediately when this information is unavailable.

    args:
        session: The verifier's current transaction.
        task: The task whose PC history is needed.
        replicas: The task's replicas, already loaded by process_vote.
    returns:
        list[str]: Distinct previous PC names in sorted order.
    raises:
        ValueError: If any current or historical PC assignment is unknown.
    """
    history_events = (
        await session.scalars(
            select(Event).where(
                Event.task_id == task.id,
                Event.type.in_(["replica_evicted", "replica_lost"]),
            )
        )
    ).all()
    previous_nodes = []
    covered: set[tuple[int, int]] = set()
    for event in history_events:
        # Event details retain the PC name even if the node record is deleted.
        detail = event.detail if isinstance(event.detail, dict) else {}
        previous_nodes.append(detail.get("node_name"))
        replica_id, attempt = detail.get("replica_id"), detail.get("attempt")
        if isinstance(replica_id, int) and isinstance(attempt, int):
            covered.add((replica_id, attempt))

    for replica in replicas:
        # Only history for this exact attempt can cover a cleared assignment.
        if replica.node_name is None and (replica.id, replica.attempt) in covered:
            continue
        previous_nodes.append(replica.node_name)
    return dispute_exclusions(previous_nodes)
