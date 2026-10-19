import asyncio
import hashlib
import hmac
import logging
import os
import threading
from contextlib import asynccontextmanager, suppress
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from kubernetes import client, config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import (
    COORDINATOR_URL,
    HEARTBEAT_AUDIENCE,
    HEARTBEAT_SERVICE_ACCOUNTS,
)
from services.coordinator.crud import (
    cancel_job,
    create_event,
    create_job,
    create_replicas,
    create_tasks,
    ensure_job_type_exists,
    evict_replica,
    get_job,
    get_replica_with_task,
    mark_replica_done,
    set_voting,
    update_node_state,
)
from services.coordinator.k8s import cancel_k3_job, review_token
from services.coordinator.loops import housekeeping_loop, scheduler_loop, watch_pods
from services.coordinator.models import (
    Ack,
    CreateJobRequest,
    CreateJobResponse,
    HeartbeatIn,
    Manifest,
    ProgressIn,
    ResultIn,
)
from services.coordinator.storage import presign
from shared.db import (
    JobType,
    JobView,
    Replica,
    ReplicaStatus,
    Task,
    session_manager,
)
from shared.migrate import run_migrations

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("coordinator")
DbSession = Annotated[AsyncSession, Depends(session_manager.get_session)]
K3API = Annotated[client.BatchV1Api, Depends(lambda: client.BatchV1Api())]
AuthAPI = Annotated[
    client.AuthenticationV1Api, Depends(lambda: client.AuthenticationV1Api())
]


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
    core_v1 = client.CoreV1Api()
    scheduler = asyncio.create_task(
        scheduler_loop(session_manager.session_factory, COORDINATOR_URL, batch_v1)
    )
    housekeeping = asyncio.create_task(
        housekeeping_loop(session_manager.session_factory, batch_v1, core_v1)
    )
    stop = threading.Event()
    watcher = threading.Thread(
        target=watch_pods,
        args=(
            asyncio.get_running_loop(),
            session_manager.session_factory,
            core_v1,
            stop,
        ),
        name="pod_watcher",
        daemon=True,
    )
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        scheduler.cancel()
        housekeeping.cancel()
        for task in (scheduler, housekeeping):
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(watcher.join, 2)
        await session_manager.close()


app = FastAPI(title="Coordinator", lifespan=lifespan)


async def require_token(
    session: AsyncSession,
    replica_id: int,
    authorization: str | None,
    lock: bool = False,
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
    res = await get_replica_with_task(session, replica_id, lock)
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
        log.exception("unauthorized request for replica %d", replica_id)
        raise HTTPException(401, "Unauthorized")
    return replica, task, job_type


@app.get("/internal/jobs/{id}")
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
        Manifest: The manifest with presigned URLs for this replica's current attempt.
    raises:
        HTTPException: 401 if the token is missing or wrong.
    """
    replica, task, job_type = await require_token(session, id, authorization)
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
    async with session.begin():
        replica, _, _ = await require_token(session, id, authorization, True)
        if replica.status == ReplicaStatus.DONE:
            raise HTTPException(409, f"replica {id} is DONE, cannot report progress")
        else:
            replica.progress = body.progress
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
    async with session.begin():
        replica, task, _ = await require_token(session, id, authorization, True)
        if replica.status != ReplicaStatus.RUNNING:
            raise HTTPException(
                409,
                f"replica {id} status is {replica.status.value}, cannot report result",
            )
        else:
            await mark_replica_done(
                session,
                replica,
                task,
                body.result_hash,
                body.exit_code,
            )
            await set_voting(session, task)
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
    async with session.begin():
        replica, task, _ = await require_token(session, id, authorization, True)
        if replica.status != ReplicaStatus.RUNNING:
            raise HTTPException(
                409, f"replica {id} is {replica.status.value}, not RUNNING"
            )
        node_name = replica.node_name
        await evict_replica(session, replica)
        await create_event(
            session,
            "replica_evicted",
            task.job_id,
            task.id,
            node_name,
            {
                "replica_id": replica.id,
                "attempt": replica.attempt,
                "node_name": node_name,
                "message": "replica was evicted from its node",
            },
        )
        await set_voting(session, task)
    return Ack()


@app.post("/internal/jobs/{id}/cancel")
async def cancel_job_request(id: UUID, session: DbSession, k3_api: K3API) -> Ack:
    """
    Cancels the job with the given id.

    args:
        id: The id of the job to cancel.
    returns:
        Ack: An acknowledgment of the cancellation.
    """
    async with session.begin():
        cancelled_replicas = await cancel_job(session, id)
    names = [
        f"replica-{replica_id}-a{attempt}"
        for replica_id, attempt in cancelled_replicas or []
    ]
    results = await asyncio.gather(
        *[cancel_k3_job(name, k3_api) for name in names], return_exceptions=True
    )
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            log.exception("failed to cancel K8s job %s: %s", name, result)
    return Ack()


async def require_node_token(
    name: str, authorization: str | None, auth_api: client.AuthenticationV1Api
) -> None:
    """
    Checks that a heartbeat comes from an allowed service account on that node.

    The token is a projected service-account token for HEARTBEAT_AUDIENCE. A
    TokenReview tells us its service account and the node its Pod runs on.

    args:
        name: The node named in the URL.
        authorization: The value of the Authorization header, "Bearer <token>".
        auth_api: The Kubernetes authentication API.
    raises:
        HTTPException: 401 if the token is missing or invalid, 403 if it belongs
            to another service account or another node, 503 if the API server
            cannot be asked.
    """
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "Unauthorized")
    try:
        user = await review_token(auth_api, token, HEARTBEAT_AUDIENCE)
    except client.ApiException:
        log.exception("token review failed")
        raise HTTPException(503, "cannot verify token")
    if user is None:
        log.warning("invalid heartbeat token for node %s", name)
        raise HTTPException(401, "Unauthorized")
    nodes = (user.extra or {}).get("authentication.kubernetes.io/node-name") or []
    if user.username not in HEARTBEAT_SERVICE_ACCOUNTS or nodes != [name]:
        log.warning(
            "heartbeat for node %s rejected: %s on %s", name, user.username, nodes
        )
        raise HTTPException(403, "Forbidden")


@app.post("/internal/nodes/{name}/heartbeat")
async def node_heartbeat(
    name: str,
    body: HeartbeatIn,
    session: DbSession,
    auth_api: AuthAPI,
    authorization: str | None = Header(None),
) -> Ack:
    """
    Records a node's state and CPU use, and when it last reported.

    args:
        name: The name of the node sending the heartbeat.
        body: The node's state and CPU use.
        session: The database session for this request.
        auth_api: The Kubernetes authentication API, for the TokenReview.
        authorization: The value of the Authorization header, "Bearer <token>".
    returns:
        Ack: An acknowledgment of the heartbeat.
    """
    await require_node_token(name, authorization, auth_api)
    async with session.begin():
        await update_node_state(session, name, body.state, body.cpu_percent)
    return Ack()


@app.get("/healthz")
async def healthcheck(session: DbSession) -> Ack:
    """
    healthcheck endpoint for the coordinator service.
    """
    try:
        await asyncio.wait_for(session.execute(select(1)), timeout=2)
    except Exception:
        log.exception("healthcheck failed")
        raise HTTPException(503, "database unavailable")
    return Ack()
