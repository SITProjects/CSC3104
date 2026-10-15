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

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from services.autoscaler.burst import config as burst_config
from services.autoscaler.burst import launch as burst
from services.autoscaler.burst.aws import ec2_client
from services.autoscaler.burst.market import LaunchUnknown, MarketChooser
from services.autoscaler.controller import cluster, pending, settings, slots
from services.autoscaler.controller.decide import (
    AccountUsage,
    Action,
    Instance,
    Snapshot,
    decide,
    parse_utc,
    pending_grace,
    split_by_slot,
)
from services.autoscaler.controller.rescue import RescueMemory, finished_replicas
from shared.migrate import run_migrations

log = logging.getLogger("autoscaler")

# What reclaimed workers were running, kept across ticks (see rescue.py)
RESCUE = RescueMemory()
# Spot or On-Demand per launch, and what Spot refusals taught it (see market.py)
MARKET = MarketChooser(settings.BURST_MARKET, settings.SPOT_COOLDOWN_S)
# Launches not yet committed to burst_slots, kept across ticks (see pending.py)
PENDING = pending.PendingLaunches()


def burst_instances(ec2) -> list[Instance]:
    """Every live burst instance, one entry per instance id. Two on the same
    slot are both kept, so neither drops out of the budget."""
    instances = []
    for found in burst.find_instances(ec2):
        slot = burst.slot_of(found)
        if slot is not None:
            tag = burst.tags_of(found).get(burst_config.RECLAIM_TAG_KEY)
            instances.append(
                Instance(
                    found["InstanceId"],
                    found["LaunchTime"],
                    found["InstanceType"],
                    slot=slot,
                    reclaim_at=parse_utc(tag) if tag is not None else None,
                    client_token=found.get("ClientToken", ""),
                )
            )
    return instances


def terminate_instance(ec2, instance_id: str) -> None:
    """Terminate one instance. One that is already gone counts as done."""
    try:
        ec2.terminate_instances(InstanceIds=[instance_id])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "InvalidInstanceID.NotFound":
            raise


def account_usage(ec2) -> AccountUsage:
    """Every running instance in the region counts towards the AWS Academy quota,
    including the control plane and anything teammates started. The ids and
    slot tokens seen are kept, so decide can tell which of our workers this
    listing already counts."""
    count, vcpus, ids, slot_tokens = 0, 0, set(), set()
    pages = ec2.get_paginator("describe_instances").paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["pending", "running"]}]
    )
    for page in pages:
        for reservation in page["Reservations"]:
            for instance in reservation["Instances"]:
                cpu = instance.get("CpuOptions", {})
                count += 1
                vcpus += cpu.get("CoreCount", 1) * cpu.get("ThreadsPerCore", 1)
                if "InstanceId" in instance:
                    ids.add(instance["InstanceId"])
                slot = burst.slot_of(instance)
                if slot is not None and instance.get("ClientToken"):
                    slot_tokens.add((slot, instance["ClientToken"]))
    return AccountUsage(count, vcpus, frozenset(ids), frozenset(slot_tokens))


def observe(db: Session, core, ec2, now: datetime) -> Snapshot:
    pods = cluster.task_pods(core)
    on_node, bound, unplaced, succeeded = cluster.pod_places(pods)
    # Remembered replicas that need no more work are never replaced. The lookup
    # is optional, so a failure only skips it (a savepoint keeps the tick alive).
    finished = set(succeeded)
    try:
        with db.begin_nested():
            finished |= finished_replicas(db, RESCUE.candidates(on_node))
    except Exception:
        log.warning("could not read replica states, skipping that check", exc_info=True)
    waiting, unschedulable = cluster.unscheduled_pods(pods, now)
    idle_campus, campus_free, burst_nodes = cluster.read_nodes(
        core, cluster.node_loads(pods)
    )
    live = burst_instances(ec2) if ec2 is not None else None
    account = account_usage(ec2) if ec2 is not None else None
    rows = slots.claimed(db)
    instances, duplicates = (None, []) if live is None else split_by_slot(live, rows)

    # The table does not store the machine size, so take it from the recorded
    # instance in EC2, then from the node label, so the budget counts what is
    # really running
    by_id = {i.instance_id: i for i in live or []}
    claimed = {}
    for slot, row in rows.items():
        machine = ""
        if row.instance_id in by_id:
            machine = by_id[row.instance_id].machine
        elif slot in burst_nodes:
            machine = burst_nodes[slot].machine
        claimed[slot] = dataclasses.replace(row, machine=machine)

    snapshot = Snapshot(
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
        duplicates=duplicates,
        bound=bound,
        waiting=unplaced,
        finished=finished,
        pending=PENDING.view(),
    )
    return dataclasses.replace(snapshot, rescue=RESCUE.update(snapshot, on_node))


