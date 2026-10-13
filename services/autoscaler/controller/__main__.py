"""Burst autoscaler loop. Every 30 seconds: observe, decide, act.

Run from the repo root:
    python -m services.autoscaler.controller
Set AUTOSCALER_DRY_RUN=1 to only log what it would do.
"""

import dataclasses
import logging
import os
import signal
import threading
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from services.autoscaler.burst import launch as burst
from services.autoscaler.burst.aws import ec2_client
from services.autoscaler.controller import cluster, settings, slots
from services.autoscaler.controller.decide import (
    AccountUsage,
    Action,
    Instance,
    Snapshot,
    decide,
    pending_grace,
)
from shared.migrate import run_migrations

log = logging.getLogger("autoscaler")


def burst_instances(ec2) -> dict[int, Instance]:
    instances = {}
    for found in burst.find_instances(ec2):
        slot = burst.slot_of(found)
        if slot is not None:
            instances[slot] = Instance(
                found["InstanceId"], found["LaunchTime"], found["InstanceType"]
            )
    return instances


def account_usage(ec2) -> AccountUsage:
    """Every running instance in the region counts towards the AWS Academy quota,
    including the control plane and anything teammates started."""
    count, vcpus = 0, 0
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}]
    )
    for page in pages:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                cpu = instance.get("CpuOptions", {})
                count += 1
                vcpus += cpu.get("CoreCount", 1) * cpu.get("ThreadsPerCore", 1)
    return AccountUsage(count, vcpus)


def observe(db: Session, core, ec2, now: datetime) -> Snapshot:
    pods = cluster.task_pods(core)
    waiting, unschedulable = cluster.unscheduled_pods(pods, now)
    idle_campus, campus_free, burst_nodes = cluster.read_nodes(
        core, cluster.node_loads(pods)
    )
    instances = burst_instances(ec2) if ec2 is not None else None
    account = account_usage(ec2) if ec2 is not None else None

    # The table does not store the machine size, so take it from EC2, then from
    # the node label, so the budget counts what is really running
    claimed = {}
    for slot, row in slots.claimed(db).items():
        machine = ""
        if instances and slot in instances:
            machine = instances[slot].machine
        elif slot in burst_nodes:
            machine = burst_nodes[slot].machine
        claimed[slot] = dataclasses.replace(row, machine=machine)

    return Snapshot(
        now=now,
        unscheduled_pods=waiting,
        unschedulable=unschedulable,
        idle_campus_nodes=idle_campus,
        burst_nodes=burst_nodes,
        claimed_slots=claimed,
        free_slots=slots.free_count(db),
        instances=instances,
        account=account,
        campus_free=campus_free,
    )


def apply(action: Action, db: Session, core, ec2, snapshot: Snapshot) -> None:
    node = snapshot.burst_nodes.get(action.slot) if action.slot is not None else None

    if action.kind == "hold":
        return  # nothing to do, the reason is already logged
    if action.kind == "launch":
        row = slots.claim_free(db, skip=set(snapshot.instances or {}))
        if row is None:
            log.info("no free burst slot")
            return
        slot = slots.slot_number(row.ip)
        # An AWS error rolls the claim back
        instance_id = burst.launch(slot, instance_type=action.machine)
        slots.record_launch(row, instance_id)
    elif action.kind == "uncordon":
        cluster.uncordon(core, node.name)
    elif action.kind == "drain":
        cluster.drain(core, node.name)
    elif action.kind == "mark_empty":
        cluster.mark_empty(core, node.name, snapshot.now)
    elif action.kind == "clear_empty":
        cluster.clear_empty(core, node.name)
    elif action.kind == "retire":
        if node is not None:
            cluster.cordon(core, node.name)
        burst.terminate(action.slot)  # also removes the K8s node
        slots.release(db, action.slot)
    elif action.kind == "free_slot":
        burst.remove_node(action.slot)
        slots.release(db, action.slot)
    elif action.kind == "kill_orphan":
        ec2.terminate_instances(InstanceIds=[action.instance_id])
        burst.remove_node(action.slot)
    else:
        raise ValueError(f"unknown action {action.kind}")


def tick(engine, core, ec2) -> None:
    now = datetime.now(UTC)
    with Session(engine) as db, db.begin():
        if not slots.try_lock(db):
            log.info("another autoscaler copy is running this tick, skipping")
            return
        snapshot = observe(db, core, ec2, now)
        log.info(
            "pods waiting %d (unschedulable %d, burst after %.0fs), campus PCs taking pods %d, "
            "burst slots in use %s",
            snapshot.unscheduled_pods,
            len(snapshot.unschedulable),
            pending_grace(len(snapshot.unschedulable)),
            snapshot.idle_campus_nodes,
            sorted(snapshot.claimed_slots) or "none",
        )
        for action in decide(snapshot):
            where = f" slot {action.slot}" if action.slot is not None else ""
            what = f" {action.machine}" if action.machine else ""
            if settings.DRY_RUN or action.kind == "hold":
                prefix = "DRY RUN would " if settings.DRY_RUN else ""
                log.info(
                    "%s%s%s%s: %s", prefix, action.kind, what, where, action.reason
                )
                continue
            log.info("%s%s%s: %s", action.kind, what, where, action.reason)
            try:
                # A savepoint per action, so one failure does not undo the others
                with db.begin_nested():
                    apply(action, db, core, ec2, snapshot)
            except Exception:
                log.exception("%s%s failed, will retry next tick", action.kind, where)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    # Same as the coordinator: make sure burst_slots exists, whoever starts first
    run_migrations(os.environ["DATABASE_URL"])
    engine = slots.connect()
    core = cluster.connect()
    ec2 = None if settings.DRY_RUN else ec2_client()
    log.info(
        "autoscaler started%s: every %ss, sizes %s, budget US$%.4f/h, "
        "burst after %s to %ss stuck, retire after %ss empty",
        " (DRY RUN)" if settings.DRY_RUN else "",
        settings.INTERVAL_S,
        ", ".join(settings.CATALOG),
        settings.BUDGET_USD_PER_HOUR,
        settings.MIN_PENDING_GRACE_S,
        settings.PENDING_GRACE_S,
        settings.EMPTY_GRACE_S,
    )
    while not stop.is_set():
        try:
            tick(engine, core, ec2)
        except Exception:
            # Keep the loop alive through API or database outages
            log.exception("tick failed")
        stop.wait(settings.INTERVAL_S)
    engine.dispose()
    log.info("autoscaler stopped")


if __name__ == "__main__":
    main()
