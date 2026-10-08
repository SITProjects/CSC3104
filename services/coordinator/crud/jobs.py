from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.models import CreateJobRequest
from shared.db import JobType
from shared.db.models import Job, JobStatus


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