def other_owner(snapshot: Snapshot, slot: int, instance_id: str) -> bool:
    """Whether an instance other than `instance_id` holds or claims this slot."""
    ids = set()
    claimed = snapshot.claimed_slots.get(slot)
    if claimed is not None:
        ids.add(claimed.instance_id)
    current = (snapshot.instances or {}).get(slot)
    if current is not None:
        ids.add(current.instance_id)
    ids |= {i.instance_id for i in snapshot.duplicates if i.slot == slot}
    return bool(ids - {instance_id})


def remove_node_of(core, snapshot: Snapshot, slot: int, instance_id: str) -> None:
    """Remove the slot's node and password, only if they are this instance's."""
    log.info(
        cluster.remove_burst_node(
            core,
            burst_config.node_name(slot),
            instance_id,
            other_owner(snapshot, slot, instance_id),
        )
    )


def apply(action: Action, db: Session, core, ec2, snapshot: Snapshot) -> None:
    node = snapshot.burst_nodes.get(action.slot) if action.slot is not None else None

    if action.kind == "hold":
        return  # nothing to do, the reason is already logged
    if action.kind == "launch":
        launch_one(action, db, snapshot)
    elif action.kind == "uncordon":
        cluster.uncordon(core, node.name, node.uid)
    elif action.kind == "drain":
        cluster.drain(core, node.name, node.uid)
    elif action.kind == "mark_empty":
        cluster.mark_empty(core, node.name, snapshot.now, node.uid)
    elif action.kind == "clear_empty":
        cluster.clear_empty(core, node.name, node.uid)
    elif action.kind == "retire":
        # Only the recorded instance. A second instance on the slot (perhaps in
        # its warning period) is left alone and keeps counting until it ends.
        # Cordon first so nothing new lands on it, but only if the node is this
        # instance's right now (read fresh, patch pinned to its uid)
        other = other_owner(snapshot, action.slot, action.instance_id)
        name = burst_config.node_name(action.slot)
        log.info(cluster.cordon_owned(core, name, action.instance_id, other))
        terminate_instance(ec2, action.instance_id)
        remove_node_of(core, snapshot, action.slot, action.instance_id)
        claimed = snapshot.claimed_slots.get(action.slot)
        if claimed is not None and claimed.instance_id == action.instance_id:
            slots.release(db, action.slot)
    elif action.kind == "free_slot":
        remove_node_of(core, snapshot, action.slot, action.instance_id)
        slots.release(db, action.slot)
    elif action.kind == "kill_orphan":
        ec2.terminate_instances(InstanceIds=[action.instance_id])
        remove_node_of(core, snapshot, action.slot, action.instance_id)
    elif action.kind == "reclaim":
        # Only the instance that had the warning, and only its own node and slot
        terminate_instance(ec2, action.instance_id)
        remove_node_of(core, snapshot, action.slot, action.instance_id)
        claimed = snapshot.claimed_slots.get(action.slot)
        if claimed is not None and claimed.instance_id == action.instance_id:
            slots.release(db, action.slot)
    elif action.kind == "warn_retire":
        # Lifetime limit (lifetime.py): the same warning as a Spot reclaim, so
        # the watcher taints the node and reports BUSY. The deadline comes from
        # the launch time, so a retry writes the same value.
        burst.tag_reclaim(ec2, action.instance_id, action.deadline)
    elif action.kind == "kill_duplicate":
        # The node and slot belong to the instance that is kept
        ec2.terminate_instances(InstanceIds=[action.instance_id])
    else:
        raise ValueError(f"unknown action {action.kind}")


