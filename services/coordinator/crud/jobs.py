import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from services.coordinator.models import CreateJobRequest
from shared.db import JobType
from shared.db.models import Job, JobStatus, Replica, ReplicaStatus, Task, TaskStatus


async def ensure_job_type_exists(session: AsyncSession, job_type: str) -> None:
    if await session.get(JobType, job_type) is None:
        raise HTTPException(422, f"unknown job_type {job_type!r}")


async def create_job(session: AsyncSession, req: CreateJobRequest) -> Job:
    job = Job(
        id=req.job_id or uuid.uuid4(),
        owner_email=req.email,
        job_type=req.job_type,
        params=req.params,
        status=JobStatus.PENDING,
        task_count=len(req.tasks),
        replicas_per_task=req.replicas_per_task,
    )
    session.add(job)
    try:
        await session.flush()  # assigns job.id
    except IntegrityError as e:
        raise HTTPException(409, f"job_id {req.job_id!r} already exists") from e
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


async def cancel_job(
    session: AsyncSession, job_id: uuid.UUID
) -> list[tuple[int, int]] | None:
    """
    Cancels a job and all its tasks and replicas. Only PENDING or RUNNING jobs can be cancelled.

    args:
        session: The database session for this request.
        job_id: The id of the job to cancel.
    returns:
        list[tuple[int, int]] | None: A list of (replica_id, attempt) tuples of the cancelled replicas, or None if the job was not found or already completed.
    """
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
        raise HTTPException(409, f"job {job_id} is {job.status.value}, cannot cancel")
    now = datetime.now(UTC)
    # Replicas first, the same order the scheduler locks them in, so a replica
    # being launched is waited for and then cancelled instead of deadlocking.
    result = await session.execute(
        update(Replica)
        .where(
            Replica.task_id.in_(select(Task.id).where(Task.job_id == job_id)),
            Replica.status.in_(
                [
                    ReplicaStatus.SCHEDULED,
                    ReplicaStatus.RUNNING,
                    ReplicaStatus.EVICTED,
                    ReplicaStatus.LOST,
                ]
            ),
        )
        .values(status=ReplicaStatus.CANCELLED, token_hash=None, ended_at=now)
        .returning(Replica.id, Replica.attempt)
    )
    await session.execute(
        update(Task)
        .where(
            Task.job_id == job_id,
            Task.status.in_(
                [
                    TaskStatus.PENDING,
                    TaskStatus.RUNNING,
                    TaskStatus.VOTING,
                    TaskStatus.DISPUTED,
                ]
            ),
        )
        .values(status=TaskStatus.CANCELLED)
    )
    await session.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
        )
        .values(status=JobStatus.CANCELLED, finished_at=now)
    )
    return [(replica_id, attempt) for replica_id, attempt in result.all()]
