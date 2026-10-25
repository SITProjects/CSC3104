import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import S3_BUCKET, S3_ENDPOINT_URL
from shared.db.models import Job, JobStatus, Replica, ReplicaStatus, Task, TaskStatus
from shared.normalized_text import result_hash

log = logging.getLogger("verifier")


def ready_jobs() -> Select[tuple[Job]]:
    """
    Selects active OCR jobs with every expected task verified.

    Checks task_count as well as task status so an incomplete intake cannot
    publish a partial output. Other job types need their own assembler.

    returns:
        Select: The ready-job query, before ordering or locking.
    """
    task_count = (
        select(func.count(Task.id))
        .where(Task.job_id == Job.id)
        .correlate(Job)
        .scalar_subquery()
    )
    unfinished = (
        select(Task.id)
        .where(Task.job_id == Job.id, Task.status != TaskStatus.VERIFIED)
        .correlate(Job)
        .exists()
    )
    return select(Job).where(
        Job.job_type == "ocr",
        Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
        task_count == Job.task_count,
        ~unfinished,
    )


def get_task_pages(
    s3, job_id: UUID, task: Task, replicas: Sequence[Replica]
) -> dict[str, str]:
    """
    Reads one usable agreeing result, trying other copies when necessary.

    Recomputes the canonical hash before accepting uploaded content. Missing,
    malformed or mismatching copies cannot become part of the final output.

    args:
        s3: The configured S3 client.
        job_id: The job whose replica objects are stored in S3.
        task: The verified task with its final_hash and expected input files.
        replicas: Successful replicas whose reported hash matches final_hash.
    returns:
        dict[str, str]: The agreed page texts keyed by input filename.
    raises:
        ValueError: If no usable agreeing result exists.
        ClientError: If storage access fails for a reason other than a missing object.
    """
    for replica in replicas:
        key = f"jobs/{job_id}/replicas/{replica.id}/result.json"
        try:
            body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
                continue
            raise
        try:
            raw = body.read()
        finally:
            body.close()
        try:
            result = json.loads(raw)
            if result_hash(result) != task.final_hash:
                continue
            pages = result.get("pages") if isinstance(result, dict) else None
            if not isinstance(pages, dict) or not pages:
                continue
            if not all(isinstance(text, str) for text in pages.values()):
                continue
            files = task.input.get("files")
            if files is not None and set(pages) != set(files):
                continue
        except (ValueError, TypeError):
            continue
        return pages
    raise ValueError(f"No usable agreeing result for task {task.id}")


def assemble_output(
    s3, job_id: UUID, tasks: Sequence[Task], replicas: Sequence[Replica]
) -> bytes:
    """
    Assembles agreed OCR text in task_index and zero-padded page filename order.

    args:
        s3: The configured S3 client.
        job_id: The job whose output is being assembled.
        tasks: Verified tasks, already ordered by task_index.
        replicas: Agreeing replicas, ordered by id for deterministic selection.
    returns:
        bytes: UTF-8 text with blank lines between pages and a final newline.
    raises:
        ValueError: If a task has no usable agreeing result.
    """
    by_task = defaultdict(list)
    for replica in replicas:
        by_task[replica.task_id].append(replica)
    parts = []
    for task in tasks:
        pages = get_task_pages(s3, job_id, task, by_task[task.id])
        parts.extend(pages[name] for name in sorted(pages))
    return ("\n\n".join(parts) + "\n").encode("utf-8")


async def complete_job(
    session_factory: async_sessionmaker[AsyncSession], s3, job_id: UUID
) -> bool:
    """
    Locks a ready job, uploads its output and commits completion afterwards.

    A single job lock serializes completion with other verifiers and job
    cancellation. Storage errors roll back database changes. Uploading again
    after a crash uses the same deterministic key and content.

    args:
        session_factory: The shared async database session factory.
        s3: The configured S3 client.
        job_id: The candidate job to recheck and complete.
    returns:
        bool: True if completed, False if ineligible or locked by another process.
    """
    async with session_factory.begin() as session:
        job = await session.scalar(
            ready_jobs()
            .where(Job.id == job_id)
            .with_for_update(of=Job, skip_locked=True)
        )
        if job is None:
            return False
        tasks = (
            await session.scalars(
                select(Task).where(Task.job_id == job.id).order_by(Task.task_index)
            )
        ).all()
        replicas = (
            await session.scalars(
                select(Replica)
                .join(Task, Replica.task_id == Task.id)
                .where(
                    Task.job_id == job.id,
                    Replica.status == ReplicaStatus.DONE,
                    Replica.exit_code == 0,
                    Replica.result_hash == Task.final_hash,
                )
                .order_by(Replica.id)
            )
        ).all()
        content = await asyncio.to_thread(assemble_output, s3, job.id, tasks, replicas)
        await asyncio.to_thread(
            s3.put_object,
            Bucket=S3_BUCKET,
            Key=f"jobs/{job.id}/output/result.txt",
            Body=content,
            ContentType="text/plain; charset=utf-8",
        )
        job.status = JobStatus.COMPLETED
        job.finished_at = func.now()
    return True


async def output_loop(
    session_factory: async_sessionmaker[AsyncSession], interval: float = 5
) -> None:
    """
    Publishes ready OCR jobs while retrying storage and database failures.

    Visits every candidate each pass so one missing result cannot block later
    jobs. boto3 calls run in threads, allowing voting and trust sync to continue.

    args:
        session_factory: The shared async database session factory.
        interval: Seconds between completion passes, including failed attempts.
    """
    s3 = None
    try:
        while True:
            try:
                if s3 is None:
                    s3 = await asyncio.to_thread(
                        boto3.client,
                        "s3",
                        endpoint_url=S3_ENDPOINT_URL or None,
                        config=Config(
                            signature_version="s3v4",
                            s3={"addressing_style": "path"},
                            connect_timeout=5,
                            read_timeout=30,
                            retries={"max_attempts": 3},
                        ),
                    )
                async with session_factory() as session:
                    job_ids = (
                        await session.scalars(
                            ready_jobs()
                            .with_only_columns(Job.id)
                            .order_by(Job.created_at, Job.id)
                        )
                    ).all()
                for job_id in job_ids:
                    try:
                        if await complete_job(session_factory, s3, job_id):
                            log.info("Completed job %s", job_id)
                    except Exception:
                        log.exception("Could not assemble job %s; will retry", job_id)
            except Exception:
                log.exception("Output processing failed; will retry")
            await asyncio.sleep(interval)
    finally:
        if s3 is not None:
            await asyncio.to_thread(s3.close)
