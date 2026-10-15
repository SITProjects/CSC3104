"""Launches the database has not recorded yet.

A launch is held here from just before its first request until its instance id
is committed to burst_slots. While held, its slot is never claimed again, and
decide counts it like a booting worker: its room, its price in the budget, and
one instance and its vCPUs in the account quota. It is outside the normal boot
and orphan timeouts.

A launch whose answer was unknown (market.LaunchUnknown) is settled only by
finding its one instance:
- adopt: one live burst instance has this slot and this ClientToken, or
- replay: the identical request, same ClientToken, succeeds. AWS returns the
  instance an earlier request started, or starts it now. Either way, one.
A later error never settles it. Even a refusal only shows that this replay
started nothing, not that an earlier request did not. So a held launch is
never released here. After PENDING_WARN_S it is logged as a warning every
PENDING_WARN_S, with its token. EC2 not listing it is no proof either.

The memory lives in this process only. The autoscaler assumes a single running
copy (one replica, Recreate, and the advisory lock per tick reduce overlap but
do not prove it). After a restart, held launches are forgotten. An instance one
of them started is then handled as an orphan once EC2 lists it, and a slot can
be reused before that. Covering restarts needs the token in burst_slots, a
follow-up with member 3.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from services.autoscaler.burst import launch as burst
from services.autoscaler.burst.market import send
from services.autoscaler.controller import settings, slots
from services.autoscaler.controller.model import PendingSlot

log = logging.getLogger("autoscaler")


@dataclass
class PendingLaunch:
    slot: int
    machine: str
    since: float  # monotonic, when the slot was reserved
    request: dict | None = None  # the last request sent, None before the first
    market: str = ""
    instance_id: str | None = None
    found_at: datetime | None = None  # wall clock, when instance_id became known
    # Written to burst_slots in a savepoint of this tick that has not finished
    staged: bool = False
    # Written in a finished savepoint. Dropped once the tick's transaction commits.
    recorded: bool = False
    warned_at: float | None = None

    @property
    def client_token(self) -> str:
        return (self.request or {}).get("ClientToken", "")

    def found(self, instance_id: str | None) -> None:
        if instance_id is not None and self.instance_id is None:
            self.instance_id, self.found_at = instance_id, datetime.now(UTC)


class PendingLaunches:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._by_slot: dict[int, PendingLaunch] = {}

    def __bool__(self) -> bool:
        return bool(self._by_slot)

    def __iter__(self):
        return iter(list(self._by_slot.values()))

    def slots(self) -> set[int]:
        return set(self._by_slot)

    def get(self, slot: int) -> PendingLaunch | None:
        return self._by_slot.get(slot)

    def reserve(self, slot: int, machine: str) -> PendingLaunch:
        if slot in self._by_slot:
            raise RuntimeError(f"slot {slot} already has a launch being settled")
        entry = PendingLaunch(slot, machine, self.clock())
        self._by_slot[slot] = entry
        return entry

    def drop(self, slot: int) -> None:
        """Only for a launch that started nothing: no request was sent, or the
        only request sent was refused."""
        self._by_slot.pop(slot, None)

    # ---- the database handoff ----

    def begin_tick(self) -> None:
        """Anything marked recorded but still here was not committed (the last
        tick's transaction failed), so it must be recorded again."""
        for entry in self._by_slot.values():
            entry.staged = entry.recorded = False

    def savepoint_done(self) -> None:
        for entry in self._by_slot.values():
            if entry.staged:
                entry.staged, entry.recorded = False, True

    def savepoint_failed(self) -> None:
        for entry in self._by_slot.values():
            entry.staged = False

    def committed(self) -> None:
        """The tick's transaction committed, so burst_slots now holds these."""
        for slot in [s for s, e in self._by_slot.items() if e.recorded]:
            del self._by_slot[slot]

    def view(self) -> list[PendingSlot]:
        """What decide counts. Entries written to burst_slots in this
        transaction are left out, because the snapshot already sees that row."""
        return [
            PendingSlot(
                e.slot, e.machine, e.client_token, e.instance_id or "", e.found_at
            )
            for e in self._by_slot.values()
            if not (e.staged or e.recorded)
        ]


def record(db: Session, entry: PendingLaunch) -> bool:
    """Write the instance id to its reserved slot row. Marks the entry staged
    (the caller's savepoint decides if it sticks).

    The row may already hold this same instance: an earlier commit succeeded
    but its reply was lost. That counts as recorded. A row holding a different
    instance is never overwritten."""
    row = slots.lock_slot(db, entry.slot)
    if row is None:
        log.error("slot %s has no row in burst_slots", entry.slot)
        return False
    if row.instance_id == entry.instance_id:
        entry.staged = True
        return True
    if row.instance_id is not None:
        log.error(
            "slot %s already records %s, not overwriting it with %s",
            entry.slot,
            row.instance_id,
            entry.instance_id,
        )
        return False
    slots.record_launch(db, row, entry.instance_id, entry.machine, entry.market)
    entry.staged = True
    return True


def _adopt(entry: PendingLaunch, live: list[dict]) -> str | None:
    """The one live burst instance with this slot and this ClientToken.
    find_instances already keeps only project=idlecloud instances."""
    if not entry.client_token:
        return None
    matches = [
        i["InstanceId"]
        for i in live
        if burst.slot_of(i) == entry.slot and i.get("ClientToken") == entry.client_token
    ]
    if len(matches) > 1:
        log.error(
            "slot %s: %d instances share token %s, adopting none: %s",
            entry.slot,
            len(matches),
            entry.client_token,
            ", ".join(matches),
        )
        return None
    return matches[0] if matches else None


def _warn_if_old(pending: PendingLaunches, entry: PendingLaunch) -> None:
    now = pending.clock()
    if now - entry.since < settings.PENDING_WARN_S:
        return
    if entry.warned_at is not None and now - entry.warned_at < settings.PENDING_WARN_S:
        return
    entry.warned_at = now
    log.warning(
        "slot %s: launch (%s, %s) unsettled for %.0f min. Its slot, budget and "
        "quota stay reserved, and it is replayed every tick with client token "
        "%s. EC2 not listing it does not prove nothing started, because "
        "listings lag. A restart drops this protection and accepts that risk.",
        entry.slot,
        entry.machine,
        entry.market or "no request",
        (now - entry.since) / 60,
        entry.client_token or "(none)",
    )


def resolve(db: Session, ec2, pending: PendingLaunches) -> None:
    """Try to settle every held launch. Each one in its own savepoint, so a
    failure only skips it."""
    live = None
    for entry in pending:
        if entry.recorded:
            continue
        if entry.request is None:
            # Reserved but nothing was ever sent (cannot normally survive apply)
            pending.drop(entry.slot)
            continue
        if entry.instance_id is None:
            if live is None:
                try:
                    live = burst.find_instances(ec2)
                except Exception:
                    log.warning("could not list burst instances", exc_info=True)
                    live = []
            entry.found(_adopt(entry, live))
            if entry.instance_id is not None:
                log.info(
                    "slot %s: adopted %s by its token", entry.slot, entry.instance_id
                )
        if entry.instance_id is None:
            try:
                entry.found(send(ec2, entry.request, entry.market))
                log.info("slot %s: replay returned %s", entry.slot, entry.instance_id)
            except Exception:
                # Never settles it: an earlier request may still have started one
                log.info(
                    "slot %s: replay did not settle the launch",
                    entry.slot,
                    exc_info=True,
                )
        if entry.instance_id is None:
            _warn_if_old(pending, entry)
            continue
        try:
            with db.begin_nested():
                record(db, entry)
        except Exception:
            pending.savepoint_failed()
            log.exception("slot %s: could not record %s", entry.slot, entry.instance_id)
        else:
            pending.savepoint_done()
