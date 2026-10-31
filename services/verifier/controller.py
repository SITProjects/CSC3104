import logging
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.coordinator.crud import create_event, create_replicas
from shared.db.models import (
    Job,
    JobStatus,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
)

from .pc_tracking import get_dispute_exclusions
from .spot_check import accept_on_trust, reopen_trusted_results
from .trust import score_replicas
from .voting import MAX_REPLICAS, winning_hash

log = logging.getLogger("verifier")


async def verify_next_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> int | None:
    """
    Votes on one finished task and saves the decision in a single transaction.

    Locks a VOTING task with FOR UPDATE SKIP LOCKED, so concurrent verifiers
    cannot process the same task. Only active jobs are eligible. The coordinator
    sets VOTING once replicas have finished. A majority verifies the task and
    stores final_hash. A single copy is accepted only from a trusted PC that
    was not picked for a spot check, otherwise it gets one more copy (see
    spot_check.py).
    An unresolved task gets one extra SCHEDULED replica if it has two, otherwise
    two, without exceeding five total. No majority at five fails the task. PCs
    that lose a vote have their results accepted on trust reopened.

    All configured initial counts from one to five are supported. Failed replicas
    count towards the total but do not contribute votes. A task_disputed event
    stores the extra replica ids and excluded_nodes, including previous retry
    PCs from eviction events. Invalid replica counts and unrecoverable PC data
    fail only the affected task, with a recorded reason. A majority is verified
    even if some nodes cannot be scored. Missing PC history fails an unresolved
    task immediately because fresh-PC placement cannot be guaranteed.
    Actual PC placement is a later integration.

    args:
        session_factory: The shared async database session factory.
    returns:
        int | None: The processed task's id, or None if no task can be processed.
    """
    async with session_factory.begin() as session:
        task = await next_voting_task(session)
        if task is None:
            return None
        await process_vote(session, task)
        return task.id


async def process_vote(session: AsyncSession, task: Task) -> None:
    """
    Applies a majority vote or records a dispute or terminal data error.

    args:
        session: The verifier's current transaction.
        task: The locked VOTING task with no unfinished replicas.
    """
    replicas = await get_replicas(session, task.id)
    replica_count = len(replicas)
    if not 1 <= replica_count <= MAX_REPLICAS:
        await fail_verification(session, task, "invalid_replica_count", replica_count)
        return

    # The coordinator validates the hash before marking a replica DONE.
    successful = [
        replica
        for replica in replicas
        if replica.status == ReplicaStatus.DONE and replica.exit_code == 0
    ]
    winner = winning_hash(
        [replica.result_hash for replica in successful if replica.result_hash],
        replica_count,
    )

    # One copy is only enough from a trusted PC that wasn't picked for a check.
    # It earns that PC no trust, because nothing was compared.
    if winner and replica_count == 1:
        if await accept_on_trust(session, successful[0]):
            task.status = TaskStatus.VERIFIED
            task.final_hash = winner
            return
        extra, event = 1, "task_spot_check"
    elif winner:
        await score_replicas(session, successful, winner)
        task.status = TaskStatus.VERIFIED
        task.final_hash = winner
        # PCs that lost the vote: re-check what they got accepted on trust.
        losers = {r.node_name for r in successful if r.result_hash != winner}
        losers.discard(None)
        if losers:
            await reopen_trusted_results(session, sorted(losers))
        return
    elif replica_count == MAX_REPLICAS:
        task.status = TaskStatus.FAILED
        task.final_hash = None
        return
    else:
        # Two copies that disagree get one tie-breaker first, so three PCs are
        # enough to settle one bad result. Later disputes add two.
        extra = 1 if replica_count == 2 else min(2, MAX_REPLICAS - replica_count)
        event = "task_disputed"

    try:
        excluded_nodes = await get_dispute_exclusions(session, task, replicas)
    except ValueError:
        await fail_verification(session, task, "missing_pc_assignment", replica_count)
        return
    await handle_dispute(session, task, extra, excluded_nodes, event)


async def next_voting_task(session: AsyncSession) -> Task | None:
    """
    Fetches and locks the oldest eligible voting task.

    Skips locked tasks and inactive jobs. Relies on the coordinator setting
    VOTING only after replicas have finished. The caller owns the transaction
    and keeps the task lock while voting.

    args:
        session: The verifier's current transaction.
    returns:
        Task | None: The next locked task, or None if none is eligible.
    """
    stmt = (
        select(Task)
        .join(Job, Task.job_id == Job.id)
        .where(
            Task.status == TaskStatus.VOTING,
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
        )
        .order_by(Task.id)
        .limit(1)
        .with_for_update(of=Task, skip_locked=True)
    )
    return await session.scalar(stmt)


async def get_replicas(session: AsyncSession, task_id: int) -> Sequence[Replica]:
    """
    Fetches all replica records for a task, including failed replicas.

    args:
        session: The verifier's current transaction.
        task_id: The task whose replicas are needed.
    returns:
        Sequence[Replica]: All replicas used to determine the vote and replica limit.
    """
    return (
        await session.scalars(select(Replica).where(Replica.task_id == task_id))
    ).all()


async def handle_dispute(
    session: AsyncSession,
    task: Task,
    extra: int,
    excluded_nodes: list[str],
    event: str = "task_disputed",
) -> None:
    """
    Marks a task disputed, creates extra replicas and records their exclusions.

    Used for a vote with no majority (task_disputed) and for a single copy that
    needs a second one (task_spot_check). Flushes to obtain replica ids for the
    event; the caller owns and commits the transaction.

    args:
        session: The verifier's current transaction.
        task: The locked task that needs more copies.
        extra: How many copies to add, keeping the total at MAX_REPLICAS or less.
        excluded_nodes: Previously used PCs excluded from the extra replicas.
        event: The event type to record.
    """
    task.status = TaskStatus.DISPUTED
    task.final_hash = None
    extra_replicas = await create_replicas(session, [task], extra)
    await create_event(
        session,
        event,
        task.job_id,
        task.id,
        detail={
            "replica_ids": [replica.id for replica in extra_replicas],
            "excluded_nodes": excluded_nodes,
        },
    )


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
    await create_event(
        session,
        "task_verification_failed",
        task.job_id,
        task.id,
        detail={"reason": reason, "replica_count": replica_count},
    )
    log.warning(
        "Task %s failed verification: %s (replicas=%s)", task.id, reason, replica_count
    )
