"""
Page splitting inside the web app, for docker compose where there is no Lambda.

Uses the renderer's own code, so pages come out byte for byte the same, and
feeds each batch through the same pages_ready handler as the real callback.
"""

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from serverless.common.errors import BadPdfError
from serverless.common.pdf_pages import count_pages, render_pages
from serverless.splitter.chunks import make_chunks
from services.web import storage
from services.web.coordinator import CoordinatorClient
from services.web.jobs import pages_ready

log = logging.getLogger("web.local_split")


def _render_batch(job_id: UUID, pdf: bytes, start: int, end: int) -> None:
    for page, png in render_pages(pdf, start, end):
        storage.put_object(f"jobs/{job_id}/{storage.page_name(page)}", png, "image/png")


async def _split(
    session_factory: async_sessionmaker[AsyncSession],
    coordinator: CoordinatorClient,
    job_id: UUID,
    pdf: bytes,
) -> None:
    try:
        page_count = await asyncio.to_thread(count_pages, pdf)
        batches = make_chunks(page_count)
    except BadPdfError as error:
        async with session_factory() as session:
            await pages_ready(
                session,
                coordinator,
                {"status": "failed", "job_id": str(job_id), "error": str(error)},
            )
        return
    for start, end in batches:
        try:
            await asyncio.to_thread(_render_batch, job_id, pdf, start, end)
            payload = {
                "status": "ok",
                "job_id": str(job_id),
                "page_count": page_count,
                "page_start": start,
                "page_end": end,
            }
        except Exception as error:
            log.exception("job %s: pages %d-%d failed to render", job_id, start, end)
            payload = {
                "status": "failed",
                "job_id": str(job_id),
                "error": f"pages {start}-{end} could not be rendered: {error}",
            }
        async with session_factory() as session:
            await pages_ready(session, coordinator, payload)
        if payload["status"] == "failed":
            return


async def split_locally(
    session_factory: async_sessionmaker[AsyncSession],
    coordinator: CoordinatorClient,
    job_id: UUID,
    pdf: bytes,
) -> None:
    """
    Splits the PDF in the background. Never raises: a crash fails the upload,
    because nothing else would ever move it out of SPLITTING.
    """
    try:
        await _split(session_factory, coordinator, job_id, pdf)
    except Exception:
        log.exception("job %s: local split crashed", job_id)
        failure = {
            "status": "failed",
            "job_id": str(job_id),
            "error": "Splitting the PDF failed. Try again.",
        }
        try:
            async with session_factory() as session:
                await pages_ready(session, coordinator, failure)
        except Exception:
            log.exception("job %s: could not mark the upload failed", job_id)
