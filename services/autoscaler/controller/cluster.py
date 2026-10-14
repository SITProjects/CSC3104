"""Read the cluster state the rules need, and change burst nodes."""

import logging
from datetime import datetime

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client.rest import ApiException

from services.autoscaler.burst import config as burst_config
from services.autoscaler.controller import settings
from services.autoscaler.controller.decide import (
    BurstNode,
    CampusRoom,
    NodeLoad,
    PodInfo,
    StuckPod,
    parse_utc,
)


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
                replica=_replica(pod),
            )
        )
    return waiting, unschedulable


def _replica(pod) -> str:
    labels = pod.metadata.labels or {}
    return labels.get(settings.REPLICA_LABEL, pod.metadata.name)


def pod_places(
    pods,
) -> tuple[dict[str, list[PodInfo]], dict[str, set[str]], set[str], set[str]]:
    """Where each task pod's replica is: the pods on each node, the nodes each
    replica has a pod on, the replicas whose pod has no node yet, and the
    replicas whose pod succeeded. Pods that are being deleted still count where
    they are, so a reclaimed worker's pods are remembered until really gone."""
    on_node: dict[str, list[PodInfo]] = {}
    bound: dict[str, set[str]] = {}
    waiting: set[str] = set()
    succeeded: set[str] = set()
    for pod in pods:
        replica = _replica(pod)
        if pod.status.phase == "Succeeded":
            succeeded.add(replica)
        if pod.status.phase not in ("Pending", "Running"):
            continue
        if not pod.spec.node_name:
            waiting.add(replica)
            continue
        cpu, mem = _pod_requests(pod)
        labels = pod.metadata.labels or {}
        group = labels.get(settings.TASK_LABEL, pod.metadata.name)
        on_node.setdefault(pod.spec.node_name, []).append(
            PodInfo(replica, group, cpu, mem, pod.spec.node_name)
        )
        bound.setdefault(replica, set()).add(pod.spec.node_name)
    return on_node, bound, waiting, succeeded


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
                instance_id=labels.get(settings.INSTANCE_ID_LABEL, ""),
                uid=node.metadata.uid or "",
                reclaim_at=(
                    parse_utc(notes[settings.RECLAIM_AT_ANNOTATION])
                    if settings.RECLAIM_AT_ANNOTATION in notes
                    else None
                ),
            )
            continue
        taints = node.spec.taints or []
        owner_active = any(t.key == settings.OWNER_TAINT for t in taints)
        if _is_ready(node) and not node.spec.unschedulable and not owner_active:
            allocatable = node.status.allocatable or {}
            campus_free.append(
                CampusRoom(
                    cpu_millicores(allocatable.get("cpu", "0")) - load.cpu_m,
                    memory_mib(allocatable.get("memory", "0")) - load.mem_mib,
                    node.metadata.name,
                    frozenset(load.groups),
                )
            )
    return len(campus_free), campus_free, burst


log = logging.getLogger("autoscaler")


def _patch(core: client.CoreV1Api, name: str, body: dict, uid: str = "") -> bool:
    """Patch node `name`. With a uid, only that exact node object is changed:
    the API server rejects the patch (422) if the node was replaced since it was
    read. Not resourceVersion, because the kubelet updates a node's status every
    few seconds and that would fail almost every patch. False if skipped."""
    if uid:
        body = {**body, "metadata": {**body.get("metadata", {}), "uid": uid}}
    try:
        core.patch_node(name, body)
    except ApiException as error:
        if error.status in (404, 409, 422):
            log.warning("node %s changed or is gone, not patched", name)
            return False
        raise
    return True


def drain(core, name: str, uid: str = "") -> None:
    """Cordon the node so it takes no new pods. Running pods carry on."""
    body = {
        "spec": {"unschedulable": True},
        "metadata": {"annotations": {settings.DRAINING_ANNOTATION: "true"}},
    }
    _patch(core, name, body, uid)


def uncordon(core, name: str, uid: str = "") -> None:
    body = {
        "spec": {"unschedulable": False},
        "metadata": {"annotations": {settings.DRAINING_ANNOTATION: None}},
    }
    _patch(core, name, body, uid)


