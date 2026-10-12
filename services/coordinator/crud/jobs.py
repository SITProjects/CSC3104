import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from services.coordinator.models import CreateJobRequest
from shared.db import JobType
from shared.db.models import Job, JobStatus, Replica, ReplicaStatus, Task


async def ensure_job_type_exists(session: AsyncSession, job_type: str) -> None:
    if await session.get(JobType, job_type) is None:
        raise HTTPException(422, f"unknown job_type {job_type!r}")


async def create_job(session: AsyncSession, req: CreateJobRequest) -> Job:
    job = Job(
        owner_email=req.email,
        job_type=req.job_type,
        params=req.params,
        status=JobStatus.PENDING,
        task_count=len(req.tasks),
        replicas_per_task=req.replicas_per_task,
    )
    session.add(job)
    await session.flush()  # assigns job.id
    return job


async def mark_job_running(session: AsyncSession, job_id: uuid.UUID) -> None:
    stmt = (
        update(Job)
        .where(Job.id == job_id, Job.status == JobStatus.PENDING)
        .values(status=JobStatus.RUNNING)
    )
    await session.execute(stmt)


async def get_job(
    session: AsyncSession, job_id: uuid.UUID
) -> tuple[Job, float | None] | None:
    progress = (
        select(func.avg(Replica.progress))
        .select_from(Task)  # Start from tasks
        .join(Replica, Replica.task_id == Task.id)
        .where(Task.job_id == Job.id)
        .where(Replica.status.in_([ReplicaStatus.RUNNING, ReplicaStatus.DONE]))
        .correlate(Job)
        .scalar_subquery()
    )

    stmt = (
        select(Job, progress.label("progress"))
        .options(selectinload(Job.tasks).selectinload(Task.replicas))
        .where(Job.id == job_id)
    )
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    job, progress = row
    return job, float(progress) if progress is not None else None
