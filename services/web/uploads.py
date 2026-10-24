"""From an uploaded PDF to a coordinator job.

The upload is saved to S3, which starts the Lambda page splitter. Each Lambda
batch calls back /hooks/pages-ready, and once every page image exists the web app
creates the job. The coordinator's POST /jobs needs the uploader's bearer token,
so it is kept in jobs/{id}/pending.json (private bucket) until the job is made.

There is no in-memory state, so it works with more than one web replica and
survives a restart: "are all pages there?" is answered by listing S3.
"""

import asyncio
import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import httpx
import pymupdf
from pydantic import BaseModel

from services.web import jobs, storage
from services.web.coordinator import coordinator

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
    job_id: UUID, owner_id: str, token: str, filename: str, data: bytes, pages: int
) -> None:
    """
    Stores the PDF so the page splitter starts.

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


async def _fail(job_id: UUID, pending: dict, message: str) -> None:
    # the token is no longer needed, so don't keep it
    pending = {**pending, "state": "failed", "error": message}
    pending.pop("token", None)
    await asyncio.to_thread(storage.put_json, storage.pending_key(job_id), pending)


async def create_job_if_ready(job_id: UUID) -> bool:
    """
    Creates the coordinator job once every page image is in S3.

    Safe to call repeatedly and from several callbacks at once: a second create
    is answered 409 by the coordinator and counts as done.

    returns:
        bool: True if the job now exists.
    raises:
        httpx.HTTPError: If the coordinator can't be reached or fails, so the
            caller can answer 5xx and the Lambda retries.
    """
    pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
    if pending is None or pending.get("state") != "rendering":
        return False
    expected = pending["page_count"]
    if await asyncio.to_thread(storage.count_pages, job_id) < expected:
        return False
    try:
        await coordinator.create_job(
            pending["token"], jobs.job_spec(str(job_id), expected)
        )
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 401:
            await _fail(
                job_id,
                pending,
                "Your session ended before the pages were ready. Upload again.",
            )
            return False
        if code != 409:  # 409: another callback got there first
            raise
    await asyncio.to_thread(storage.delete, storage.pending_key(job_id))
    await asyncio.to_thread(
        storage.delete, storage.pending_index_key(pending["owner_id"], job_id)
    )
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


def _render_pages(job_id: UUID, data: bytes) -> None:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for number, page in enumerate(doc, start=1):
            pixmap = page.get_pixmap(
                dpi=LOCAL_RENDER_DPI, colorspace=pymupdf.csGRAY, alpha=False
            )
            storage.put_bytes(
                storage.page_key(job_id, number), pixmap.tobytes("png"), "image/png"
            )


async def render_locally(job_id: UUID, data: bytes) -> None:
    """Stand-in for the Lambda when running without AWS (LOCAL_RENDER=true)."""
    try:
        await asyncio.to_thread(_render_pages, job_id, data)
        await create_job_if_ready(job_id)
    except Exception as exc:
        log.exception("local page rendering failed for %s", job_id)
        pending = await asyncio.to_thread(storage.get_json, storage.pending_key(job_id))
        if pending is not None:
            await _fail(job_id, pending, f"Pages could not be prepared: {exc}")
