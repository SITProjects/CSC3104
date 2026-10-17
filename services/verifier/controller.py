import logging
import re
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.models import (
    Event,
    Job,
    JobStatus,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
)

from .voting import dispute_exclusions, winning_hash

log = logging.getLogger("verifier")


async def verify_next_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> int | None:
    """
    Votes on one finished task and saves the decision in a single transaction.

    Locks a VOTING task with FOR UPDATE SKIP LOCKED, so concurrent verifiers
    cannot process the same task. Only active jobs whose replicas are all DONE
    or FAILED are eligible. A majority verifies the task and stores final_hash.
    An unresolved task gets two extra SCHEDULED replicas, or one if four already
    exist, without exceeding five total. No majority at five fails the task.

    All configured initial counts from one to five are supported. Failed replicas
    count towards the total but do not contribute votes. A task_disputed event
    stores the extra replica ids and excluded_nodes, including previous retry
    PCs from eviction events. Tasks missing PC assignments are left unchanged
    while other tasks are checked. This records a scheduling request; actual
    PC placement is a later integration.

    args:
        session_factory: The shared async database session factory.
    returns:
        int | None: The processed task's id, or None if no task can be processed.
    raises:
        ValueError: If the task has fewer than one or more than five replicas.
    """
    unfinished = (
        select(Replica.id)
        .where(
            Replica.task_id == Task.id,
            Replica.status.not_in([ReplicaStatus.DONE, ReplicaStatus.FAILED]),
        )
        .correlate(Task)
        .exists()
    )
    stmt = (
        select(Task)
        .join(Job, Task.job_id == Job.id)
        .where(
            Task.status == TaskStatus.VOTING,
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            ~unfinished,
        )
        .order_by(Task.id)
        .limit(1)
        .with_for_update(of=Task, skip_locked=True)
    )

    async with session_factory.begin() as session:
        after_id = 0
        while True:
            task = await session.scalar(stmt.where(Task.id > after_id))
            if task is None:
                return None
            if await vote_task(session, task):
                return task.id
            after_id = task.id


async def vote_task(session: AsyncSession, task: Task) -> bool:
    """
    Applies a vote to a locked task, leaving incomplete PC data for a later poll.

    args:
        session: The verifier's current transaction.
        task: The locked VOTING task with no unfinished replicas.
    returns:
        bool: True when a decision was saved, False when PC data is incomplete.
    """
    replicas = (
        await session.scalars(select(Replica).where(Replica.task_id == task.id))
    ).all()
    hashes = [
        replica.result_hash
        for replica in replicas
        if replica.status == ReplicaStatus.DONE
        and replica.exit_code == 0
        and replica.result_hash is not None
        and re.fullmatch(r"[0-9a-f]{64}", replica.result_hash)
    ]
    winner = winning_hash(hashes, len(replicas))

    if winner is not None:
        task.status = TaskStatus.VERIFIED
    elif len(replicas) == 5:
        task.status = TaskStatus.FAILED
    else:
        try:
            excluded_nodes = await get_dispute_exclusions(session, task, replicas)
        except ValueError:
            log.warning("Task %s is waiting for complete PC assignments", task.id)
            return False

        task.status = TaskStatus.DISPUTED
        extra_replicas = [
            Replica(task_id=task.id, status=ReplicaStatus.SCHEDULED, attempt=1)
            for _ in range(min(2, 5 - len(replicas)))
        ]
        session.add_all(extra_replicas)
        await session.flush()
        session.add(
            Event(
                type="task_disputed",
                job_id=task.job_id,
                task_id=task.id,
                detail={
                    "replica_ids": [replica.id for replica in extra_replicas],
                    "excluded_nodes": excluded_nodes,
                },
            )
        )

    task.final_hash = winner
    await session.flush()
    return True


async def get_dispute_exclusions(
    session: AsyncSession, task: Task, replicas: Sequence[Replica]
) -> list[str]:
    """
    Collects current and historical PCs to exclude from extra replicas.

    Eviction events preserve earlier assignments. An event for the exact
    replica and attempt can also recover a cleared final PC assignment.

    args:
        session: The verifier's current transaction.
        task: The task whose PC history is needed.
        replicas: The task's replicas, already loaded by vote_task.
    returns:
        list[str]: Distinct previous PC names in sorted order.
    raises:
        ValueError: If any previous PC assignment is missing.
    """
    eviction_events = (
        await session.scalars(
            select(Event).where(
                Event.task_id == task.id,
                Event.type == "replica_evicted",
            )
        )
    ).all()
    assignments = {
        (replica.id, replica.attempt): replica.node_name for replica in replicas
    }
    historical_nodes = []
    for event in eviction_events:
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