def cordon(core, name: str, uid: str = "") -> bool:
    return _patch(core, name, {"spec": {"unschedulable": True}}, uid)


def mark_empty(core, name: str, since: datetime, uid: str = "") -> None:
    note = {settings.EMPTY_SINCE_ANNOTATION: since.isoformat()}
    _patch(core, name, {"metadata": {"annotations": note}}, uid)


def clear_empty(core, name: str, uid: str = "") -> None:
    note = {settings.EMPTY_SINCE_ANNOTATION: None}
    _patch(core, name, {"metadata": {"annotations": note}}, uid)


def cordon_owned(core, name: str, instance_id: str, other_owner: bool) -> str:
    """Cordon node `name` only if it is `instance_id`'s right now. The ownership
    is read fresh, and the patch is pinned to that node's uid."""
    node = _read(lambda: core.read_node(name))
    if node is None:
        return f"no node {name} to cordon"
    reason = _not_owned(node, instance_id, other_owner)
    if reason:
        return f"did not cordon {name}, {reason}"
    if not cordon(core, name, node.metadata.uid):
        return f"did not cordon {name}, it was replaced while checking"
    return f"cordoned {name}"


# ---------- removing a burst node safely ----------


def _delete_options(uid: str) -> client.V1DeleteOptions:
    # The API server deletes only the object with this uid, so an object made
    # again under the same name in the meantime is never touched
    return client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=uid))


def remove_burst_node(core, name: str, instance_id: str, other_owner: bool) -> str:
    """Delete burst node `name` and its K3s node password, but only if they
    belong to `instance_id`. Returns what happened, for the log.

    Ownership is checked now, not from the tick's snapshot. A node labelled
    idlecloud/instance-id belongs to that instance. An older node without the
    label counts as this instance's only when no other instance holds or claims
    the slot (`other_owner` is False).

    The password secret has no owner of its own. K3s may create a new worker's
    password before that worker registers its node, so a password is only
    deleted when the owned node is seen both before and after reading it (same
    uid). K3s refuses a second password for a name while one exists, and our
    code never deletes a password without deleting its node first. So a
    password read while that same node exists belongs to it. Both deletes
    carry a uid precondition. If there is no node, the node changes, or its
    delete finds it gone or replaced, the password is kept. `launch` clears a
    leftover password by name before it starts a new worker on the slot.
    """
    secret = f"{name}.node-password.k3s"
    node = _read(lambda: core.read_node(name))
    if node is None:
        return f"no node {name}, kept any password (ownership unknown)"
    reason = _not_owned(node, instance_id, other_owner)
    if reason:
        return f"kept node {name}, {reason}"

    password = _read(lambda: core.read_namespaced_secret(secret, "kube-system"))
    again = _read(lambda: core.read_node(name))
    if (
        again is None
        or again.metadata.uid != node.metadata.uid
        or _not_owned(again, instance_id, other_owner)
    ):
        return f"kept node {name} and its password, the node changed while checking"

    uid = node.metadata.uid
    if not _delete(lambda: core.delete_node(name, body=_delete_options(uid))):
        return f"kept the password of {name}, the node was already gone or replaced"
    if password is not None:
        _delete(
            lambda: core.delete_namespaced_secret(
                secret, "kube-system", body=_delete_options(password.metadata.uid)
            )
        )
    return f"removed node {name} of {instance_id}"


def _not_owned(node, instance_id: str, other_owner: bool) -> str:
    """Why this node is not `instance_id`'s, or an empty string if it is."""
    owner = (node.metadata.labels or {}).get(settings.INSTANCE_ID_LABEL)
    if owner and owner != instance_id:
        return f"it belongs to {owner}"
    if not owner and other_owner:
        return "it has no instance label and another instance holds the slot"
    return ""


def _read(call):
    try:
        return call()
    except ApiException as error:
        if error.status == 404:
            return None
        raise


def _delete(call) -> bool:
    """True only if this exact object was deleted now. False if it was already
    gone (404) or the uid no longer matched (409)."""
    try:
        call()
    except ApiException as error:
        if error.status in (404, 409):
            return False
        raise
    return True
