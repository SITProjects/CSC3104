"""What the autoscaler observes and what it decides to do, as plain records.
The loop (__main__.py, cluster.py, slots.py, pending.py) fills them, the rules
in decide.py read them. No logic beyond small helpers."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import NamedTuple

# ---------- what the loop observed ----------


@dataclass(frozen=True)
class StuckPod:
    name: str
    waited_s: float  # how long the scheduler has called it Unschedulable
    cpu_m: int
    mem_mib: float
    group: str  # replicas of one task share a group and need different nodes
    replica: str = ""  # the coordinator's replica id, from the pod label


@dataclass(frozen=True)
class PodInfo:
    """A task pod's replica, task and requests, as remembered from a tick."""

    replica: str
    group: str
    cpu_m: int
    mem_mib: float
    node: str = ""  # the node it was on
    instance: str = ""  # the warned instance that node ran on (rescue.py sets it)


class CampusRoom(NamedTuple):
    """Free room on a campus PC that takes pods, and the tasks already on it."""

    cpu_m: int
    mem_mib: float
    name: str = ""
    groups: frozenset = frozenset()


@dataclass
class NodeLoad:
    """The task pods bound to one node and what they request."""

    pods: int = 0
    cpu_m: int = 0
    mem_mib: float = 0
    groups: set[str] = field(default_factory=set)
    smallest: tuple[int, float] | None = None  # (cpu_m, mem_mib) of the smallest pod

    def add(self, cpu_m: int, mem_mib: float, group: str) -> None:
        self.pods += 1
        self.cpu_m += cpu_m
        self.mem_mib += mem_mib
        self.groups.add(group)
        if self.smallest is None or (cpu_m, mem_mib) < self.smallest:
            self.smallest = (cpu_m, mem_mib)


@dataclass(frozen=True)
class BurstNode:
    slot: int
    name: str
    ready: bool
    draining: bool  # we cordoned it to let it empty out
    task_pods: int
    empty_since: datetime | None
    machine: str = ""  # from the node's idlecloud/instance-type label
    ready_since: datetime | None = None
    load: NodeLoad = field(default_factory=NodeLoad)
    # EC2 instance the node runs on, from its idlecloud/instance-id label
    instance_id: str = ""
    # The node object's uid, so a patch never lands on a replacement node
    uid: str = ""
    # Deadline the Spot watcher wrote on the node (idlecloud/reclaim-at)
    reclaim_at: datetime | None = None


@dataclass(frozen=True)
class ClaimedSlot:
    slot: int
    instance_id: str
    claimed_at: datetime
    machine: str  # instance type, for the budget


@dataclass(frozen=True)
class PendingSlot:
    """A launch not yet recorded in burst_slots (controller/pending.py). Its
    slot is reserved, and it counts like a booting worker until recorded."""

    slot: int
    machine: str
    client_token: str = ""
    instance_id: str = ""  # known once AWS answered, before the row is written
    found_at: datetime | None = None  # when instance_id became known


@dataclass(frozen=True)
class Instance:
    instance_id: str
    launched_at: datetime
    machine: str
    slot: int | None = None
    # Deadline from the EC2 tag reclaim-at, set by `launch reclaim`
    reclaim_at: datetime | None = None
    # The idempotency token of the request that started it
    client_token: str = ""


@dataclass(frozen=True)
class AccountUsage:
    """Everything running in the AWS region, not just burst workers."""

    instances: int
    vcpus: int
    # What this listing saw, so a worker another listing already shows is
    # still reserved until this one counts it too
    ids: frozenset[str] = frozenset()
    slot_tokens: frozenset[tuple[int, str]] = frozenset()  # (slot, ClientToken)


@dataclass
class Snapshot:
    now: datetime
    unscheduled_pods: int  # task pods with no node yet, for any reason
    unschedulable: list[StuckPod]  # those the scheduler could not place
    idle_campus_nodes: int  # campus PCs that take pods (Ready, uncordoned, owner away)
    burst_nodes: dict[int, BurstNode]  # by slot
    claimed_slots: dict[int, ClaimedSlot]  # by slot
    free_slots: int
    # Burst instances alive in EC2, by slot, and the account totals.
    # None when EC2 was not checked (dry run).
    instances: dict[int, Instance] | None = None
    account: AccountUsage | None = None
    # Free room on each campus PC that takes pods
    campus_free: list[CampusRoom] = field(default_factory=list)
    # Extra live instances on a slot that already has one in `instances`.
    # Should never happen, but each still costs money.
    duplicates: list[Instance] = field(default_factory=list)
    # Proactive replacement. `rescue` holds replicas that ran on a worker when
    # its reclaim warning arrived (rescue.py remembers them). `bound` maps each
    # replica to the nodes it has a pod on, and `waiting` lists replicas whose
    # pod has no node yet.
    rescue: list[PodInfo] = field(default_factory=list)
    bound: dict[str, set[str]] = field(default_factory=dict)
    waiting: set[str] = field(default_factory=set)
    # Replicas known to need no more work: their pod succeeded, or the
    # coordinator marks the replica, its task or its job as finished
    finished: set[str] = field(default_factory=set)
    # Launches whose instance id is not in burst_slots yet (pending.py)
    pending: list[PendingSlot] = field(default_factory=list)


# ---------- what the loop should do ----------


@dataclass(frozen=True)
class Action:
    # launch, uncordon, drain, mark_empty, clear_empty, retire, free_slot,
    # kill_orphan, kill_duplicate, reclaim, warn_retire,
    # or hold (nothing to do, only logged)
    kind: str
    slot: int | None = None
    reason: str = ""
    instance_id: str | None = None
    machine: str | None = None
    deadline: datetime | None = None  # warn_retire: the reclaim-at tag to write


def parse_utc(text: str | None) -> datetime | None:
    """A reclaim deadline like 2026-11-01T12:02:00Z. A time without a zone is
    UTC. Unreadable text gives None, the same as the Spot watcher."""
    try:
        moment = datetime.fromisoformat(str(text).strip())
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