def launch_one(action: Action, db: Session, snapshot: Snapshot) -> None:
    """Claim a slot and start one worker. The slot is held in PENDING from just
    before the first request until the instance id is committed, so no other
    launch can take it while the answer is unknown or not saved yet."""
    # Read held slots now, not from the snapshot: a launch earlier in this tick
    # may have just reserved one
    row = slots.claim_free(db, skip=set(snapshot.instances or {}) | PENDING.slots())
    if row is None:
        log.info("no free burst slot")
        return
    slot = slots.slot_number(row.ip)
    entry = PENDING.reserve(slot, action.machine or "")

    def on_request(request: dict, market: str) -> None:
        entry.request, entry.market = request, market

    try:
        instance_id = burst.launch(
            slot, instance_type=action.machine, chooser=MARKET, on_request=on_request
        )
    except LaunchUnknown as unknown:
        # It may have started a machine. Keep the slot and settle it next tick.
        log.warning(
            "slot %s: %s. Slot held until its instance is found (token %s)",
            slot,
            unknown,
            entry.client_token,
        )
        return
    except Exception:
        # A refusal, or a failure before any request: nothing started
        PENDING.drop(slot)
        raise
    entry.found(instance_id)
    slots.record_launch(row, instance_id)
    entry.staged = True  # kept until the tick's transaction commits


def tick(engine, core, ec2) -> None:
    now = datetime.now(UTC)
    # A launch marked recorded last tick but still held was not committed
    PENDING.begin_tick()
    with Session(engine) as db:
        with db.begin():
            if not slots.try_lock(db):
                log.info("another autoscaler copy is running this tick, skipping")
                return
            _act(db, core, ec2, now)
        PENDING.committed()


def _act(db: Session, core, ec2, now: datetime) -> None:
    """One tick inside its transaction: settle held launches, observe, decide, act."""
    if ec2 is not None and PENDING:
        pending.resolve(db, ec2, PENDING)
    snapshot = observe(db, core, ec2, now)
    log.info(
        "pods waiting %d (unschedulable %d, burst after %.0fs), campus PCs taking pods %d, "
        "burst slots in use %s, replicas from reclaimed workers %d",
        snapshot.unscheduled_pods,
        len(snapshot.unschedulable),
        pending_grace(len(snapshot.unschedulable)),
        snapshot.idle_campus_nodes,
        sorted(snapshot.claimed_slots) or "none",
        len(snapshot.rescue),
    )
    for action in decide(snapshot):
        where = f" slot {action.slot}" if action.slot is not None else ""
        what = f" {action.machine}" if action.machine else ""
        if settings.DRY_RUN or action.kind == "hold":
            prefix = "DRY RUN would " if settings.DRY_RUN else ""
            log.info("%s%s%s%s: %s", prefix, action.kind, what, where, action.reason)
            continue
        log.info("%s%s%s: %s", action.kind, what, where, action.reason)
        try:
            # A savepoint per action, so one failure does not undo the others
            with db.begin_nested():
                apply(action, db, core, ec2, snapshot)
        except Exception:
            PENDING.savepoint_failed()
            log.exception("%s%s failed, will retry next tick", action.kind, where)
        else:
            PENDING.savepoint_done()


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
        "autoscaler started%s: every %ss, sizes %s, market %s, budget US$%.4f/h, "
        "burst after %s to %ss stuck, retire after %ss empty, "
        "lifetime %ss (no new pods %ss before, warning %ss before)",
        " (DRY RUN)" if settings.DRY_RUN else "",
        settings.INTERVAL_S,
        ", ".join(settings.CATALOG),
        settings.BURST_MARKET,
        settings.BUDGET_USD_PER_HOUR,
        settings.MIN_PENDING_GRACE_S,
        settings.PENDING_GRACE_S,
        settings.EMPTY_GRACE_S,
        settings.MAX_LIFETIME_S,
        settings.RETIRE_CORDON_LEAD_S,
        settings.RETIRE_WARN_LEAD_S,
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
