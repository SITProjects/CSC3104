from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.models import CreateJobRequest
from shared.db import Job, ReplicaStatus, Task, TaskStatus
from shared.db.models import Replica


async def create_tasks(
    session: AsyncSession, job: Job, req: CreateJobRequest
) -> list[Task]:
    """
    Creates one PENDING task per entry in the request, numbered from 0.

    args:
        session: The database session for this request.
        job: The new job the tasks belong to.
        req: The create-job request, for each task's input and resources.
    returns:
        list[Task]: The new tasks, with ids assigned.
    """
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
    """
    Moves a task from PENDING to RUNNING when its first replica launches.

    args:
        session: The scheduler's database session.
        task_id: The id of the task.
    """
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
    Moves a task to VOTING once none of its replicas are still in progress.

    In progress means SCHEDULED, RUNNING, EVICTED or LOST, so a task whose
    replicas are all DONE or FAILED goes to voting. Call it after a replica
    reaches DONE or FAILED.

    args:
        session: The database session for this request.
        task: The task to check.
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
