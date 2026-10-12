"""The autoscaler's rules. Pure functions only: a snapshot of the cluster goes in,
a list of actions comes out. Nothing here talks to AWS, Kubernetes or Postgres.

Scale-up works like a small Karpenter. It packs the stuck pods onto machines,
first onto workers that are already booting, then onto new ones. Each new
machine is the cheapest size that fits. All of them launch in the same tick,
within the free slots, the hourly budget and the AWS account quota.
"""

from dataclasses import dataclass, field
from datetime import datetime

from services.autoscaler.controller import settings
from services.autoscaler.controller.settings import CATALOG, MachineType

# ---------- what the loop observed ----------


@dataclass(frozen=True)
class StuckPod:
    name: str
    waited_s: float  # how long the scheduler has called it Unschedulable
    cpu_m: int
    mem_mib: float
    group: str  # replicas of one task share a group and need different nodes


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


@dataclass(frozen=True)
class ClaimedSlot:
    slot: int
    instance_id: str
    claimed_at: datetime
    machine: str  # instance type, for the budget


@dataclass(frozen=True)
class Instance:
    instance_id: str
    launched_at: datetime
    machine: str


@dataclass(frozen=True)
class AccountUsage:
    """Everything running in the AWS region, not just burst workers."""

    instances: int
    vcpus: int


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
    # Free (cpu_m, mem_mib) on each campus PC that takes pods
    campus_free: list[tuple[int, float]] = field(default_factory=list)


# ---------- what the loop should do ----------


@dataclass(frozen=True)
class Action:
    # launch, uncordon, drain, mark_empty, clear_empty, retire, free_slot,
    # kill_orphan, or hold (nothing to do, only logged)
    kind: str
    slot: int | None = None
    reason: str = ""
    instance_id: str | None = None
    machine: str | None = None


@dataclass
class _Plan:
    actions: list[Action] = field(default_factory=list)
    gone: set[int] = field(default_factory=set)  # slots being removed this tick

    def add(self, kind: str, slot: int | None = None, reason: str = "", **extra):
        self.actions.append(Action(kind, slot, reason, **extra))
        if kind in ("retire", "free_slot", "kill_orphan") and slot is not None:
            self.gone.add(slot)


def _age(now: datetime, since: datetime) -> float:
    return (now - since).total_seconds()


def pending_grace(waiting: int) -> float:
    """How long a pod must be stuck before we burst. One stuck pod waits the full
    time, in case a campus PC frees up. A big backlog will not be absorbed by
    campus PCs, so the wait shrinks with it, down to a floor."""
    return max(settings.MIN_PENDING_GRACE_S, settings.PENDING_GRACE_S / max(waiting, 1))


def smallest_fit(pod: StuckPod) -> MachineType | None:
    """The cheapest machine that can hold this pod, or None if none can."""
    fitting = [
        t
        for t in CATALOG.values()
        if pod.cpu_m <= t.cpu_m and pod.mem_mib <= t.free_mem_mib
    ]
    return min(fitting, key=lambda t: t.cost_per_hour, default=None)


def decide(s: Snapshot) -> list[Action]:
    plan = _Plan()
    _reconcile_with_ec2(s, plan)
    booting = _check_booting(s, plan)
    _scale_down_empty(s, plan)
    _scale_by_demand(s, plan, booting)
    return plan.actions


def _reconcile_with_ec2(s: Snapshot, plan: _Plan) -> None:
    """Fix drift between the burst_slots table and what really runs in EC2."""
    if s.instances is None:
        return
    for slot, claimed in s.claimed_slots.items():
        # The instance ended on its own: lifetime limit, failed boot or reclaim
        if (
            slot not in s.instances
            and _age(s.now, claimed.claimed_at) > settings.ORPHAN_GRACE_S
        ):
            plan.add("free_slot", slot, "its EC2 instance is gone")
    for slot, instance in s.instances.items():
        # Running in EC2 but not recorded, e.g. the autoscaler crashed mid-launch
        if (
            slot not in s.claimed_slots
            and _age(s.now, instance.launched_at) > settings.ORPHAN_GRACE_S
        ):
            plan.add(
                "kill_orphan",
                slot,
                "running but not in burst_slots",
                instance_id=instance.instance_id,
            )


