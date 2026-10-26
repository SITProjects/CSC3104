import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.coordinator.crud import create_event
from shared.db.models import Job, JobStatus, Replica, ReplicaStatus, Task, TaskStatus

log = logging.getLogger("verifier")

ACTIVE_JOBS = (JobStatus.PENDING, JobStatus.RUNNING)
# Copies and tasks that are still waiting or working; a failed job stops them
UNFINISHED_REPLICAS = (
    ReplicaStatus.SCHEDULED,
    ReplicaStatus.RUNNING,
    ReplicaStatus.EVICTED,
    ReplicaStatus.LOST,
)
UNFINISHED_TASKS = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.VOTING,
    TaskStatus.DISPUTED,
)


def failed_tasks(job_id) -> Select[int]:
    """Selects the ids of a job's tasks that failed verification for good."""
    return select(Task.id).where(
        Task.job_id == job_id, Task.status == TaskStatus.FAILED
    )


async def fail_job(session: AsyncSession, job: Job) -> None:
    """
    Fails a job with a task that can no longer be verified, in the caller's
    transaction.

    Like a cancel, the job's unfinished copies become CANCELLED without their
    tokens, so their late results are refused and coordinator housekeeping
    deletes their Pods. Otherwise the job would stay RUNNING for ever and keep
    counting towards its owner's limit of unfinished jobs.

    args:
        session: The verifier's current transaction.
        job: The locked active job with at least one FAILED task.
    """
    now = datetime.now(UTC)
    failed = (await session.scalars(failed_tasks(job.id).order_by(Task.id))).all()
    await session.execute(
        update(Replica)
        .where(
            Replica.task_id.in_(select(Task.id).where(Task.job_id == job.id)),
            Replica.status.in_(UNFINISHED_REPLICAS),
        )
        .values(status=ReplicaStatus.CANCELLED, token_hash=None, ended_at=now)
    )
    await session.execute(
        update(Task)
        .where(Task.job_id == job.id, Task.status.in_(UNFINISHED_TASKS))
        .values(status=TaskStatus.CANCELLED)
    )
    job.status = JobStatus.FAILED
    job.finished_at = now
    await create_event(
        session, "job_failed", job.id, failed[0], detail={"failed_task_ids": failed}
    )


def completed_other_jobs() -> Select[Job]:
    """
    Selects active jobs that aren't OCR with every expected task verified.

    OCR jobs are completed by output_loop once their text is assembled; other
    job types have no output to assemble, so a full set of verified tasks is
    enough.
    """
    task_count = (
        select(func.count(Task.id))
        .where(Task.job_id == Job.id)
        .correlate(Job)
        .scalar_subquery()
    )
    unverified = (
        select(Task.id)
        .where(Task.job_id == Job.id, Task.status != TaskStatus.VERIFIED)
        .correlate(Job)
        .exists()
    )
    return select(Job).where(
        Job.job_type != "ocr",
        Job.status.in_(ACTIVE_JOBS),
        task_count == Job.task_count,
        ~unverified,
    )


async def finish_jobs(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """
    Ends the jobs that are done but that nothing else would end: jobs with a
    failed task, and verified jobs of types other than OCR.

    Locks each job with SKIP LOCKED, the same row a cancel locks first, so a
    concurrent cancel or second verifier simply wins.
    """
    async with session_factory.begin() as session:
        has_failed_task = failed_tasks(Job.id).correlate(Job).exists()
        jobs = (
            await session.scalars(
                select(Job)
                .where(Job.status.in_(ACTIVE_JOBS), has_failed_task)
                .order_by(Job.created_at)
                .with_for_update(of=Job, skip_locked=True)
            )
        ).all()
        for job in jobs:
            await fail_job(session, job)
            log.info("Failed job %s: a task could not be verified", job.id)

    async with session_factory.begin() as session:
        jobs = (
            await session.scalars(
                completed_other_jobs()
                .order_by(Job.created_at)
                .with_for_update(of=Job, skip_locked=True)
            )
        ).all()
        for job in jobs:
            job.status = JobStatus.COMPLETED
            job.finished_at = datetime.now(UTC)
            log.info("Completed %s job %s", job.job_type, job.id)


async def finish_loop(
    session_factory: async_sessionmaker[AsyncSession], interval: float = 5
) -> None:
    """
    Runs finish_jobs every interval seconds, retrying database failures.

    args:
        session_factory: The shared async database session factory.
        interval: Seconds between passes, including failed ones.
    """
    while True:
        try:
            await finish_jobs(session_factory)
        except Exception:
            log.exception("Could not finish jobs; will retry")
        await asyncio.sleep(interval)
