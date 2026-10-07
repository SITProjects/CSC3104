import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from shared.migrate import run_migrations

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    applied = run_migrations(os.environ["DATABASE_URL"])
    log.info("migrations applied: %s", applied or "none (up to date)")
    yield


app = FastAPI(title="Coordinator", lifespan=lifespan)

HASH = "a" * 64  # TODO: remove this once we have a real hash
S3 = "http://localhost:9000/csc3104-local"


def require_token(replica_id: int, authorization: str | None) -> None:
    if authorization != f"Bearer {HASH}":
        log.warning("unauthorized request for replica %d", replica_id)
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/internal/jobs/{id}")
def get_jobs(id: int) -> JobView:
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
def create_jobs(req: CreateJobRequest) -> CreateJobResponse:
    task_ids = [100 + i for i in range(len(req.tasks))]
    return CreateJobResponse(
        job_id=req.job_id, task_ids=task_ids, replica_count=3 * len(task_ids)
    )


@app.get("/internal/replicas/{id}/manifest")
def get_replica_manifest(id: int, authorization: str | None = Header(None)) -> Manifest:
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
    require_token(id, authorization)
    return Ack()


@app.post("/internal/replicas/{id}/result")
def report_result(
    id: int, body: ResultIn, authorization: str | None = Header(None)
) -> Ack:
    require_token(id, authorization)
    return Ack()


@app.post("/internal/replicas/{id}/evicted")
def report_evicted(id: int, authorization: str | None = Header(None)) -> Ack:
    require_token(id, authorization)
    return Ack()


@app.post("/internal/jobs/{id}/cancel")
def cancel_job(id: int) -> Ack:
    return Ack()


@app.get("/healthz")
def healthcheck() -> Ack:
    return Ack()
