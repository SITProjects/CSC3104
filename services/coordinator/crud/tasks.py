from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.models import CreateJobRequest
from shared.db import Job, Task, TaskStatus


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
