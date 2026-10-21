import asyncio
import logging
from datetime import UTC, datetime, timedelta

from kubernetes.client import BatchV1Api, CoreV1Api, V1Job, V1Pod
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import HOUSEKEEPING_INTERVAL
from services.coordinator.crud import (
    create_event,
    fetch_running_replicas,
    lock_stale_replica_job,
    mark_lost,
    mark_offline,
)
from services.coordinator.crud.users import delete_expired_sessions
from services.coordinator.k8s import (
    cancel_k3_job,
    cancel_k3_pod,
    list_replica_jobs,
    list_replica_pods,
)

log = logging.getLogger("coordinator.housekeeping")
LAUNCH_GRACE = timedelta(minutes=2)
HEARTBEAT_GRACE = timedelta(seconds=30)


def replica_key(resource: V1Job | V1Pod) -> tuple[int, int] | None:
    """Ignore malformed resources instead of aborting reconciliation."""
    labels = resource.metadata.labels if resource.metadata else None
    try:
        return int((labels or {})["idlecloud/replica-id"]), int(
            (labels or {})["idlecloud/attempt"]
        )
    except (KeyError, TypeError, ValueError):
        log.warning(
            "Skipping resource with invalid replica labels: %s", resource.metadata
        )
        return None


async def housekeeping_pass(
    session_factory: async_sessionmaker[AsyncSession],
    batch_api: BatchV1Api,
    core_api: CoreV1Api,
) -> None:
    now = datetime.now(UTC)
    async with session_factory.begin() as session:
        await mark_offline(session, now - HEARTBEAT_GRACE)
        await delete_expired_sessions(session)
        # Snapshot before listing Kubernetes resources so new launches are excluded.
        replicas = await fetch_running_replicas(session, now - LAUNCH_GRACE)

    jobs, pods = await asyncio.gather(
        list_replica_jobs(batch_api), list_replica_pods(core_api)
    )
    jobs_by_replica = {
        key: job for job in jobs if (key := replica_key(job)) is not None
    }
    pod_keys = {key for pod in pods if (key := replica_key(pod)) is not None}

    for replica, job_id in replicas:
        key = (replica.id, replica.attempt)
        if key in jobs_by_replica and key in pod_keys:
            continue
        async with session_factory.begin() as session:
            if not await mark_lost(session, replica.id, replica.attempt):
                continue
            log.info(
                "Replica %s/%s is lost; job %s, node %s",
                replica.id,
                replica.attempt,
                job_id,
                replica.node_name,
            )
            await create_event(
                session,
                "replica_lost",
                job_id,
                replica.task_id,
                replica.node_name,
                {
                    "replica_id": replica.id,
                    "attempt": replica.attempt,
                    "node_name": replica.node_name,
                    "message": "JobMissing"
                    if key not in jobs_by_replica
                    else "PodMissing",
                },
            )

    for (replica_id, attempt), job in jobs_by_replica.items():
        if job.metadata is None:
            continue
        if not job.metadata.name or job.metadata.deletion_timestamp is not None:
            continue
        async with session_factory.begin() as session:
            if await lock_stale_replica_job(session, replica_id, attempt):
                await cancel_k3_job(job.metadata.name, batch_api)

    for pod in pods:
        key = replica_key(pod)
        if key is None or key in jobs_by_replica:
            continue
        meta = pod.metadata
        if meta is None or not meta.name or meta.deletion_timestamp is not None:
            continue
        # A dangling Job owner reference can remain after the Job disappears.
        # Recheck the replica under lock, just as for stale Job cleanup.
        async with session_factory.begin() as session:
            if await lock_stale_replica_job(session, *key):
                await cancel_k3_pod(meta.name, core_api, meta.uid)


async def housekeeping_loop(
    session_factory: async_sessionmaker[AsyncSession],
    batch_api: BatchV1Api,
    core_api: CoreV1Api,
) -> None:
    """Reconcile missing replicas, stale Jobs/Pods and node heartbeats."""
    while True:
        try:
            await housekeeping_pass(session_factory, batch_api, core_api)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Housekeeping pass failed")
        await asyncio.sleep(HOUSEKEEPING_INTERVAL)