def _check_booting(s: Snapshot, plan: _Plan) -> list[ClaimedSlot]:
    """Slots launched but not yet a Ready node. Give up on any that take too long."""
    booting = []
    for slot, claimed in s.claimed_slots.items():
        if slot in plan.gone:
            continue
        node = s.burst_nodes.get(slot)
        if node is not None and node.ready:
            continue
        if _age(s.now, claimed.claimed_at) > settings.BOOT_TIMEOUT_S:
            plan.add("retire", slot, f"did not join within {settings.BOOT_TIMEOUT_S}s")
        else:
            booting.append(claimed)
    return booting


def _active_nodes(s: Snapshot, plan: _Plan) -> list[BurstNode]:
    return [
        node
        for slot, node in sorted(s.burst_nodes.items())
        if slot in s.claimed_slots and slot not in plan.gone and node.ready
    ]


def _scale_down_empty(s: Snapshot, plan: _Plan) -> None:
    """Retire burst nodes that have had no task pods for EMPTY_GRACE_S, or that we
    were draining and have now emptied."""
    for node in _active_nodes(s, plan):
        if node.task_pods > 0:
            if node.empty_since is not None:
                plan.add("clear_empty", node.slot)
        elif node.draining:
            plan.add("retire", node.slot, "drained, campus PCs took over")
        elif node.empty_since is None:
            plan.add("mark_empty", node.slot)
        elif _age(s.now, node.empty_since) >= settings.EMPTY_GRACE_S:
            plan.add("retire", node.slot, f"no task pods for {settings.EMPTY_GRACE_S}s")


# ---------- scale up and down by demand ----------


@dataclass
class _Bin:
    """One machine in the packing plan, booting or new."""

    machine: MachineType
    new: bool
    opener: StuckPod | None = None  # the pod this new machine was opened for
    cpu_left: float = 0
    mem_left: float = 0
    groups: set[str] = field(default_factory=set)

    def __post_init__(self):
        self.cpu_left, self.mem_left = self.machine.cpu_m, self.machine.free_mem_mib

    @classmethod
    def settling(cls, node: "BurstNode") -> "_Bin":
        """A node that just turned Ready, minus what already runs on it."""
        b = cls(CATALOG.get(node.machine, settings.CHEAPEST), new=False)
        b.cpu_left -= node.load.cpu_m
        b.mem_left -= node.load.mem_mib
        b.groups = set(node.load.groups)
        return b

    def takes(self, pod: StuckPod) -> bool:
        return (
            pod.group not in self.groups
            and pod.cpu_m <= self.cpu_left
            and pod.mem_mib <= self.mem_left
        )

    def place(self, pod: StuckPod) -> None:
        self.cpu_left -= pod.cpu_m
        self.mem_left -= pod.mem_mib
        self.groups.add(pod.group)


def _pack(pods: list[StuckPod], existing: list[_Bin]) -> list[_Bin]:
    """First-fit decreasing bin packing. Returns the new machines needed.

    Pods go onto machines that are booting or just turned Ready first, so the
    next tick does not launch twice for the same pods. Two replicas of one task
    never share a machine, matching the coordinator's pod anti-affinity.
    """
    bins = [_copy_bin(b) for b in existing]
    for pod in sorted(pods, key=lambda p: (p.cpu_m, p.mem_mib), reverse=True):
        target = next((b for b in bins if b.takes(pod)), None)
        if target is None:
            machine = smallest_fit(pod)
            if machine is None:
                continue  # too big for any allowed machine
            target = _Bin(machine, new=True, opener=pod)
            bins.append(target)
        target.place(pod)
    return [b for b in bins if b.new]


def _copy_bin(b: _Bin) -> _Bin:
    c = _Bin(b.machine, new=b.new, opener=b.opener)
    c.cpu_left, c.mem_left, c.groups = b.cpu_left, b.mem_left, set(b.groups)
    return c


def plan_machines(
    pods: list[StuckPod], booting: list[MachineType]
) -> list[MachineType]:
    return [b.machine for b in _pack(pods, [_Bin(m, new=False) for m in booting])]


def _machine_of(claimed: ClaimedSlot) -> MachineType:
    return CATALOG.get(claimed.machine, settings.CHEAPEST)


