import logging
import re
from collections.abc import Sequence
from enum import Enum, auto

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
MAX_REPLICAS = 5


class VoteOutcome(Enum):
    """Represents whether task verification is finished or deferred while waiting."""

    FINISHED = auto()
    WAITING = auto()


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
    async with session_factory.begin() as session:
        after_id = 0
        while True:
            task = await next_voting_task(session, after_id)
            if task is None:
                return None
            outcome = await process_vote(session, task)
            if outcome == VoteOutcome.FINISHED:
                return task.id
            after_id = task.id


async def process_vote(session: AsyncSession, task: Task) -> VoteOutcome:
    """
    Applies a vote, waits for PC metadata or records a terminal data error.

    args:
        session: The verifier's current transaction.
        task: The locked VOTING task with no unfinished replicas.
    returns:
        VoteOutcome.FINISHED: This verifier pass has reached/saved an outcome
            for the task; the task status records the business outcome.
        VoteOutcome.WAITING: Processing is deferred while waiting for missing
            PC metadata.
    """
    if task.status != TaskStatus.VOTING:
        return VoteOutcome.FINISHED

    replicas = await get_replicas(session, task.id)
    replica_count = len(replicas)
    if not 1 <= replica_count <= MAX_REPLICAS:
        await fail_verification(session, task, "invalid_replica_count", replica_count)
        return VoteOutcome.FINISHED

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

    if winner:
        try:
            await score_replicas(session, successful, winner)
        except ValueError:
            return await handle_missing_pc(session, task, replicas)
        task.status = TaskStatus.VERIFIED
    elif replica_count == MAX_REPLICAS:
        task.status = TaskStatus.FAILED
    else:
        try:
            excluded_nodes = await get_dispute_exclusions(session, task, replicas)
        except ValueError:
            return await handle_missing_pc(session, task, replicas)

        await handle_dispute(session, task, replica_count, excluded_nodes)

    task.final_hash = winner
    await session.flush()
    return VoteOutcome.FINISHED


async def next_voting_task(session: AsyncSession, after_id: int) -> Task | None:
    """
    Fetches and locks the next eligible voting task after the supplied id.

    Skips locked tasks and tasks with unfinished replicas or inactive jobs.
    The caller owns the transaction and keeps the task lock while voting.

    args:
        session: The verifier's current transaction.
        after_id: The last task id examined during this pass.
    returns:
        Task | None: The next locked task, or None if none is eligible.
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
    return await session.scalar(stmt.where(Task.id > after_id))


async def get_replicas(session: AsyncSession, task_id: int) -> list[Replica]:
    """
    Fetches all replica records for a task, including failed replicas.

    args:
        session: The verifier's current transaction.
        task_id: The task whose replicas are needed.
    returns:
        list[Replica]: All replicas used to determine the vote and replica limit.
    """
    return list(
        (await session.scalars(select(Replica).where(Replica.task_id == task_id))).all()
    )


async def handle_dispute(
    session: AsyncSession, task: Task, replica_count: int, excluded_nodes: list[str]
) -> None:
    """
    Marks a task disputed, creates extra replicas and records their exclusions.

    Adds up to two replicas without exceeding MAX_REPLICAS. Flushes to obtain
    replica ids for the event; the caller owns and commits the transaction.

    args:
        session: The verifier's current transaction.
        task: The locked task with no majority and room for more replicas.
        replica_count: The number of existing replicas, including failures.
        excluded_nodes: Previously used PCs excluded from the extra replicas.
    """
    task.status = TaskStatus.DISPUTED
    extra_replicas = [
        Replica(task_id=task.id, status=ReplicaStatus.SCHEDULED, attempt=1)
        for _ in range(min(2, MAX_REPLICAS - replica_count))
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


async def handle_missing_pc(
    session: AsyncSession, task: Task, replicas: Sequence[Replica]
) -> VoteOutcome:
    """
    Defers processing while PC metadata is missing, or fails after the deadline.

    Uses the existing bounded wait in the caller's transaction and task lock.

    args:
        session: The verifier's current transaction.
        task: The locked task with missing PC metadata.
        replicas: All replicas for the task's current voting round.
    returns:
        VoteOutcome.WAITING: The metadata grace period has not expired.
        VoteOutcome.FINISHED: The grace period expired and failure was recorded.
    """
    if not await pc_wait_expired(session, task, replicas):
        return VoteOutcome.WAITING
    await fail_verification(session, task, "missing_pc_assignment", len(replicas))
    return VoteOutcome.FINISHED


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
