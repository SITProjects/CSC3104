import logging
from collections.abc import Sequence
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Event, Replica, Task

from .voting import dispute_exclusions

log = logging.getLogger("verifier")
PC_ASSIGNMENT_TIMEOUT_SECONDS = 300


async def get_dispute_exclusions(
    session: AsyncSession, task: Task, replicas: Sequence[Replica]
) -> list[str]:
    """
    Collects current and historical PCs to exclude from extra replicas.

    Eviction and loss events preserve earlier assignments. An event for the exact
    replica and attempt can also recover a cleared final PC assignment.

    Missing current or historical PC names prevent creating extra replicas,
    because previously used PCs cannot be safely excluded. The verifier waits
    up to five minutes for the missing information before failing the task.

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
    assignments = {
        (replica.id, replica.attempt): replica.node_name for replica in replicas
    }
    historical_nodes = []
    for event in history_events:
        # Event details retain the PC name even if the node record is deleted.
        detail = event.detail or {}
        name = detail.get("node_name")
        name = name if isinstance(name, str) else event.node_name
        historical_nodes.append(name)
        key = (detail.get("replica_id"), detail.get("attempt"))
        # Retries may use different PCs, restore only the matching replica and attempt.
        if (
            isinstance(key[0], int)
            and isinstance(key[1], int)
            and key in assignments
            and assignments[key] is None
        ):
            assignments[key] = name
    previous_nodes = list(assignments.values()) + historical_nodes
    return dispute_exclusions(previous_nodes)


async def pc_wait_expired(
    session: AsyncSession, task: Task, replicas: Sequence[Replica]
) -> bool:
    """
    Records the first missing PC data and checks whether its grace period expired.

    The first waiting event stores the database time, so polls and restarts do
    not reset the timer. Replica ids identify the voting round: extra replicas
    in a later round get a separate grace period. The caller holds the task lock,
    preventing concurrent verifiers from creating duplicate waiting events.

    returns:
        bool: True if the deadline expired, False if it is still waiting.
    """
    replica_ids = sorted(replica.id for replica in replicas)
    waiting = await session.scalar(
        select(Event)
        .where(
            Event.task_id == task.id,
            Event.type == "task_pc_assignment_waiting",
            Event.detail["replica_ids"] == replica_ids,
        )
        .order_by(Event.id)
        .limit(1)
    )
    # Use the database clock for both timestamps, regardless of verifier host.
    now = await session.scalar(select(func.clock_timestamp()))
    if waiting is None:
        session.add(
            Event(
                type="task_pc_assignment_waiting",
                job_id=task.job_id,
                task_id=task.id,
                time=now,
                detail={"replica_ids": replica_ids},
            )
        )
        await session.flush()
        log.warning("Task %s is waiting for missing PC assignments", task.id)
        return False

    deadline = waiting.time + timedelta(seconds=PC_ASSIGNMENT_TIMEOUT_SECONDS)
    return now >= deadline
