from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.models import CreateJobRequest
from shared.db import Job, ReplicaStatus, Task, TaskStatus
from shared.db.models import Replica


async def create_tasks(
    session: AsyncSession, job: Job, req: CreateJobRequest
) -> list[Task]:
    tasks = [
        Task(
            job_id=job.id,
            task_index=i,
            input=task.input,
            status=TaskStatus.PENDING,
            cpu_request=req.cpu_request,
            mem_request=req.mem_request,
        )
        for i, task in enumerate(req.tasks)
    ]
    session.add_all(tasks)
    await session.flush()  # assigns task ids
    return tasks


async def mark_task_running(session: AsyncSession, task_id: int) -> None:
    stmt = (
        update(Task)
        .where(
            Task.id == task_id,
            Task.status == TaskStatus.PENDING,
        )
        .values(status=TaskStatus.RUNNING)
    )

    await session.execute(stmt)


async def set_voting(session: AsyncSession, task: Task):
    """
    Checks if a task is eligible for voting based on its replicas' statuses and update the task's status to VOTING if eligible.

    args:
        task: The Task object to check for voting eligibility.

    """
    unfinished = await session.scalar(
        select(
            exists().where(
                Replica.task_id == task.id,
                Replica.status.in_(
                    [
                        ReplicaStatus.SCHEDULED,
                        ReplicaStatus.RUNNING,
                        ReplicaStatus.EVICTED,
                        ReplicaStatus.LOST,
                    ]
                ),
            )
        )
    )

    if not unfinished and task.status in [TaskStatus.DISPUTED, TaskStatus.RUNNING]:
        task.status = TaskStatus.VOTING
