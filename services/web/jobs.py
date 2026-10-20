"""
Job intake and status for the web app.

An upload becomes a job in three steps:
1. start_upload saves an uploads row and jobs/{job_id}/input.pdf. The S3 write
   starts member 6's splitter Lambda.
2. Each render Lambda calls /hooks/pages-ready, handled by pages_ready. Repeats
   are ignored through upload_batches.
3. Once the batches cover every page, submit registers the job with the
   coordinator under the same id, as 10-page tasks with 3 replicas each.
"""

import asyncio
import hashlib
import logging
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from services.web import storage
from services.web.coordinator import CoordinatorClient, CoordinatorError
from services.web.models import ApiKey, Upload, UploadBatch, UploadStatus
from services.web.settings import (
    CPU_REQUEST,
    DENSE_PAGE_BYTES,
    JOB_TYPE,
    MAX_RUNNING_JOBS,
    MAX_UPLOAD_BYTES,
    MEM_DENSE,
    MEM_NORMAL,
    PAGES_PER_TASK,
    REPLICAS_PER_TASK,
)
from shared.db import Event, Job, JobStatus, JobView, Replica, ReplicaStatus
from shared.db.models import Node

log = logging.getLogger("web.jobs")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
FINISHED = {"COMPLETED", "FAILED", "CANCELLED"}


class UserError(Exception):
    """A problem to show the user, with the HTTP status to reply with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def clean_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise UserError(400, "Enter a valid email address.")
    return email


def check_pdf(filename: str, data: bytes) -> str:
    """
    Checks an upload is a PDF within the size limit.

    args:
        filename: The name the browser sent.
        data: The file contents.
    returns:
        str: A safe display name for the file.
    raises:
        UserError: 413 if too large, 400 if empty or not a PDF.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise UserError(413, f"The PDF is over {MAX_UPLOAD_BYTES // 2**20} MB.")
    if not data:
        raise UserError(400, "The file is empty.")
    # The extension and content type are up to the client, the magic bytes are not
    if not data.startswith(b"%PDF-"):
        raise UserError(400, "Only PDF files are accepted.")
    name = re.split(r"[\\/]", filename or "")[-1].strip() or "upload.pdf"
    return name[:200]


async def running_jobs(session: AsyncSession, email: str) -> int:
    """Counts a user's uploads still being split plus their unfinished jobs."""
    stmt = (
        select(func.count())
        .select_from(Upload)
        .outerjoin(Job, Job.id == Upload.job_id)
        .where(
            Upload.owner_email == email,
            or_(
                Upload.status == UploadStatus.SPLITTING.value,
                and_(
                    Upload.status == UploadStatus.SUBMITTED.value,
                    Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
                ),
            ),
        )
    )
    return (await session.execute(stmt)).scalar_one()


async def start_upload(
    session: AsyncSession,
    email: str,
    filename: str,
    pdf: bytes,
    webhook_url: str | None = None,
) -> UUID:
    """
    Records an upload and saves its PDF to S3, which starts page splitting.

    args:
        session: A database session with no open transaction.
        email: The owner's email, already cleaned.
        filename: The display name from check_pdf.
        pdf: The checked PDF bytes.
        webhook_url: A checked URL to notify when the job ends, if any.
    returns:
        UUID: The new job id.
    raises:
        UserError: 429 if the user already has MAX_RUNNING_JOBS running, 502 if
            the PDF cannot be stored.
    """
    job_id = uuid4()
    async with session.begin():
        # One user's uploads take turns here, so two at once cannot both pass the limit
        await session.execute(
            select(func.pg_advisory_xact_lock(func.hashtext(f"upload:{email}")))
        )
        if await running_jobs(session, email) >= MAX_RUNNING_JOBS:
            raise UserError(
                429,
                f"You already have {MAX_RUNNING_JOBS} jobs running. "
                "Wait for one to finish or cancel it.",
            )
        session.add(
            Upload(
                job_id=job_id,
                owner_email=email,
                filename=filename,
                size_bytes=len(pdf),
                webhook_url=webhook_url,
            )
        )
    # Committed first: the splitter can call back before put_object returns
    try:
        await asyncio.to_thread(
            storage.put_object,
            storage.input_key(str(job_id)),
            pdf,
            "application/pdf",
        )
    except Exception:
        log.exception("could not store input.pdf for job %s", job_id)
        async with session.begin():
            upload = await session.get(Upload, job_id, with_for_update=True)
            if upload is not None:
                upload.status = UploadStatus.FAILED.value
                upload.error = "The PDF could not be stored. Try again."
        raise UserError(502, "The PDF could not be stored. Try again.")
    log.info("job %s uploaded by %s (%d bytes)", job_id, email, len(pdf))
    return job_id


