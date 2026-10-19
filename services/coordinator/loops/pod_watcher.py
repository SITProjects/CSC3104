import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, cast

from kubernetes import watch
from kubernetes.client import (
    ApiException,
    CoreV1Api,
    V1Node,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
)
from pydantic.dataclasses import dataclass
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core.settings import JOB_NAMESPACE
from services.coordinator.crud import (
    create_event,
    ensure_node,
    handle_replica_completed,
    record_placement,
    set_voting,
)
from shared.db.models import ReplicaStatus, Task

SELECTOR = "app.kubernetes.io/managed-by=idlecloud-coordinator"
log = logging.getLogger("coordinator.pod_watcher")


@dataclass
class PodUpdate:
    replica_id: int
    attempt: int
    node: str | None
    started_at: datetime | None
    outcome: tuple[ReplicaStatus, str] | None
    exit_code: int | None


EVENT_TYPES = {
    ReplicaStatus.EVICTED: "replica_evicted",
    ReplicaStatus.LOST: "replica_lost",
    ReplicaStatus.FAILED: "replica_failed",
}


def watch_pods(loop, session_factory, v1, stop: threading.Event):
    while not stop.is_set():
        w = watch.Watch()
        try:
            pods = cast(
                V1PodList,
                v1.list_namespaced_pod(JOB_NAMESPACE, label_selector=SELECTOR),
            )
            for pod in pods.items or []:
                submit(loop, session_factory, to_update(v1, "ADDED", pod))
            resource_version = (
                pods.metadata.resource_version if pods.metadata else None
            )
            for event in w.stream(
                v1.list_namespaced_pod,
                JOB_NAMESPACE,
                label_selector=SELECTOR,
                resource_version=resource_version,
                timeout_seconds=60,
            ):
                ev = cast(dict[str, Any], event)
                submit(
                    loop,
                    session_factory,
                    to_update(v1, ev["type"], cast(V1Pod, ev["object"])),
                )
                if stop.is_set():
                    break
        except ApiException as e:
            # 410 means the resource_version expired: list again straight away
            if e.status != 410:
                log.exception("pod watch failed")
                stop.wait(5)
        except Exception:
            log.exception("pod watch failed")
            stop.wait(5)
        finally:
            w.stop()


def node_is_ready(v1: CoreV1Api, name: str) -> bool:
    """
    Whether the node's Ready condition is True, so a disrupted Pod on it was
    evicted rather than lost with its node.
    """
    try:
        node = cast(V1Node, v1.read_node(name))
    except ApiException:
        return False  # the node object is already gone
    conditions = (node.status.conditions if node.status else None) or []
    return any(c.type == "Ready" and c.status == "True" for c in conditions)


def to_update(v1: CoreV1Api, kind: str, pod: V1Pod) -> PodUpdate | None:
    if pod.metadata is None or pod.spec is None or pod.status is None:
        return
    meta: V1ObjectMeta = pod.metadata
    spec: V1PodSpec = pod.spec
    status: V1PodStatus = pod.status
    labels = meta.labels or {}
    replica_id = int(labels["idlecloud/replica-id"])
    node = spec.node_name
    phase = status.phase
    attempt = int(labels["idlecloud/attempt"])
    reason = status.reason
    started = status.start_time
    conditions = {c.type: c for c in (status.conditions or [])}
    disruption = conditions.get("DisruptionTarget")
    disruption_reason = (
        disruption.reason if disruption and disruption.status == "True" else None
    )
    exit_code = None
    for cs in status.container_statuses or []:
        if cs.name == "worker" and cs.state and cs.state.terminated:
            exit_code = cs.state.terminated.exit_code

    return PodUpdate(
        replica_id=replica_id,
        attempt=attempt,
        node=node,
        started_at=started,
        outcome=decide(
            kind,
            phase,
            reason,
            disruption_reason,
            node_is_ready(v1, node) if disruption_reason and node else True,
        ),
        exit_code=exit_code,
    )


def decide(
    kind: str,
    phase: str | None,
    reason: str | None,
    disruption_reason: str | None,
    node_ready: bool,
) -> tuple[ReplicaStatus, str] | None:
    if disruption_reason:
        return (
            ReplicaStatus.EVICTED if node_ready else ReplicaStatus.LOST
        ), disruption_reason
    match phase:
        case "Failed":
            return ReplicaStatus.FAILED, reason or "PodFailed"
        case "Succeeded":
            return ReplicaStatus.FAILED, "ExitWithoutResult"
        case _:
            if kind == "DELETED":
                return ReplicaStatus.LOST, "PodDeleted"
    return None


async def apply(
    session_factory: async_sessionmaker[AsyncSession],
    pod_update: PodUpdate,
) -> None:
    async with session_factory.begin() as session:
        if pod_update.node:
            await ensure_node(session, pod_update.node)
            await record_placement(
                session,
                pod_update.replica_id,
                pod_update.attempt,
                pod_update.node,
                pod_update.started_at,
            )
        if pod_update.outcome is None:
            return
        status, cause = pod_update.outcome
        changed = await handle_replica_completed(
            session,
            pod_update.replica_id,
            pod_update.attempt,
            status,
            pod_update.exit_code,
        )
        if changed is None:
            return
        task_id, node_name = changed
        task = await session.get(Task, task_id)
        if task is None:
            raise ValueError(f"task {task_id} not found")
        await create_event(
            session,
            EVENT_TYPES[status],
            task.job_id,
            task.id,
            node_name,
            {
                "replica_id": pod_update.replica_id,
                "attempt": pod_update.attempt,
                "node_name": node_name,
                "message": cause,
                "exit_code": pod_update.exit_code,
            },
        )
        if status == ReplicaStatus.FAILED:
            await set_voting(session, task)


def submit(loop, session_factory, pod_update: PodUpdate | None) -> None:
    if pod_update is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            apply(session_factory, pod_update), loop
        ).result(timeout=30)
    except Exception:
        log.exception(
            "failed to apply pod update for replica %d", pod_update.replica_id
        )
