"""Read the cluster state the rules need, and change burst nodes."""

from datetime import datetime

from kubernetes import client
from kubernetes import config as kube_config

from services.autoscaler.burst import config as burst_config
from services.autoscaler.controller import settings
from services.autoscaler.controller.decide import BurstNode, NodeLoad, StuckPod


def connect() -> client.CoreV1Api:
    try:
        kube_config.load_incluster_config()  # running as the Deployment
    except kube_config.ConfigException:
        kube_config.load_kube_config()  # running in the dev container
    return client.CoreV1Api()


# ---------- quantities ----------

_MEM_UNITS = {
    "Ki": 1 / 1024,
    "Mi": 1,
    "Gi": 1024,
    "Ti": 1024 * 1024,
    "K": 1000 / 1024**2,
    "M": 1000**2 / 1024**2,
    "G": 1000**3 / 1024**2,
}


def cpu_millicores(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1])
    return int(float(value) * 1000)


def memory_mib(value: str) -> float:
    for unit, factor in _MEM_UNITS.items():
        if value.endswith(unit):
            return float(value[: -len(unit)]) * factor
    return float(value) / 1024**2  # plain bytes


def _pod_requests(pod) -> tuple[int, float]:
    cpu, mem = 0, 0.0
    for container in pod.spec.containers:
        requests = (container.resources and container.resources.requests) or {}
        cpu += cpu_millicores(requests.get("cpu", "0"))
        mem += memory_mib(requests.get("memory", "0"))
    return cpu, mem


# ---------- pods ----------


def task_pods(core: client.CoreV1Api):
    return core.list_namespaced_pod(settings.JOB_NAMESPACE).items


def unscheduled_pods(pods, now: datetime) -> tuple[int, list[StuckPod]]:
    """Count task pods with no node yet, and return the ones no node can take,
    with how long they have waited and what they need.

    That is a pod the scheduler marked Unschedulable, or one it has not judged
    at all. The second case happens when the scheduler cannot run, for example
    K3s holds its scheduler back until a usable worker node exists. A pod held by
    a scheduling gate (reason SchedulingGated) is waiting on purpose and skipped.
    """
    waiting, unschedulable = 0, []
    for pod in pods:
        if pod.status.phase != "Pending" or pod.spec.node_name:
            continue
        waiting += 1
        condition = next(
            (c for c in pod.status.conditions or [] if c.type == "PodScheduled"), None
        )
        if condition is not None and not (
            condition.status == "False" and condition.reason == "Unschedulable"
        ):
            continue
        since = (
            condition.last_transition_time if condition else None
        ) or pod.metadata.creation_timestamp
        cpu, mem = _pod_requests(pod)
        labels = pod.metadata.labels or {}
        unschedulable.append(
            StuckPod(
                name=pod.metadata.name,
                waited_s=(now - since).total_seconds(),
                cpu_m=cpu,
                mem_mib=mem,
                # replicas of one task share this label, so they need different nodes
                group=labels.get(settings.TASK_LABEL, pod.metadata.name),
            )
        )
    return waiting, unschedulable


def node_loads(pods) -> dict[str, NodeLoad]:
    """What the task pods bound to each node request, by node name."""
    loads: dict[str, NodeLoad] = {}
    for pod in pods:
        if pod.spec.node_name and pod.status.phase in ("Pending", "Running"):
            cpu, mem = _pod_requests(pod)
            labels = pod.metadata.labels or {}
            loads.setdefault(pod.spec.node_name, NodeLoad()).add(
                cpu, mem, labels.get(settings.TASK_LABEL, pod.metadata.name)
            )
    return loads


# ---------- nodes ----------


def _is_ready(node) -> bool:
    return any(
        c.type == "Ready" and c.status == "True" for c in node.status.conditions or []
    )


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _ready_since(node) -> datetime | None:
    for c in node.status.conditions or []:
        if c.type == "Ready" and c.status == "True":
            return c.last_transition_time
    return None


def read_nodes(
    core: client.CoreV1Api, loads: dict[str, NodeLoad]
) -> tuple[int, list[tuple[int, float]], dict[int, BurstNode]]:
    """Return the campus PCs that take pods, the free room on each, and the
    burst nodes by slot.

    A campus PC takes pods when it is a Ready worker, not cordoned, with no
    owner-active taint (member 2's node agent adds that taint when the owner is
    back). Its free room is its allocatable CPU and memory minus what the task
    pods bound to it request.
    """
    campus_free, burst = [], {}
    for node in core.list_node().items:
        labels = node.metadata.labels or {}
        if labels.get(settings.ROLE_LABEL) != "worker":
            continue
        load = loads.get(node.metadata.name, NodeLoad())
        if labels.get(settings.POOL_LABEL) == "cloud":
            slot = labels.get(burst_config.SLOT_LABEL_KEY)
            if slot is None or not slot.isdigit():
                continue
            notes = node.metadata.annotations or {}
            burst[int(slot)] = BurstNode(
                slot=int(slot),
                name=node.metadata.name,
                ready=_is_ready(node),
                draining=notes.get(settings.DRAINING_ANNOTATION) == "true",
                task_pods=load.pods,
                empty_since=_parse_time(notes.get(settings.EMPTY_SINCE_ANNOTATION)),
                machine=labels.get(settings.TYPE_LABEL, ""),
                ready_since=_ready_since(node),
                load=load,
            )
            continue
        taints = node.spec.taints or []
        owner_active = any(t.key == settings.OWNER_TAINT for t in taints)
        if _is_ready(node) and not node.spec.unschedulable and not owner_active:
            allocatable = node.status.allocatable or {}
            campus_free.append(
                (
                    cpu_millicores(allocatable.get("cpu", "0")) - load.cpu_m,
                    memory_mib(allocatable.get("memory", "0")) - load.mem_mib,
                )
            )
    return len(campus_free), campus_free, burst


def _patch(core: client.CoreV1Api, name: str, body: dict) -> None:
    core.patch_node(name, body)


def drain(core, name: str) -> None:
    """Cordon the node so it takes no new pods. Running pods carry on."""
    _patch(
        core,
        name,
        {
            "spec": {"unschedulable": True},
            "metadata": {"annotations": {settings.DRAINING_ANNOTATION: "true"}},
        },
    )


def uncordon(core, name: str) -> None:
    _patch(
        core,
        name,
        {
            "spec": {"unschedulable": False},
            "metadata": {"annotations": {settings.DRAINING_ANNOTATION: None}},
        },
    )


def cordon(core, name: str) -> None:
    _patch(core, name, {"spec": {"unschedulable": True}})


def mark_empty(core, name: str, since: datetime) -> None:
    _patch(
        core,
        name,
        {
            "metadata": {
                "annotations": {settings.EMPTY_SINCE_ANNOTATION: since.isoformat()}
            }
        },
    )


def clear_empty(core, name: str) -> None:
    _patch(
        core,
        name,
        {"metadata": {"annotations": {settings.EMPTY_SINCE_ANNOTATION: None}}},
    )
