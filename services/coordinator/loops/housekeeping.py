import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from kubernetes.client import BatchV1Api, CoreV1Api, V1Job, V1Node, V1Pod
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import HOUSEKEEPING_INTERVAL, PLACEMENT_TIMEOUT
from services.coordinator.capacity import WORKER_SELECTOR, usable
from services.coordinator.crud import (
    create_event,
    fail_unplaced,
    fetch_running_replicas,
    lock_stale_replica_job,
    mark_lost,
    mark_offline,
    set_voting,
)
from services.coordinator.crud.users import delete_expired_sessions
from services.coordinator.k8s import (
    cancel_k3_job,
    cancel_k3_pod,
    list_replica_jobs,
    list_replica_pods,
)
from shared.db.models import Task

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


@dataclass(frozen=True)
class Unplaceable:
    replica_id: int
    attempt: int
    job_name: str
    avoided: list[str]
    waited_s: float


def avoided_nodes(pod: V1Pod) -> set[str]:
    """The nodes a task Pod must stay off: the NotIn names k8s.py gives it."""
    affinity = pod.spec.affinity if pod.spec else None
    node_affinity = affinity.node_affinity if affinity else None
    required = (
        node_affinity.required_during_scheduling_ignored_during_execution
        if node_affinity
        else None
    )
    names = set()
    for term in (required.node_selector_terms or []) if required else []:
        for field in term.match_fields or []:
            if field.key == "metadata.name" and field.operator == "NotIn":
                names.update(field.values or [])
    return names


def unplaceable_pods(
    pods: list[V1Pod], nodes: list[V1Node], now: datetime, timeout: float
) -> list[Unplaceable]:
    """
    Task Pods that have waited too long for a node no usable worker can be.

    A copy is kept off the PCs its siblings used, so with fewer usable workers
    than copies (or after one was lost) it can never be scheduled. A Pod that
    only waits for room on an allowed worker is left to wait.

    args:
        pods: Task Pods.
        nodes: Worker nodes.
        now: The current time.
        timeout: Seconds a Pod may stay Unschedulable with no allowed worker.
    returns:
        list[Unplaceable]: The Pods to give up on.
    """
    usable_names = {n.metadata.name for n in nodes if usable(n)}
    stuck = []
    for pod in pods:
        meta, status = pod.metadata, pod.status
        if (
            meta is None
            or status is None
            or status.phase != "Pending"
            or (pod.spec and pod.spec.node_name)
            or meta.deletion_timestamp is not None
            or (key := replica_key(pod)) is None
        ):
            continue
        avoided = avoided_nodes(pod)
        if not avoided or usable_names - avoided:
            continue
        condition = next(
            (c for c in status.conditions or [] if c.type == "PodScheduled"), None
        )
        if condition is not None and not (
            condition.status == "False" and condition.reason == "Unschedulable"
        ):
            continue
        since = (
            condition.last_transition_time if condition else None
        ) or meta.creation_timestamp
        job_name = (meta.labels or {}).get("batch.kubernetes.io/job-name")
        if since is None or job_name is None:
            continue
        waited = (now - since).total_seconds()
        if waited >= timeout:
            stuck.append(Unplaceable(*key, job_name, sorted(avoided), waited))
    return stuck


async def fail_unplaceable(
    session_factory: async_sessionmaker[AsyncSession],
    batch_api: BatchV1Api,
    core_api: CoreV1Api,
    pods: list[V1Pod],
    now: datetime,
) -> None:
    """Fails copies no allowed worker can take, so their tasks never hang."""
    nodes = await asyncio.to_thread(
        core_api.list_node, label_selector=WORKER_SELECTOR, _request_timeout=10
    )
    for stuck in unplaceable_pods(pods, nodes.items or [], now, PLACEMENT_TIMEOUT):
        async with session_factory.begin() as session:
            task_id = await fail_unplaced(session, stuck.replica_id, stuck.attempt)
            task = await session.get(Task, task_id) if task_id is not None else None
            if task is None:
                continue
            log.info(
                "Replica %s/%s failed: no usable worker outside %s after %.0f s",
                stuck.replica_id,
                stuck.attempt,
                stuck.avoided,
                stuck.waited_s,
            )
            await create_event(
                session,
                "replica_unplaceable",
                task.job_id,
                task.id,
                None,
                {
                    "replica_id": stuck.replica_id,
                    "attempt": stuck.attempt,
                    "node_name": None,
                    "avoided_nodes": stuck.avoided,
                    "waited_s": round(stuck.waited_s),
                    "message": "NoAllowedWorker",
                },
            )
            await set_voting(session, task)
        await cancel_k3_job(stuck.job_name, batch_api)


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

    await fail_unplaceable(session_factory, batch_api, core_api, pods, now)


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
