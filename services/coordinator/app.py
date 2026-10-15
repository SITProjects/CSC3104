import asyncio
import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from kubernetes import client, config
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import COORDINATOR_URL, S3_URL
from services.coordinator.crud.jobs import get_job
from services.coordinator.crud.replicas import get_replica_with_task
from services.coordinator.scheduler import scheduler_loop
from shared.db import JobType, JobView, Replica, Task, session_manager
from shared.migrate import run_migrations

from .crud import create_job, create_replicas, create_tasks, ensure_job_type_exists
from .models import (
    Ack,
    CreateJobRequest,
    CreateJobResponse,
    Manifest,
    ProgressIn,
    ResultIn,
)
from .storage import presign

log = logging.getLogger("coordinator")
DbSession = Annotated[AsyncSession, Depends(session_manager.get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations(os.environ["DATABASE_URL"])
    session_manager.init_db(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    try:
        await asyncio.to_thread(config.load_incluster_config)
    except config.ConfigException:
        await asyncio.to_thread(config.load_kube_config)
    batch_v1 = client.BatchV1Api()
    task = asyncio.create_task(
        scheduler_loop(session_manager.session_factory, COORDINATOR_URL, batch_v1)
    )
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await session_manager.close()


app = FastAPI(title="Coordinator", lifespan=lifespan)

S3 = S3_URL


async def require_token(
    session: AsyncSession, replica_id: int, authorization: str | None
) -> tuple[Replica, Task, JobType]:
    """
    Checks the request's REPLICA_TOKEN against the hash stored for the replica.

    args:
        session: The database session for this request.
        replica_id: The id of the replica making the request.
        authorization: The value of the Authorization header, "Bearer <REPLICA_TOKEN>".
    returns:
        Replica: The replica the token belongs to.
    raises:
        HTTPException: 401 if the replica does not exist or the token is missing or wrong.
    """
    res = await get_replica_with_task(session, replica_id)
    if res is None:
        raise HTTPException(401, "Unauthorized")
    replica, task, job_type = res
    token = (authorization or "").removeprefix("Bearer ")
    if (
        not replica
        or not replica.token_hash
        or not hmac.compare_digest(
            hashlib.sha256(token.encode()).hexdigest(), replica.token_hash
        )
    ):
        log.warning("unauthorized request for replica %d", replica_id)
        raise HTTPException(401, "Unauthorized")
    return replica, task, job_type


@app.get("/internal/job/{id}")
async def fetch_job(id: UUID, session: DbSession) -> JobView:
    """
    Returns a job view for the given job id.

    args:
        id: The id of the job to retrieve.
    returns:
        JobView: A job view with the given id
    """
    async with session:
        result = await get_job(session, id)
        if result is None:
            raise HTTPException(404, f"job {id} not found")
        job, progress = result
        view = JobView.model_validate(job, from_attributes=True)
        view.progress = int(progress) if progress is not None else 0
        return view


@app.post("/internal/jobs", status_code=201)
async def create_jobs(req: CreateJobRequest, session: DbSession) -> CreateJobResponse:
    """
    Creates a job with the given request and returns a mock response.

    args:
        req: The request to create the job.
    returns:
        CreateJobResponse: A mock response with the job id and task ids.
    """
    async with session.begin():
        await ensure_job_type_exists(session, req.job_type)
        job = await create_job(session, req)
        tasks = await create_tasks(session, job, req)
        task_ids = [task.id for task in tasks]
        await create_replicas(session, tasks, req.replicas_per_task)
    return CreateJobResponse(
        job_id=job.id,
        task_ids=task_ids,
        replica_count=req.replicas_per_task * len(task_ids),
    )


@app.get("/internal/replicas/{id}/manifest")
async def get_replica_manifest(
    id: int, session: DbSession, authorization: str | None = Header(None)
) -> Manifest:
    """
    Fetches the manifest for the given replica id.

    args:
        id: The id of the replica to fetch the manifest for.
        session: The database session for this request.
        authorization: The value of the Authorization header, "Bearer <REPLICA_TOKEN>".
    returns:
        Manifest: A mock manifest for the given replica id.
    """
    replica, task, job_type = await require_token(session, id, authorization)
    if not task:
        raise HTTPException(404, f"task {replica.task_id} not found")
    prefix = f"jobs/{task.job_id}/replicas/{replica.id}"
    ttl = min(job_type.timeout_seconds + 300, 7 * 24 * 3600)
    return Manifest(
        replica_id=replica.id,
        task_index=task.task_index,
        attempt=replica.attempt,
        input=task.input,
        input_urls={
            f: presign("get_object", f"jobs/{task.job_id}/{f}", ttl)
            for f in task.input.get("files", [])
        },
        output_upload_url=presign("put_object", f"{prefix}/result.json", ttl),
        checkpoint_get_url=presign("get_object", f"{prefix}/checkpoint.json", ttl)
        if replica.attempt > 1
        else None,
        checkpoint_put_url=presign("put_object", f"{prefix}/checkpoint.json", ttl),
    )


@app.post("/internal/replicas/{id}/progress")
async def report_progress(
    id: int,
    body: ProgressIn,
    session: DbSession,
    authorization: str | None = Header(None),
) -> Ack:
    """
    Reports the progress of the given replica id.

    args:
        id: The id of the replica reporting progress.
        session: The database session for this request.
        body: The progress report from the replica.
        authorization: The value of the Authorization header, "Bearer <REPLICA_TOKEN>".
    returns:
        Ack: An acknowledgment of the progress report.
    """
    await require_token(session, id, authorization)
    return Ack()


@app.post("/internal/replicas/{id}/result")
async def report_result(
    id: int,
    body: ResultIn,
    session: DbSession,
    authorization: str | None = Header(None),
) -> Ack:
    """
    Reports the result of the given replica id.

    args:
        id: The id of the replica reporting the result.
        session: The database session for this request.
        body: The result report from the replica.
        authorization: The value of the Authorization header, "Bearer <REPLICA_TOKEN>".
    returns:
        Ack: An acknowledgment of the result report.
    """
    await require_token(session, id, authorization)
    return Ack()


@app.post("/internal/replicas/{id}/evicted")
async def report_evicted(
    id: int, session: DbSession, authorization: str | None = Header(None)
) -> Ack:
    """
    Reports that the given replica has been evicted.

    args:
        id: The id of the replica that has been evicted.
        session: The database session for this request.
        authorization: The value of the Authorization header, "Bearer <REPLICA_TOKEN>".
    returns:
        Ack: An acknowledgment of the evicted report.
    """
    await require_token(session, id, authorization)
    return Ack()


@app.post("/internal/jobs/{id}/cancel")
def cancel_job(id: UUID) -> Ack:
    """
    Cancels the job with the given id.

    args:
        id: The id of the job to cancel.
    returns:
        Ack: An acknowledgment of the cancellation.
    """
    return Ack()


@app.get("/healthz")
def healthcheck() -> Ack:
    """
    TODO: Implement a real health check.

    returns:
        Ack: An acknowledgment of the health check.
    """
    return Ack()
