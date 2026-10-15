"""The burst_slots table (shared/migrations/001_init.sql). A row with an
instance_id is in use. Claiming uses SELECT ... FOR UPDATE SKIP LOCKED, so two
autoscaler loops can never take the same slot."""

import os
from datetime import UTC, datetime

from sqlalchemy import Engine, create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from services.autoscaler.burst import config as burst_config
from services.autoscaler.controller import settings, usage
from services.autoscaler.controller.model import ClaimedSlot
from shared.db.models import BurstSlot, Node

# We own burst workers, so each new machine starts fully trusted (plan step 5)
BURST_TRUST = 1.0


def connect() -> Engine:
    url = os.environ["DATABASE_URL"].replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    return create_engine(url, pool_pre_ping=True, pool_size=1, max_overflow=0)


def slot_number(ip) -> int:
    return int(str(ip).split(".")[-1])


def slot_ip(slot: int) -> str:
    return burst_config.node_ip(slot)


def try_lock(db: Session) -> bool:
    """Only one autoscaler copy acts per tick. Released when the transaction ends."""
    return bool(
        db.execute(
            text("SELECT pg_try_advisory_xact_lock(:id)"), {"id": settings.LOCK_ID}
        ).scalar()
    )


def claimed(db: Session) -> dict[int, ClaimedSlot]:
    rows = db.execute(
        select(BurstSlot).where(BurstSlot.instance_id.is_not(None))
    ).scalars()
    return {
        # The machine size is not stored in the table. observe() fills it in
        # from EC2 or the node label.
        slot_number(row.ip): ClaimedSlot(
            slot_number(row.ip), row.instance_id, row.claimed_at, machine=""
        )
        for row in rows
    }


def free_count(db: Session) -> int:
    return db.execute(
        select(func.count())
        .select_from(BurstSlot)
        .where(BurstSlot.instance_id.is_(None))
    ).scalar_one()


def claim_free(db: Session, skip: set[int] | None = None) -> BurstSlot | None:
    """Lock the lowest free slot for this transaction, or None if all are taken.
    Slots in skip still have an EC2 instance winding down, so they are passed over."""
    busy = [slot_ip(slot) for slot in skip or ()]
    return db.execute(
        select(BurstSlot)
        .where(BurstSlot.instance_id.is_(None), BurstSlot.ip.not_in(busy))
        .order_by(BurstSlot.ip)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()


def lock_slot(db: Session, slot: int) -> BurstSlot | None:
    """Lock this exact slot's row, whatever it holds, or None if it does not exist."""
    return db.execute(
        select(BurstSlot).where(BurstSlot.ip == slot_ip(slot)).with_for_update()
    ).scalar_one_or_none()


def record_launch(
    db: Session, row: BurstSlot, instance_id: str, machine: str, market: str = ""
) -> None:
    """Save a new machine on its slot, start its cost record and reset its
    node's trust, all in the same transaction. A replay of an instance the row
    already holds never comes here, so a machine's earned score is never
    reset. It only runs again for the same instance if the first transaction
    rolled back."""
    slot = slot_number(row.ip)
    row.instance_id = instance_id
    row.node_name = burst_config.node_name(slot)
    row.claimed_at = datetime.now(UTC)
    usage.start_machine(db, instance_id, slot, row.node_name, machine, market)
    reset_trust(db, row.node_name)


def reset_trust(db: Session, name: str) -> None:
    """Node names are reused per slot, so a new machine must not inherit the
    old one's score. Only trust_score changes. The counters, state and
    heartbeat belong to the verifier and the coordinator.

    An error here fails the caller's savepoint like any slot error. The launch
    then stays held with its instance id, and the record is retried next tick
    with no new EC2 launch (see pending.py)."""
    db.execute(
        insert(Node)
        .values(name=name, trust_score=BURST_TRUST)
        .on_conflict_do_update(
            index_elements=[Node.name], set_={"trust_score": BURST_TRUST}
        )
    )


def release(db: Session, slot: int) -> None:
    """Free the slot and close the cost record of the machine it held."""
    instance_id = db.execute(
        select(BurstSlot.instance_id).where(BurstSlot.ip == slot_ip(slot))
    ).scalar_one_or_none()
    if instance_id is not None:
        usage.end_machine(db, instance_id)
    db.execute(
        update(BurstSlot)
        .where(BurstSlot.ip == slot_ip(slot))
        .values(instance_id=None, node_name=None, claimed_at=None)
    )
