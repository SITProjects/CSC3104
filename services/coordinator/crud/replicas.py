from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    session: AsyncSession, replica_id: int
) -> tuple[Replica, Task, JobType] | None:
    stmt = (
        select(Replica, Task, JobType)
        .join(Replica.task)
        .join(Task.job)
        .join(Job.job_type_obj)
        .where(Replica.id == replica_id)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    replica, task, job_type = row
    return replica, task, job_type
