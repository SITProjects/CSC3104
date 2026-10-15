"""Burst cost records (shared/migrations/004_burst_cost.sql).

A machine row is written when a launch is recorded on a slot and closed when
the slot is released, both in the slot's own transaction. Each tick also saves
what task pods ran on burst machines, from their container start and finish
times. A pod already saved is updated by its uid wherever it is listed, so its
finish still arrives after its node is gone or reused. A new pod is only linked
to a machine that was running when the pod started, so an old pod is never
charged to the new machine that reuses its node name.

The views in the migration turn these rows into estimated cost per job.
"""

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from services.autoscaler.burst import config as burst_config
from services.autoscaler.controller import cluster, settings
from services.autoscaler.controller.model import BurstNode, Instance

JOB_LABEL = "idlecloud/job-id"
ATTEMPT_LABEL = "idlecloud/attempt"
WORKER_CONTAINER = "worker"


def start_machine(
    db: Session, instance_id: str, slot: int, node_name: str, machine: str, market: str
) -> None:
    """Record a new machine with the rate the budget uses for it. Writing the
    same instance again (after a rolled back transaction) changes nothing.
    An empty size means the launch template default."""
    machine = machine or burst_config.INSTANCE_TYPE
    known = settings.KNOWN_TYPES.get(machine)
    db.execute(
        text(
            "INSERT INTO burst_machines (instance_id, slot, node_name, machine,"
            " market, usd_per_hour, cpu_m, launched_at)"
            " VALUES (:id, :slot, :node, :machine, :market, :rate, :cpu, now())"
            " ON CONFLICT (instance_id) DO NOTHING"
        ),
        {
            "id": instance_id,
            "slot": slot,
            "node": node_name,
            "machine": machine,
            "market": market,
            "rate": settings.cost_per_hour(machine),
            "cpu": known.cpu_m if known else None,
        },
    )


def end_machine(db: Session, instance_id: str) -> None:
    db.execute(
        text(
            "UPDATE burst_machines SET ended_at = now()"
            " WHERE instance_id = :id AND ended_at IS NULL"
        ),
        {"id": instance_id},
    )


def record(
    db: Session,
    pods,
    burst_nodes: dict[int, BurstNode],
    live: list[Instance],
    now: datetime,
) -> None:
    """Take launch times from EC2, then save the runs seen on burst nodes."""
    for instance in live:
        db.execute(
            text(
                "UPDATE burst_machines SET launched_at = :at"
                " WHERE instance_id = :id AND launched_at <> :at"
            ),
            {"id": instance.instance_id, "at": instance.launched_at},
        )
    on = {n.name: n.instance_id for n in burst_nodes.values() if n.instance_id}
    for pod in pods:
        started, finished = _container_times(pod)
        if started is None:
            continue
        # A saved run keeps its machine. Only fill its finish and last seen.
        seen = db.execute(
            text(
                "UPDATE burst_runs SET"
                " finished_at = COALESCE(finished_at, :finished),"
                " last_seen_at = GREATEST(last_seen_at, :now)"
                " WHERE pod_uid = :uid"
            ),
            {"uid": pod.metadata.uid, "finished": finished, "now": now},
        )
        instance_id = on.get(pod.spec.node_name)
        if seen.rowcount or instance_id is None:
            continue
        labels = pod.metadata.labels or {}
        db.execute(
            text(
                "INSERT INTO burst_runs (pod_uid, instance_id, job_id, task_id,"
                " replica_id, attempt, cpu_m, started_at, finished_at, last_seen_at)"
                " SELECT :uid, instance_id, :job, :task, :replica, :attempt, :cpu,"
                " :started, :finished, :now FROM burst_machines"
                " WHERE instance_id = :id AND launched_at <= :started"
                " ON CONFLICT (pod_uid) DO NOTHING"
            ),
            {
                "uid": pod.metadata.uid,
                "id": instance_id,
                "job": _uuid(labels.get(JOB_LABEL)),
                "task": _int(labels.get(settings.TASK_LABEL)),
                "replica": _int(labels.get(settings.REPLICA_LABEL)),
                "attempt": _int(labels.get(ATTEMPT_LABEL)),
                "cpu": cluster.pod_requests(pod)[0],
                "started": started,
                "finished": finished,
                "now": now,
            },
        )


def _container_times(pod) -> tuple[datetime | None, datetime | None]:
    """Start and finish of the worker container, or None where not known."""
    statuses = getattr(pod.status, "container_statuses", None) or []
    status = next((s for s in statuses if s.name == WORKER_CONTAINER), None)
    state = status.state if status is not None else None
    if state is None:
        return None, None
    if state.terminated is not None:
        return state.terminated.started_at, state.terminated.finished_at
    if state.running is not None:
        return state.running.started_at, None
    return None, None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _uuid(value: str | None) -> str | None:
    try:
        return str(uuid.UUID(value)) if value is not None else None
    except ValueError:
        return None
