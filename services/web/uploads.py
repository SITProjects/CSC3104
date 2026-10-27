"""From an uploaded PDF to a coordinator job.

The upload is saved to S3, which starts the Lambda page splitter. Each Lambda
batch calls back /hooks/pages-ready. The first batch that is ready creates the
job, with its final task count, and each later batch adds its own tasks, so OCR
starts while the rest of the PDF is still being rendered. The coordinator calls
need the uploader's bearer token, so it is kept in jobs/{id}/pending.json
(private bucket) until every task has been sent.

There is no in-memory state, so it works with more than one web replica and
survives a restart: "are all pages there?" is answered by listing S3.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

import httpx
import pymupdf
from pydantic import BaseModel

from services.web import jobs, storage
from services.web.coordinator import coordinator
from services.web.settings import settings

log = logging.getLogger("uvicorn.error")

TIMESTAMP_HEADER = "X-Idlecloud-Timestamp"
SIGNATURE_HEADER = "X-Idlecloud-Signature"
MAX_AGE_SECONDS = 300
LOCAL_RENDER_DPI = 200  # the Lambda uses 300; lower keeps local runs quick


class PagesReady(BaseModel):
    """The Lambda's callback, see serverless/README.md. Extra fields are ignored."""

    status: Literal["ok", "failed"]
    job_id: UUID
    page_count: int | None = None
    error: str | None = None