def resource_request(page_sizes: list[int]) -> tuple[int, int]:
    """
    The automatic resource rule (member 5, step 3): users never type limits.

    args:
        page_sizes: Bytes of each rendered page PNG.
    returns:
        tuple[int, int]: CPU cores and memory in MiB for every task of the job.
    """
    if page_sizes and sum(page_sizes) / len(page_sizes) > DENSE_PAGE_BYTES:
        return CPU_REQUEST, MEM_DENSE
    return CPU_REQUEST, MEM_NORMAL


def make_tasks(page_count: int) -> list[dict[str, Any]]:
    """Splits pages 1..page_count into tasks of PAGES_PER_TASK pages."""
    tasks = []
    for start in range(1, page_count + 1, PAGES_PER_TASK):
        end = min(start + PAGES_PER_TASK - 1, page_count)
        files = [storage.page_name(page) for page in range(start, end + 1)]
        tasks.append({"input": {"files": files, "pages": [start, end]}})
    return tasks


async def submit(
    session: AsyncSession, coordinator: CoordinatorClient, upload: Upload
) -> None:
    """
    Registers a fully rendered upload with the coordinator. Runs inside the
    caller's transaction, with the upload row locked.

    raises:
        CoordinatorError: If the coordinator is down or fails (5xx). The caller
            rolls back, so a retried callback submits again.
    """
    assert upload.page_count is not None
    sizes = await asyncio.to_thread(storage.page_sizes, str(upload.job_id))
    cpu, mem = resource_request(sizes)
    upload.pages_per_task = PAGES_PER_TASK
    body = {
        "job_id": str(upload.job_id),
        "email": upload.owner_email,
        "job_type": JOB_TYPE,
        "params": {"filename": upload.filename, "page_count": upload.page_count},
        "tasks": make_tasks(upload.page_count),
        "replicas_per_task": REPLICAS_PER_TASK,
        "cpu_request": cpu,
        "mem_request": mem,
    }
    try:
        await coordinator.create_job(body)
    except CoordinatorError as error:
        if error.status == 409:
            pass  # an earlier attempt got through before its reply was lost
        elif 400 <= error.status < 500:
            log.error("coordinator rejected job %s: %s", upload.job_id, error.detail)
            upload.status = UploadStatus.FAILED.value
            upload.error = "The scheduler rejected this job."  # detail is in the log
            return
        else:
            raise
    upload.status = UploadStatus.SUBMITTED.value
    log.info(
        "job %s submitted: %d pages, %d CPU, %d MiB",
        upload.job_id,
        upload.page_count,
        cpu,
        mem,
    )


async def pages_ready(
    session: AsyncSession, coordinator: CoordinatorClient, payload: dict[str, Any]
) -> None:
    """
    Handles one pages-ready callback (serverless/README.md has the format).

    args:
        session: A database session with no open transaction.
        coordinator: The coordinator client, for submitting the job.
        payload: The callback's JSON body.
    raises:
        UserError: 400 for a malformed payload, 404 for an unknown job.
        CoordinatorError: If the job could not be submitted yet. Nothing is
            saved, so the Lambda's retry tries again.
    """
    try:
        job_id = UUID(str(payload["job_id"]))
        status = payload["status"]
        if status == "ok":
            page_count = int(payload["page_count"])
            start, end = int(payload["page_start"]), int(payload["page_end"])
            if not 1 <= start <= end <= page_count:
                raise ValueError("bad page range")
        elif status != "failed":
            raise ValueError(f"unknown status {status!r}")
    except (KeyError, TypeError, ValueError) as error:
        raise UserError(400, f"bad payload: {error}") from error

    async with session.begin():
        upload = await session.get(Upload, job_id, with_for_update=True)
        if upload is None:
            raise UserError(404, f"unknown job {job_id}")
        if upload.status != UploadStatus.SPLITTING.value:
            return  # already submitted, failed or cancelled; a repeat is harmless
        if status == "failed":
            upload.status = UploadStatus.FAILED.value
            upload.error = str(payload.get("error") or "The PDF could not be split.")[
                :500
            ]
            log.warning("job %s failed to split: %s", job_id, upload.error)
            return
        upload.page_count = page_count
        await session.execute(
            pg_insert(UploadBatch)
            .values(job_id=job_id, page_start=start, page_end=end)
            .on_conflict_do_nothing()
        )
        covered = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(UploadBatch.page_end - UploadBatch.page_start + 1), 0
                    )
                ).where(UploadBatch.job_id == job_id)
            )
        ).scalar_one()
        if covered >= page_count:
            await submit(session, coordinator, upload)


async def cancel(
    session: AsyncSession, coordinator: CoordinatorClient, job_id: UUID
) -> None:
    """
    Cancels a job: locally while it is still being split, otherwise through
    the coordinator, which stops every replica.

    raises:
        UserError: 404 for an unknown job, 409 if it has already ended.
    """
    async with session.begin():
        upload = await session.get(Upload, job_id, with_for_update=True)
        if upload is None:
            raise UserError(404, "Job not found.")
        if upload.status == UploadStatus.SPLITTING.value:
            upload.status = UploadStatus.CANCELLED.value
            return
        if upload.status != UploadStatus.SUBMITTED.value:
            raise UserError(409, f"The job is already {upload.status.lower()}.")
    try:
        await coordinator.cancel_job(job_id)
    except CoordinatorError as error:
        if error.status in (404, 409):
            raise UserError(409, "The job has already finished.") from error
        raise UserError(502, "The coordinator could not cancel the job.") from error


