from sqlalchemy import exists, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.models import JobSpec, TaskIn
from shared.db import Job, ReplicaStatus, Task, TaskStatus
from shared.db.models import Replica


async def create_tasks(session: AsyncSession, job: Job, req: JobSpec) -> list[Task]:
    """
    Creates one PENDING task per entry in the request, numbered by each task's
    index, or from 0 in list order when the request gives none.

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
            task_index=i if task.index is None else task.index,
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


async def add_tasks(
    session: AsyncSession, job: Job, new_tasks: list[TaskIn]
) -> list[Task]:
    """
    Adds tasks to an existing job, skipping any index it already has.

    Safe to repeat: a task index that exists is left as it is, so a retried or
    duplicated request adds nothing twice. The new tasks get the same CPU and
    memory as the job's first task.

    args:
        session: The caller's transaction, holding the job's row lock.
        job: The job, created with a task_count that leaves room for these.
        new_tasks: Tasks with their index set.
    returns:
        list[Task]: Only the tasks that were actually added.
    """
    first = await session.scalar(
        select(Task).where(Task.job_id == job.id).order_by(Task.task_index).limit(1)
    )
    if first is None:
        raise ValueError(f"job {job.id} has no tasks to copy resources from")
    rows = [
        {
            "job_id": job.id,
            "task_index": task.index,
            "input": task.input,
            "status": TaskStatus.PENDING,
            "cpu_request": first.cpu_request,
            "mem_request": first.mem_request,
        }
        for task in new_tasks
    ]
    stmt = (
        insert(Task)
        .values(rows)
        .on_conflict_do_nothing(index_elements=[Task.job_id, Task.task_index])
        .returning(Task)
    )
    return list((await session.scalars(stmt)).all())


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
