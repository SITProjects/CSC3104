import uuid
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from services.coordinator.models import JobSpec
from shared.db import JobType, User
from shared.db.models import Job, JobStatus, Replica, ReplicaStatus, Task, TaskStatus


async def ensure_job_type_exists(session: AsyncSession, job_type: str) -> None:
    if await session.get(JobType, job_type) is None:
        raise HTTPException(422, f"unknown job_type {job_type!r}")


async def create_job(
    session: AsyncSession,
    req: JobSpec,
    owner_id: uuid.UUID,
) -> Job:
    job = Job(
        id=req.job_id or uuid.uuid4(),
        owner_id=owner_id,
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


async def list_jobs(
    session: AsyncSession,
    owner_id: uuid.UUID | None,
    limit: int,
    offset: int,
    statuses: list[JobStatus] | None = None,
    created_from: date | None = None,
    created_to: date | None = None,
) -> list[Job]:
    """
    Returns jobs, newest first.

    args:
        session: The database session for this request.
        owner_id: The user whose jobs to list, or None for everyone's.
        limit: The most jobs to return.
        offset: How many of the newest jobs to skip.
        statuses: Only jobs in one of these states, or None (or empty) for all.
        created_from: Only jobs created on or after this day (UTC), if given.
        created_to: Only jobs created on or before this day (UTC), if given.
    returns:
        list[Job]: The jobs, without their tasks.
    """
    stmt = select(Job).order_by(Job.created_at.desc(), Job.id)
    if owner_id is not None:
        stmt = stmt.where(Job.owner_id == owner_id)
    if statuses:
        stmt = stmt.where(Job.status.in_(statuses))
    if created_from is not None:
        start = datetime.combine(created_from, time.min, UTC)
        stmt = stmt.where(Job.created_at >= start)
    # date.max has no next day to stop before, and nothing is created after it
    if created_to is not None and created_to < date.max:
        end = datetime.combine(created_to + timedelta(days=1), time.min, UTC)
        stmt = stmt.where(Job.created_at < end)
    return list((await session.scalars(stmt.limit(limit).offset(offset))).all())


ACTIVE_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)


async def count_active_jobs(
    session: AsyncSession, owner_id: uuid.UUID, lock: bool = False
) -> int:
    """
    Counts the user's unfinished (PENDING or RUNNING) jobs.

    args:
        lock: Lock the user's row first (inside a transaction), so concurrent
            submissions by the same user are counted one after another.
    """
    if lock:
        await session.execute(
            select(User.id).where(User.id == owner_id).with_for_update()
        )
    return await session.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.owner_id == owner_id, Job.status.in_(ACTIVE_STATUSES))
    )


async def count_jobs_by_status(session: AsyncSession) -> dict[str, int]:
    """
    returns:
        dict: How many jobs are in each status, every status present even if zero.
    """
    rows = await session.execute(select(Job.status, func.count()).group_by(Job.status))
    counts = dict.fromkeys((s.value for s in JobStatus), 0)
    counts.update({status.value: count for status, count in rows.all()})
    return counts


async def cancel_job(session: AsyncSession, job_id: uuid.UUID) -> list[tuple[int, int]]:
    """
    Cancels a job and all its tasks and replicas. Only PENDING or RUNNING jobs can be cancelled.

    args:
        session: The database session for this request.
        job_id: The id of the job to cancel.
    returns:
        list[tuple[int, int]] | None: A list of (replica_id, attempt) tuples of the cancelled replicas.
    """
    job = await session.get(Job, job_id, with_for_update=True)
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
