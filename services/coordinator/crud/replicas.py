import hashlib
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import RETRY_ATTEMPTS
from shared.db.models import (
    Job,
    JobStatus,
    JobType,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
)


async def create_replicas(
    session: AsyncSession, tasks: list[Task], replicas_per_task: int
) -> list[Replica]:
    replicas = [
        Replica(task_id=task.id, attempt=1, status=ReplicaStatus.SCHEDULED)
        for task in tasks
        for _ in range(replicas_per_task)
    ]
    session.add_all(replicas)
    await session.flush()
    return replicas


async def fetch_scheduled_replicas(
    session: AsyncSession, limit: int
) -> list[tuple[Replica, Task, JobType]]:
    """
    Fetches list of scheduled replicas from database, up to specified limit
    """
    stmt = (
        select(Replica, Task, JobType)
        .join(Replica.task)
        .join(Task.job)
        .join(Job.job_type_obj)
        .where(
            Replica.status == ReplicaStatus.SCHEDULED,
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .order_by(Replica.id)
        .limit(limit)
        # of Replica to only lock the Replica table, not the Task or Job tables
        .with_for_update(skip_locked=True, of=Replica)
    )
    result = await session.execute(stmt)
    return [(replica, task, job_type) for replica, task, job_type in result.all()]


async def get_replica_with_task(
    session: AsyncSession, replica_id: int, lock: bool
) -> tuple[Replica, Task, JobType] | None:
    stmt = (
        select(Replica, Task, JobType)
        .join(Replica.task)
        .join(Task.job)
        .join(Job.job_type_obj)
        .where(Replica.id == replica_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=Replica)
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    replica, task, job_type = row
    return replica, task, job_type


async def evict_replica(session: AsyncSession, replica: Replica) -> None:
    """
    Evicts a replica from the database by setting its status to EVICTED.

    args:
        session: The database session for this request.
        replica_id: The id of the replica to evict.
        replica: The Replica object to evict.
    """
    replica.token_hash = None
    replica.node_name = None
    if replica.attempt >= RETRY_ATTEMPTS:
        replica.status = ReplicaStatus.FAILED
        replica.ended_at = datetime.now(UTC)
    else:
        replica.status = ReplicaStatus.EVICTED
    await session.flush()


async def recover_replicas(session: AsyncSession) -> None:
    """
    Recovers a replica from the database by setting its status to SCHEDULED.

    args:
        session: The database session for this request.
        replica_id: The id of the replica to recover.
    """
    stmt = (
        select(Replica)
        .join(Task, Replica.task_id == Task.id)
        .where(
            or_(
                Replica.status == ReplicaStatus.EVICTED,
                Replica.status == ReplicaStatus.LOST,
            ),
            Task.status == TaskStatus.RUNNING,
        )
        .with_for_update(of=Replica, skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars()
    for replica in rows:
        if replica.attempt >= RETRY_ATTEMPTS:
            replica.status = ReplicaStatus.FAILED
            replica.ended_at = datetime.now(UTC)
            continue
        replica.attempt += 1
        replica.status = ReplicaStatus.SCHEDULED
        replica.started_at = None
        replica.ended_at = None
        await session.flush()


async def schedule_replica(session: AsyncSession, replica: Replica, token: str) -> None:
    """
    Schedules replicas for a list of tasks by creating Replica objects in the database.

    args:
        session: The database session for this request.
        tasks: A list of Task objects to schedule replicas for.
        replicas_per_task: The number of replicas to create for each task.
    """
    replica.status = ReplicaStatus.RUNNING
    replica.token_hash = hashlib.sha256(token.encode()).hexdigest()
    replica.progress = 0
    replica.started_at = datetime.now(UTC)
    await session.flush()


async def mark_replica_done(
    session: AsyncSession,
    replica: Replica,
    task: Task,
    result_hash: str,
    exit_code: int,
) -> None:
    """
    Marks a replica as done by updating its status, progress, result hash, exit code, and ended_at timestamp.

    args:
        replica: The Replica object to mark as done.
        result_hash: The hash of the result produced by the replica.
        exit_code: The exit code of the replica process.
    """
    if task is None:
        raise ValueError(f"task {replica.task_id} not found")
    replica.status = ReplicaStatus.DONE
    replica.progress = 100
    replica.result_hash = result_hash
    replica.exit_code = exit_code
    replica.ended_at = datetime.now(UTC)
    await session.flush()
