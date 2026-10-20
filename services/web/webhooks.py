"""Optional user webhooks: one signed POST when a job ends (member 5, step 5)."""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from serverless.common.signing import signed_headers
from services.web.jobs import UserError
from services.web.models import Upload, UploadStatus
from services.web.settings import (
    HMAC_SECRET,
    PUBLIC_URL,
    WEBHOOK_CONCURRENCY,
    WEBHOOK_INTERVAL,
    WEBHOOK_MAX_ATTEMPTS,
    WEBHOOK_TIMEOUT,
)
from shared.db import Job, JobStatus

log = logging.getLogger("web.webhooks")


def check_webhook_url(url: str) -> str:
    """
    Checks a webhook URL points at a public host. The web app runs inside the
    cluster, so without this a user could make it POST to the coordinator,
    Postgres or the EC2 metadata service.

    Blocking (it resolves DNS), so call it in a thread.

    raises:
        UserError: 400 if the URL is malformed or resolves to a non-public address.
    """
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port  # raises ValueError for a port that is not 0-65535
    except ValueError as error:
        raise UserError(400, "The webhook URL is not valid.") from error
    if parts.scheme not in ("http", "https") or not parts.hostname or len(url) > 2000:
        raise UserError(400, "The webhook must be an http or https URL.")
    try:
        infos = socket.getaddrinfo(parts.hostname, port or 443)
    except (socket.gaierror, UnicodeError) as error:
        raise UserError(400, "The webhook host cannot be resolved.") from error
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise UserError(400, "The webhook must point at a public address.")
    return url


def webhook_secret(owner_email: str) -> str:
    """
    The key that signs one owner's webhooks, or "" if the server has no
    HMAC_SECRET. Derived rather than stored, and never the Lambda callback key
    itself, which would let a webhook receiver forge pages-ready callbacks.
    """
    if not HMAC_SECRET:
        return ""
    message = f"webhook:{owner_email}".encode()
    return hmac.new(HMAC_SECRET.encode(), message, hashlib.sha256).hexdigest()


def webhook_payload(job_id: UUID, status: str) -> dict[str, str]:
    payload = {"job_id": str(job_id), "status": status}
    if PUBLIC_URL:
        payload["status_url"] = f"{PUBLIC_URL}/jobs/{job_id}"
        if status == "COMPLETED":
            payload["result_url"] = f"{PUBLIC_URL}/api/jobs/{job_id}/result"
    return payload


async def deliver(
    http: httpx.AsyncClient, url: str, payload: dict[str, Any], secret: str
) -> bool:
    """
    Sends one webhook, signed with secret if there is one (the same scheme as
    the pages-ready callback: X-Idlecloud-Timestamp and X-Idlecloud-Signature).

    returns:
        bool: True once the receiver answered with a 2xx.
    """
    body = json.dumps(payload).encode()
    headers = signed_headers(secret, body) if secret else {}
    try:
        # Checked again: the DNS answer may have changed since submission
        await asyncio.to_thread(check_webhook_url, url)
        response = await http.post(
            url, content=body, headers={"Content-Type": "application/json"} | headers
        )
    except (UserError, httpx.HTTPError) as error:
        log.warning("webhook for job %s failed: %s", payload["job_id"], error)
        return False
    log.info("webhook for job %s: %s", payload["job_id"], response.status_code)
    return response.is_success


async def send_due(session: AsyncSession, http: httpx.AsyncClient) -> int:
    """
    Sends every webhook whose job has ended. A delivery that fails is put back
    and tried again on the next pass, up to WEBHOOK_MAX_ATTEMPTS failures.

    returns:
        int: How many were claimed.
    """
    ended = [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
    async with session.begin():
        # Claiming with one UPDATE means two web pods never send the same webhook
        claimed = (
            await session.execute(
                update(Upload)
                .where(
                    Upload.webhook_url.is_not(None),
                    Upload.webhook_sent_at.is_(None),
                    Upload.webhook_attempts < WEBHOOK_MAX_ATTEMPTS,
                    or_(
                        Upload.status.in_(
                            [UploadStatus.FAILED.value, UploadStatus.CANCELLED.value]
                        ),
                        and_(
                            Upload.status == UploadStatus.SUBMITTED.value,
                            exists().where(
                                Job.id == Upload.job_id, Job.status.in_(ended)
                            ),
                        ),
                    ),
                )
                .values(webhook_sent_at=func.now())
                .returning(
                    Upload.job_id, Upload.owner_email, Upload.webhook_url, Upload.status
                )
            )
        ).all()
        job_status = dict(
            (
                await session.execute(
                    select(Job.id, Job.status).where(
                        Job.id.in_([row.job_id for row in claimed])
                    )
                )
            ).all()
        )

    gate = asyncio.Semaphore(WEBHOOK_CONCURRENCY)

    async def send(row: Any) -> bool:
        status = (
            job_status[row.job_id].value if row.job_id in job_status else row.status
        )
        async with gate:
            return await deliver(
                http,
                row.webhook_url,
                webhook_payload(row.job_id, status),
                webhook_secret(row.owner_email),
            )

    # One slow receiver must not hold up the others
    results = await asyncio.gather(
        *(send(row) for row in claimed), return_exceptions=True
    )
    failed = []
    for row, result in zip(claimed, results, strict=True):
        if isinstance(result, BaseException):  # a bug, not a refusal: still retried
            log.error("webhook for job %s crashed", row.job_id, exc_info=result)
        if result is not True:
            failed.append(row.job_id)
    if failed:
        async with session.begin():
            await session.execute(
                update(Upload)
                .where(Upload.job_id.in_(failed))
                .values(
                    webhook_sent_at=None, webhook_attempts=Upload.webhook_attempts + 1
                )
            )
    return len(claimed)


async def webhook_loop(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with httpx.AsyncClient(
        timeout=WEBHOOK_TIMEOUT, follow_redirects=False
    ) as http:
        while True:
            try:
                async with session_factory() as session:
                    await send_due(session, http)
            except Exception:
                log.exception("webhook loop failed")
            await asyncio.sleep(WEBHOOK_INTERVAL)