@dataclass
class JobSnapshot:
    """Everything the status page, the live stream and the API show for a job."""

    upload: Upload
    job: JobView | None = None
    pages_ready: int = 0
    events: list[Event] = field(default_factory=list)
    coordinator_down: bool = False

    @property
    def stage(self) -> str:
        if self.upload.status != UploadStatus.SUBMITTED.value:
            return self.upload.status
        return self.job.status.value if self.job else "PENDING"

    @property
    def finished(self) -> bool:
        return self.stage in FINISHED

    @property
    def progress(self) -> int:
        if self.job:
            return 100 if self.stage == "COMPLETED" else self.job.progress
        return 0

    def as_api(self) -> dict[str, Any]:
        tasks = self.job.tasks if self.job else []
        return {
            "job_id": str(self.upload.job_id),
            "filename": self.upload.filename,
            "status": self.stage,
            "error": self.upload.error,
            "page_count": self.upload.page_count,
            "pages_rendered": self.pages_ready,
            "progress": self.progress,
            "tasks_total": len(tasks),
            "tasks_verified": sum(t.status.value == "VERIFIED" for t in tasks),
            "created_at": self.upload.created_at.isoformat(),
            "result_ready": self.stage == "COMPLETED",
        }


async def snapshot(
    session: AsyncSession, coordinator: CoordinatorClient, job_id: UUID
) -> JobSnapshot | None:
    upload = await session.get(Upload, job_id)
    if upload is None:
        return None
    snap = JobSnapshot(upload=upload)
    if upload.status == UploadStatus.SPLITTING.value:
        snap.pages_ready = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(UploadBatch.page_end - UploadBatch.page_start + 1), 0
                    )
                ).where(UploadBatch.job_id == job_id)
            )
        ).scalar_one()
    elif upload.status == UploadStatus.SUBMITTED.value:
        snap.pages_ready = upload.page_count or 0
        try:
            snap.job = await coordinator.get_job(job_id)
        except CoordinatorError:
            log.warning("coordinator unavailable for job %s", job_id)
            snap.coordinator_down = True
        snap.events = list(
            (
                await session.execute(
                    select(Event)
                    .where(Event.job_id == job_id)
                    .order_by(Event.time.desc())
                    .limit(30)
                )
            ).scalars()
        )
    return snap


async def history(session: AsyncSession, email: str) -> list[tuple[Upload, Job | None]]:
    """A user's 100 most recent uploads, each with its job once submitted."""
    rows = await session.execute(
        select(Upload, Job)
        .outerjoin(Job, Job.id == Upload.job_id)
        .where(Upload.owner_email == email)
        .order_by(Upload.created_at.desc())
        .limit(100)
    )
    return [(upload, job) for upload, job in rows.all()]


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def create_api_key(session: AsyncSession, email: str) -> str:
    """Makes a new API key for email. Only its hash is stored, so show it once."""
    key = "ic_" + secrets.token_urlsafe(32)
    async with session.begin():
        session.add(ApiKey(key_hash=hash_key(key), owner_email=email))
    return key


async def api_key_owner(session: AsyncSession, key: str) -> str | None:
    row = await session.get(ApiKey, hash_key(key))
    return row.owner_email if row else None


async def admin_overview(session: AsyncSession) -> dict[str, Any]:
    """Nodes, burst machines and recent jobs with their burst cost."""
    running = (
        select(Replica.node_name, func.count().label("running"))
        .where(Replica.status == ReplicaStatus.RUNNING)
        .group_by(Replica.node_name)
        .subquery()
    )
    nodes = (
        await session.execute(
            select(Node, func.coalesce(running.c.running, 0))
            .outerjoin(running, running.c.node_name == Node.name)
            .order_by(Node.state, Node.name)
        )
    ).all()
    # The burst cost views come from member 6's migration and have no ORM models
    machines = (
        (
            await session.execute(
                text(
                    "SELECT * FROM burst_machine_cost "
                    "ORDER BY running DESC, launched_at DESC LIMIT 20"
                )
            )
        )
        .mappings()
        .all()
    )
    jobs = (
        (
            await session.execute(
                text(
                    """
                    SELECT u.job_id, u.owner_email, u.filename, u.page_count,
                           u.status AS upload_status, u.created_at,
                           j.status AS job_status, j.finished_at,
                           c.usd, c.has_estimated_runs
                    FROM uploads u
                    LEFT JOIN jobs j ON j.id = u.job_id
                    LEFT JOIN burst_job_cost c ON c.job_id = u.job_id
                    ORDER BY u.created_at DESC
                    LIMIT 30
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    return {"nodes": nodes, "machines": machines, "jobs": jobs}
