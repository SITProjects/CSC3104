"""How many more task copies the worker nodes can take right now.

The web app uses this to size an OCR job's tasks: with many free slots it splits a
PDF finely so all of it runs at once, with few it keeps tasks large so per-copy
overhead stays small. It is an estimate for planning, not a reservation.
"""

import asyncio
from collections.abc import Iterable

from kubernetes import client
from kubernetes.utils import parse_quantity

from core.settings import JOB_NAMESPACE
from services.coordinator.k8s import (
    GPU_JOB_LABEL,
    GPU_NODE_LABEL,
    GPU_NODE_VALUE,
    GPU_VRAM_LABEL,
)

WORKER_SELECTOR = "idlecloud/role=worker"
# Taints a task Pod tolerates (k8s.py): only brief node trouble
TOLERATED_TAINTS = {"node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"}
FINISHED_PHASES = {"Succeeded", "Failed"}


def usable(node: client.V1Node) -> bool:
    """
    A worker the scheduler may place a task on: Ready, not cordoned, and with no
    taint that keeps task Pods off, such as an owner using the PC or a
    quarantined node.
    """
    if node.spec is None or node.spec.unschedulable:
        return False
    ready = any(
        c.type == "Ready" and c.status == "True"
        for c in (node.status.conditions or [] if node.status else [])
    )
    blocking = any(
        t.effect in ("NoSchedule", "NoExecute") and t.key not in TOLERATED_TAINTS
        for t in node.spec.taints or []
    )
    return ready and not blocking


def _requests(pod: client.V1Pod) -> tuple[float, float]:
    """The CPU (cores) and memory (bytes) a Pod's containers ask for."""
    cpu = mem = 0.0
    for container in pod.spec.containers or []:
        requests = (container.resources and container.resources.requests) or {}
        cpu += float(parse_quantity(requests.get("cpu", "0")))
        mem += float(parse_quantity(requests.get("memory", "0")))
    return cpu, mem


def gpu_node(node: client.V1Node, min_gpu_vram_gib: int | None = None) -> bool:
    """A worker labelled with a working NVIDIA GPU, with enough GPU memory if a
    minimum is asked for (the same rules k8s.py gives GPU copies)."""
    labels = (node.metadata.labels if node.metadata else None) or {}
    if labels.get(GPU_NODE_LABEL) != GPU_NODE_VALUE:
        return False
    if not min_gpu_vram_gib:
        return True
    try:
        return int(labels.get(GPU_VRAM_LABEL, "")) >= min_gpu_vram_gib
    except ValueError:
        return False


def free_slots(
    nodes: Iterable[client.V1Node],
    pods: Iterable[client.V1Pod],
    cpu_cores: int,
    mem_mib: int,
    gpu: bool = False,
    min_gpu_vram_gib: int | None = None,
) -> tuple[int, int]:
    """
    Counts copies of size (cpu_cores, mem_mib) that still fit on usable workers.

    Each node's allocatable CPU and memory, less what its unfinished task Pods
    ask for, decides how many more copies it can take. A GPU copy also needs a
    GPU node with no unfinished GPU copy on it, so such a node has at most one
    slot, however many GPUs it has.

    args:
        nodes: Worker nodes.
        pods: Task Pods (any node, any phase; finished ones are ignored).
        cpu_cores: The CPU one copy asks for.
        mem_mib: The memory one copy asks for.
        gpu: Count slots for GPU copies.
        min_gpu_vram_gib: For GPU copies, the smallest GPU memory accepted.
    returns:
        tuple[int, int]: Free slots in all, and how many usable nodes there are
            (usable GPU nodes, for GPU copies).
    """
    room = {}
    for node in nodes:
        if usable(node) and (not gpu or gpu_node(node, min_gpu_vram_gib)):
            alloc = node.status.allocatable or {}
            room[node.metadata.name] = [
                float(parse_quantity(alloc.get("cpu", "0"))),
                float(parse_quantity(alloc.get("memory", "0"))),
            ]
    busy_gpu = set()
    for pod in pods:
        node_name = pod.spec.node_name if pod.spec else None
        if node_name not in room or (
            pod.status and pod.status.phase in FINISHED_PHASES
        ):
            continue
        cpu, mem = _requests(pod)
        room[node_name][0] -= cpu
        room[node_name][1] -= mem
        if ((pod.metadata.labels if pod.metadata else None) or {}).get(GPU_JOB_LABEL):
            busy_gpu.add(node_name)
    fits = {
        name: max(0, min(int(cpu // cpu_cores), int(mem // (mem_mib * 2**20))))
        for name, (cpu, mem) in room.items()
    }
    if gpu:
        slots = sum(1 for name, n in fits.items() if n > 0 and name not in busy_gpu)
    else:
        slots = sum(fits.values())
    return slots, len(room)


async def current_free_slots(
    core_api: client.CoreV1Api,
    cpu_cores: int,
    mem_mib: int,
    gpu: bool = False,
    min_gpu_vram_gib: int | None = None,
) -> tuple[int, int]:
    """free_slots for the cluster as it is now (needs list on nodes and on Pods
    in the task namespace)."""
    nodes, pods = await asyncio.gather(
        asyncio.to_thread(
            core_api.list_node, label_selector=WORKER_SELECTOR, _request_timeout=10
        ),
        asyncio.to_thread(
            core_api.list_namespaced_pod, JOB_NAMESPACE, _request_timeout=10
        ),
    )
    return free_slots(
        nodes.items or [], pods.items or [], cpu_cores, mem_mib, gpu, min_gpu_vram_gib
    )