def verify_signature(
    secret: str,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    now: float | None = None,
) -> bool:
    """Checks hex(HMAC-SHA256(secret, f"{timestamp}." + body)) and its age."""
    if not secret or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(int(now if now is not None else time.time()) - sent_at) > MAX_AGE_SECONDS:
        return False
    expected = hmac.new(
        secret.encode(), f"{sent_at}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def save_upload(
    job_id: UUID,
    owner_id: str,
    token: str,
    filename: str,
    data: bytes,
    pages: int,
    cpu: int,
    mem: int,
    pages_per_task: int | None = None,
) -> None:
    """
    Stores the PDF so the page splitter starts.

    pages_per_task fixes how the PDF is split (jobs.plan_tasks), so every
    callback, whichever web replica gets it, plans the same tasks.

    The pending record goes first, because writing input.pdf is what triggers the
    Lambda and its first callback may arrive within a second.
    """
    await asyncio.to_thread(
        storage.put_json,
        storage.pending_key(job_id),
        {
            "state": "rendering",
            "owner_id": owner_id,
            "token": token,
            "filename": filename,
            "page_count": pages,
            "cpu_request": cpu,
            "mem_request": mem,
            "pages_per_task": pages_per_task or settings.pages_per_task,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    await asyncio.to_thread(
        storage.put_bytes,
        storage.pending_index_key(owner_id, job_id),
        b"",
        "application/octet-stream",
    )
    await asyncio.to_thread(
        storage.put_bytes, storage.input_key(job_id), data, "application/pdf"
    )


def timestamp_is_fresh(timestamp: str | None, now: float | None = None) -> bool:
    """Cheap check of the callback's timestamp header, done before reading its body."""
    try:
        sent_at = int(timestamp or "")
    except ValueError:
        return False
    return (
        abs(int(now if now is not None else time.time()) - sent_at) <= MAX_AGE_SECONDS
    )


async def _cancel_started_job(job_id: UUID | str, pending: dict) -> None:
    """
    Cancels the coordinator job for an upload that is failing, if its first
    batches already created one, so it can't wait for the rest for ever. Best
    effort: no job (404), an ended one (409) or no token is nothing to do.
    """
    token = pending.get("token")
    if not token:
        return
    try:
        await coordinator.cancel_job(token, UUID(str(job_id)))
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (401, 404, 409):
            log.warning("cancelling %s after its upload failed: %r", job_id, exc)
    except httpx.RequestError as exc:
        log.warning("cancelling %s after its upload failed: %r", job_id, exc)


async def _fail(job_id: UUID | str, pending: dict, message: str) -> dict:
    """
    Marks an upload failed and deletes its stored session token, after
    cancelling any job its first batches already started.
    """
    await _cancel_started_job(job_id, pending)
    pending = {**pending, "state": "failed", "error": message}
    pending.pop("token", None)
    await asyncio.to_thread(storage.put_json, storage.pending_key(job_id), pending)
    return pending


def is_expired(pending: dict, now: datetime | None = None) -> bool:
    """True if the upload has been waiting for its pages longer than the limit."""
    if pending.get("state") != "rendering":
        return False
    try:
        created = datetime.fromisoformat(pending["created_at"])
    except (KeyError, ValueError, TypeError):
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    limit = timedelta(minutes=settings.pending_ttl_minutes)
    return (now or datetime.now(UTC)) - created > limit


async def expire_if_stale(job_id: UUID | str, pending: dict) -> dict:
    """
    Fails an upload that has waited too long for its pages, so a callback that
    never arrives can't leave the user's token in S3 or the page on "Preparing".

    returns:
        dict: The pending record, as updated if it expired.
    """
    if not is_expired(pending):
        return pending
    log.warning("upload %s expired waiting for its pages", job_id)
    return await _fail(
        job_id, pending, "Preparing took too long. Please upload the file again."
    )


async def discard_pending(job_id: UUID | str, pending: dict) -> None:
    """Forgets an upload that has no coordinator job: its record, token and list entry."""
    await asyncio.to_thread(storage.delete, storage.pending_key(job_id))
    await asyncio.to_thread(
        storage.delete, storage.pending_index_key(pending["owner_id"], job_id)
    )


async def create_job_if_ready(job_id: UUID) -> bool:
    """
    Sends the coordinator every task whose pages are all in S3: the first time
    it creates the job with its final task count, later it adds tasks.

    Safe to call repeatedly and from several callbacks at once: a create that
    finds the job is answered 409 and becomes an add, and the coordinator skips
    task indices it already has. Once every task is sent the pending record and
    its token are deleted.

    returns:
        bool: True once the job has all its tasks.
    raises:
        httpx.HTTPError: If the coordinator can't be reached or fails, so the
            caller can answer 5xx and the Lambda retries.
    """
    pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
    if pending is None or pending.get("state") != "rendering":
        return False
    if is_expired(pending):
        # too late: the user was told to upload again, so don't start the work
        await expire_if_stale(job_id, pending)
        return False
    expected = pending["page_count"]
    plan = jobs.plan_tasks(expected, pending.get("pages_per_task"))
    rendered = await asyncio.to_thread(storage.page_numbers, job_id)
    ready = [
        task
        for task in plan
        if all(page in rendered for page in range(task["first"], task["last"] + 1))
    ]
    if not ready:
        return False
    payload = [jobs.task_payload(task) for task in ready]
    try:
        try:
            # uploads saved before users chose resources fall back to the old fixed ones
            spec = jobs.job_spec(
                str(job_id),
                "ocr",
                pending.get("cpu_request", settings.task_cpu),
                pending.get("mem_request", settings.task_mem_mib),
                expected,
                tasks=payload,
                task_count=len(plan),
                params={
                    "uploaded_at": pending.get("created_at"),
                    "pages_per_task": pending.get("pages_per_task"),
                },
            )
            await coordinator.create_job(pending["token"], spec)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            # an earlier batch (or another web replica) created it: add to it
            await coordinator.add_tasks(pending["token"], job_id, payload)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 409:
            # from add_tasks: the job ended (cancelled, failed), so stop sending
            await discard_pending(job_id, pending)
            return False
        if code == 401:
            await _fail(
                job_id,
                pending,
                "Your session ended before the pages were ready. Upload again.",
            )
            return False
        if code == 429:
            # a retry won't help until another job ends, so don't let the
            # Lambda retry; the user starts it again later
            await _fail(
                job_id,
                pending,
                "You had too many jobs running when the pages were ready. "
                "Upload again once one finishes.",
            )
            return False
        raise
    if len(ready) < len(plan):
        return False  # later batches add the rest
    await discard_pending(job_id, pending)
    return True


async def on_pages_ready(event: PagesReady) -> None:
    """Handles one Lambda callback; see create_job_if_ready for errors."""
    if event.status == "failed":
        pending = await asyncio.to_thread(
            storage.get_json, storage.pending_key(event.job_id)
        )
        if pending is not None and pending.get("state") == "rendering":
            log.warning("page rendering failed for %s: %s", event.job_id, event.error)
            await _fail(
                event.job_id, pending, event.error or "The PDF could not be processed."
            )
        return
    await create_job_if_ready(event.job_id)


def _render_pages(job_id: UUID, data: bytes, first: int, last: int) -> None:
    """Renders pages first..last (numbered from 1) and stores their images."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for number in range(first, last + 1):
            pixmap = doc[number - 1].get_pixmap(
                dpi=LOCAL_RENDER_DPI, colorspace=pymupdf.csGRAY, alpha=False
            )
            storage.put_bytes(
                storage.page_key(job_id, number), pixmap.tobytes("png"), "image/png"
            )


def _page_count(data: bytes) -> int:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc.page_count


async def render_locally(job_id: UUID, data: bytes) -> None:
    """
    Stand-in for the Lambda when running without AWS (LOCAL_RENDER=true). Like
    the Lambda it works in batches, and each batch's tasks start straight away.
    """
    try:
        pages = await asyncio.to_thread(_page_count, data)
        batch = settings.render_batch_pages
        for first in range(1, pages + 1, batch):
            last = min(first + batch - 1, pages)
            await asyncio.to_thread(_render_pages, job_id, data, first, last)
            await create_job_if_ready(job_id)
    except Exception as exc:
        log.exception("local page rendering failed for %s", job_id)
        pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
        if pending is not None:
            await _fail(job_id, pending, f"Pages could not be prepared: {exc}")
