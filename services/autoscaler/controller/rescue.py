"""Remember which replicas ran on a burst worker when its reclaim warning came.

Proactive replacement (decide._rescue_demand) needs to know what a reclaimed
worker was running. The Spot watcher taints the node at once, so its pods may
already be deleted by the next tick. This keeps the pods each node had on the
previous tick, with the instance that node ran on. When a worker is warned it
keeps that instance's replicas until a while after the deadline.

The memory lives in this process only. After an autoscaler restart it starts
empty. Replacement is a speed-up, never the only path: once the coordinator
requeues an evicted replica as a new pod, the normal scale-up places it.
"""

import dataclasses
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.autoscaler.burst import config as burst_config
from services.autoscaler.controller import settings
from services.autoscaler.controller.decide import reclaim_deadlines
from services.autoscaler.controller.model import PodInfo, Snapshot

BURST_NODES = [burst_config.node_name(slot) for slot in burst_config.SLOTS]


class RescueMemory:
    def __init__(self) -> None:
        # node name: (instance id from its label, its pods) on the last tick
        self.last_on_node: dict[str, tuple[str, list[PodInfo]]] = {}
        self.kept: dict[str, tuple[PodInfo, datetime]] = {}  # replica: (pod, until)

    def candidates(self, on_node: dict[str, list[PodInfo]]) -> set[str]:
        """Replica ids this tick could remember or already remembers: the ones
        kept, and every replica on a burst worker now or on the last tick. The
        loop checks these against the coordinator before calling update()."""
        ids = set(self.kept)
        for name in BURST_NODES:
            ids |= {pod.replica for pod in on_node.get(name, [])}
            ids |= {pod.replica for pod in self.last_on_node.get(name, ("", []))[1]}
        return ids

    def update(self, s: Snapshot, on_node: dict[str, list[PodInfo]]) -> list[PodInfo]:
        """Take this tick's pods per node, return the replicas to replace."""
        keep_for = timedelta(seconds=settings.RESCUE_KEEP_S)
        for slot, (warned, deadline) in reclaim_deadlines(s).items():
            name = burst_config.node_name(slot)
            seen = []
            # Last tick's pods, even if the node is gone by now, but only if
            # that node ran the warned instance (or predates the instance label)
            last_instance, last_pods = self.last_on_node.get(name, ("", []))
            if last_instance in ("", warned):
                seen += last_pods
            node = s.burst_nodes.get(slot)
            if node is not None and node.instance_id in ("", warned):
                seen += on_node.get(name, [])
            for pod in seen:
                pod = dataclasses.replace(pod, node=name, instance=warned)
                old = self.kept.get(pod.replica)
                # The same warning seen again keeps its first expiry, so one
                # warning never keeps a replica longer. A warning on another
                # instance (the replica moved there) gets a fresh record.
                if old is None or old[0].instance != warned:
                    self.kept[pod.replica] = (pod, deadline + keep_for)
        self.kept = {
            r: v for r, v in self.kept.items() if v[1] > s.now and r not in s.finished
        }
        labels = {node.name: node.instance_id for node in s.burst_nodes.values()}
        self.last_on_node = {
            name: (labels.get(name, ""), pods) for name, pods in on_node.items()
        }
        return [pod for pod, _ in self.kept.values()]


def finished_replicas(db: Session, replicas: set[str]) -> set[str]:
    """Which of these replica ids need no more work, per the coordinator: the
    replica is DONE or FAILED, its task is VERIFIED or FAILED, or its job is
    COMPLETED, FAILED or CANCELLED. A failed pod alone does not count, since
    the coordinator may retry it. If a status here turns out not to be final,
    only the head start is lost: a retried replica still comes back as a pod."""
    ids = sorted(int(r) for r in replicas if r.isdigit())
    if not ids:
        return set()
    rows = db.execute(
        text(
            """
            SELECT r.id FROM replicas r
            JOIN tasks t ON t.id = r.task_id
            JOIN jobs j ON j.id = t.job_id
            WHERE r.id = ANY(:ids)
              AND (r.status IN ('DONE', 'FAILED')
                   OR t.status IN ('VERIFIED', 'FAILED')
                   OR j.status IN ('COMPLETED', 'FAILED', 'CANCELLED'))
            """
        ),
        {"ids": ids},
    )
    return {str(row[0]) for row in rows}
