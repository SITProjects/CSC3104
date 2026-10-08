import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db import (
    session_manager,
)
from shared.migrate import run_migrations

from .crud import create_job, create_replicas, create_tasks, ensure_job_type_exists
from .models import (
    Ack,
    CreateJobRequest,
    CreateJobResponse,
    JobView,
    Manifest,
    ProgressIn,
    ReplicaView,
    ResultIn,
    TaskView,
)

log = logging.getLogger("coordinator")
DbSession = Annotated[AsyncSession, Depends(session_manager.get_session)]


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations(os.environ["DATABASE_URL"])
    session_manager.init_db(
        os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    )
    yield
    await session_manager.close()


app = FastAPI(title="Coordinator", lifespan=lifespan)

HASH = "a" * 64  # TODO: remove this once we have a real hash
S3 = "http://localhost:9000/csc3104-local"


def require_token(replica_id: int, authorization: str | None) -> None:
    """
    Checks that the request has the correct authorization token for the given id.

    args:
        replica_id: The id of the replica making the request.
        authorization: The value of the Authorization header from the request.
    raises:
        HTTPException: If the authorization header is missing or incorrect.
    """
    if authorization != f"Bearer {HASH}":
        log.warning("unauthorized request for replica %d", replica_id)
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/internal/jobs/{id}")
def get_jobs(id: UUID) -> JobView:
    """
    Returns a mock job view for the given job id.

    args:
        id: The id of the job to retrieve.
    returns:
        JobView: A mock job view with the given id
    """
    return JobView(
        id=id,
        job_type="ocr",
        status="RUNNING",
        progress=55,
        tasks=[
            TaskView(
                id=100,
                task_index=0,
                status="VERIFIED",
                final_hash=HASH,
                replicas=[
                    ReplicaView(
                        id=1,
                        node_name="lab-pc-01",
                        status="DONE",
                        attempt=1,
                        progress=100,
                        result_hash=HASH,
                    ),
                    ReplicaView(
                        id=2,
                        node_name="lab-pc-02",
                        status="DONE",
                        attempt=1,
                        progress=100,
                        result_hash=HASH,
                    ),
                    ReplicaView(
                        id=3,
                        node_name="lab-pc-03",
                        status="DONE",
                        attempt=2,
                        progress=100,
                        result_hash=HASH,
                    ),
                ],
            ),
            TaskView(
                id=101,
                task_index=1,
                status="RUNNING",
                final_hash=None,
                replicas=[
                    ReplicaView(
                        id=4,
                        node_name="lab-pc-01",
                        status="RUNNING",
                        attempt=1,
                        progress=40,
                        result_hash=None,
                    ),
                    ReplicaView(
                        id=5,
                        node_name="lab-pc-04",
                        status="EVICTED",
                        attempt=1,
                        progress=20,
                        result_hash=None,
                    ),
                    ReplicaView(
                        id=6,
                        node_name=None,
                        status="SCHEDULED",
                        attempt=1,
                        progress=0,
                        result_hash=None,
                    ),
                ],
            ),
        ],
    )


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
def get_replica_manifest(id: int, authorization: str | None = Header(None)) -> Manifest:
    """
    Fetches the manifest for the given replica id.

    args:
        id: The id of the replica to fetch the manifest for.
        authorization: The value of the Authorization header from the request.
    returns:
        Manifest: A mock manifest for the given replica id.
    """
    require_token(id, authorization)
    return Manifest(
        replica_id=id,
        task_index=id % 2,
        input={"pages": [1, 10]},
        input_urls={"input.pdf": f"{S3}/input.pdf"},
        output_upload_url=f"{S3}/replicas/{id}/result.json",
        checkpoint_get_url=None if id % 2 == 0 else f"{S3}/checkpoint.json",
        checkpoint_put_url=f"{S3}/replicas/{id}/checkpoint.json",
        attempt=1,
    )


@app.post("/internal/replicas/{id}/progress")
def report_progress(
    id: int, body: ProgressIn, authorization: str | None = Header(None)
) -> Ack:
    """
    Reports the progress of the given replica id.

    args:
        id: The id of the replica reporting progress.
        body: The progress report from the replica.
        authorization: The value of the Authorization header from the request.
    returns:
        Ack: An acknowledgment of the progress report.
    """
    require_token(id, authorization)
    return Ack()


@app.post("/internal/replicas/{id}/result")
def report_result(
    id: int, body: ResultIn, authorization: str | None = Header(None)
) -> Ack:
    """
    Reports the result of the given replica id.

    args:
        id: The id of the replica reporting the result.
        body: The result report from the replica.
        authorization: The value of the Authorization header from the request.
    returns:
        Ack: An acknowledgment of the result report.
    """
    require_token(id, authorization)
    return Ack()


@app.post("/internal/replicas/{id}/evicted")
def report_evicted(id: int, authorization: str | None = Header(None)) -> Ack:
    """
    Reports that the given replica has been evicted.

    args:
        id: The id of the replica that has been evicted.
        authorization: The value of the Authorization header from the request.
    returns:
        Ack: An acknowledgment of the evicted report.
    """
    require_token(id, authorization)
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