def _scale_by_demand(s: Snapshot, plan: _Plan, booting: list[ClaimedSlot]) -> None:
    active = _active_nodes(s, plan)
    grace = pending_grace(len(s.unschedulable))
    stuck = [p for p in s.unschedulable if p.waited_s >= grace]

    if stuck:
        # Reopen nodes we were draining before paying for new ones
        draining = [node for node in active if node.draining]
        if draining:
            for node in draining:
                plan.add("uncordon", node.slot, f"{len(stuck)} task pods stuck again")
            return
        _launch_for(stuck, s, plan, booting)
        return

    # Campus PCs have room again and nothing is waiting: start emptying one burst
    # node. It takes no new pods, finishes what it runs, then _scale_down_empty
    # retires it. One per tick, so capacity drops gradually. A campus PC only
    # counts if it has room for at least the smallest pod on that burst node.
    if s.campus_free and s.unscheduled_pods == 0:
        candidates = [
            node
            for node in active
            if not node.draining and node.task_pods > 0 and _campus_can_take(s, node)
        ]
        if candidates:
            node = min(candidates, key=lambda n: n.task_pods)
            plan.add(
                "drain",
                node.slot,
                "a campus PC has room for its work and no pods are waiting",
            )


def _campus_can_take(s: Snapshot, node: BurstNode) -> bool:
    if node.load.smallest is None:
        return False
    cpu, mem = node.load.smallest
    return any(
        free_cpu >= cpu and free_mem >= mem for free_cpu, free_mem in s.campus_free
    )


def _launch_for(
    stuck: list[StuckPod], s: Snapshot, plan: _Plan, booting: list[ClaimedSlot]
) -> None:
    too_big = [p.name for p in stuck if smallest_fit(p) is None]
    if too_big:
        plan.add(
            "hold",
            None,
            f"pods {', '.join(too_big)} are bigger than any allowed machine",
        )

    # Running spend counts every slot still in use, booting or not
    spend = sum(
        _machine_of(c).cost_per_hour
        for slot, c in s.claimed_slots.items()
        if slot not in plan.gone
    )
    free_slots = s.free_slots
    if s.instances is not None:
        # A free slot whose old instance is still winding down cannot be used yet
        free_slots -= len([slot for slot in s.instances if slot not in s.claimed_slots])
    instances = s.account.instances if s.account else None
    vcpus = s.account.vcpus if s.account else None

    # Plan, then take the first new machine. Accept it if the limits allow,
    # otherwise drop the pod that needed it. Then plan again with what is left.
    # Every round accepts a machine or drops a pod, so this always ends.
    pods = [p for p in stuck if smallest_fit(p) is not None]
    bins = [_Bin(_machine_of(c), new=False) for c in booting]
    bins += [
        _Bin.settling(node)
        for node in _active_nodes(s, plan)
        if not node.draining
        and node.ready_since is not None
        and _age(s.now, node.ready_since) < settings.READY_SETTLE_S
    ]
    unaffordable: list[str] = []
    while True:
        wanted = _pack(pods, bins)
        if not wanted:
            break
        first = wanted[0]
        machine = first.machine
        # Slots and AWS quotas stop every further launch
        blocked = None
        if free_slots <= 0:
            blocked = "no free WireGuard slot"
        elif instances is not None and instances + 1 > settings.ACCOUNT_MAX_INSTANCES:
            blocked = f"AWS instance quota of {settings.ACCOUNT_MAX_INSTANCES} reached"
        elif vcpus is not None and vcpus + machine.vcpu > settings.ACCOUNT_MAX_VCPU:
            blocked = f"AWS vCPU quota of {settings.ACCOUNT_MAX_VCPU} reached"
        if blocked:
            plan.add("hold", None, f"{len(wanted)} more machines wanted, {blocked}")
            break
        # The budget only rules out this pod. Cheaper pods can still burst.
        if spend + machine.cost_per_hour > settings.BUDGET_USD_PER_HOUR + 1e-9:
            unaffordable.append(f"{first.opener.name} needs a {machine.name}")
            pods.remove(first.opener)
            continue
        plan.add(
            "launch",
            None,
            f"{len(stuck)} task pods stuck for over {pending_grace(len(s.unschedulable)):.0f}s",
            machine=machine.name,
        )
        bins.append(_Bin(machine, new=False))
        free_slots -= 1
        spend += machine.cost_per_hour
        if instances is not None:
            instances += 1
            vcpus += machine.vcpu

    if unaffordable:
        plan.add(
            "hold",
            None,
            f"budget US${settings.BUDGET_USD_PER_HOUR:.4f}/h reached at US${spend:.4f}/h: "
            + "; ".join(unaffordable),
        )
