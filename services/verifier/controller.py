import logging
import re

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

from .pc_tracking import get_dispute_exclusions, pc_wait_expired
from .trust import score_replicas
from .voting import winning_hash

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
    PCs from eviction events. Invalid replica counts and unrecoverable PC data
    fail only the affected task, with a recorded reason. Missing PC metadata
    gets a bounded wait recorded in the database before failing the task.
    Waiting tasks allow later tasks to proceed. Actual PC placement is a later
    integration.

    args:
        session_factory: The shared async database session factory.
    returns:
        int | None: The processed task's id, or None if no task can be processed.
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
    Applies a vote, waits for PC metadata or records a terminal data error.

    args:
        session: The verifier's current transaction.
        task: The locked VOTING task with no unfinished replicas.
    returns:
        bool: True after saving an outcome, False while waiting for PC metadata.
    """
    if task.status != TaskStatus.VOTING:
        return True

    replicas = (
        await session.scalars(select(Replica).where(Replica.task_id == task.id))
    ).all()
    replica_count = len(replicas)
    if not 1 <= replica_count <= 5:
        await fail_verification(session, task, "invalid_replica_count", replica_count)
        return True

    successful = [
        replica
        for replica in replicas
        if replica.status == ReplicaStatus.DONE
        and replica.exit_code == 0
        and replica.result_hash is not None
        and re.fullmatch(r"[0-9a-f]{64}", replica.result_hash)
    ]
    winner = winning_hash(
        [replica.result_hash for replica in successful], replica_count
    )

    if winner is not None:
        try:
            await score_replicas(session, successful, winner)
        except ValueError:
            if not await pc_wait_expired(session, task, replicas):
                return False
            await fail_verification(
                session, task, "missing_pc_assignment", replica_count
            )
            return True
        task.status = TaskStatus.VERIFIED
    elif replica_count == 5:
        task.status = TaskStatus.FAILED
    else:
        try:
            excluded_nodes = await get_dispute_exclusions(session, task, replicas)
        except ValueError:
            if not await pc_wait_expired(session, task, replicas):
                return False
            await fail_verification(
                session, task, "missing_pc_assignment", replica_count
            )
            return True

        task.status = TaskStatus.DISPUTED
        extra_replicas = [
            Replica(task_id=task.id, status=ReplicaStatus.SCHEDULED, attempt=1)
            for _ in range(min(2, 5 - replica_count))
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


async def fail_verification(
    session: AsyncSession, task: Task, reason: str, replica_count: int
) -> None:
    """
    Saves a task's verification data failure in the caller's transaction.

    Records the reason, clears any stale final hash and prevents repeated polls
    from retrying a task whose data cannot be used safely.

    args:
        session: The verifier's current transaction.
        task: The locked task that cannot be verified safely.
        reason: The data error, e.g. invalid_replica_count or missing_pc_assignment.
        replica_count: The number of replica records found for this task.
    """
    task.status = TaskStatus.FAILED
    task.final_hash = None
    session.add(
        Event(
            type="task_verification_failed",
            job_id=task.job_id,
            task_id=task.id,
            detail={"reason": reason, "replica_count": replica_count},
        )
    )
    await session.flush()
    log.warning(
        "Task %s failed verification: %s (replicas=%s)", task.id, reason, replica_count
    )
