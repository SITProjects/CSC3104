import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.db.models import Job, JobStatus, Replica, ReplicaStatus, Task, TaskStatus

from .voting import winning_hash


async def verify_next_task(
    session_factory: async_sessionmaker[AsyncSession],
) -> int | None:
    """
    Votes on one finished task and saves the decision in a single transaction.

    Locks a VOTING task with FOR UPDATE SKIP LOCKED, so concurrent verifiers
    cannot process the same task. Only active jobs whose replicas are all DONE
    or FAILED are eligible. A majority verifies the task and stores final_hash.
    An unresolved task gets up to two extra SCHEDULED replicas, capped at five;
    without a majority at five it fails. The coordinator launches the replicas.

    Normal tasks expand from three to five replicas. Jobs configured with fewer
    replicas follow the same strict-majority rule and expand by up to two at a
    time. Failed replicas count towards the limit but do not contribute votes.

    args:
        session_factory: The shared async database session factory.
    returns:
        int | None: The processed task's id, or None if no eligible task is free.
    raises:
        ValueError: If the task has no replicas or more than five replicas.
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
        task = await session.scalar(stmt)
        if task is None:
            return None

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
        task.final_hash = winner

        if winner is not None:
            task.status = TaskStatus.VERIFIED
        elif len(replicas) == 5:
            task.status = TaskStatus.FAILED
        else:
            task.status = TaskStatus.DISPUTED
            session.add_all(
                Replica(task_id=task.id, status=ReplicaStatus.SCHEDULED, attempt=1)
                for _ in range(min(2, 5 - len(replicas)))
            )

        await session.flush()
        return task.id
