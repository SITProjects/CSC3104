"""Bin packing for scale-up: which new machines the waiting pods need.

First-fit decreasing. Pods go onto machines with room first (booting workers,
held launches, workers that just turned Ready), then onto new machines, each
the cheapest size that fits. Two replicas of one task never share a machine.
Pure functions only.
"""

from dataclasses import dataclass, field

from services.autoscaler.controller import settings
from services.autoscaler.controller.model import BurstNode, StuckPod
from services.autoscaler.controller.settings import CATALOG, MachineType


def smallest_fit(pod: StuckPod) -> MachineType | None:
    """The cheapest machine that can hold this pod, or None if none can."""
    fitting = [
        t
        for t in CATALOG.values()
        if pod.cpu_m <= t.cpu_m and pod.mem_mib <= t.free_mem_mib
    ]
    return min(fitting, key=lambda t: t.cost_per_hour, default=None)


@dataclass
class Bin:
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
    def room(cls, cpu_m: float, mem_mib: float, groups) -> "Bin":
        """Free room on an existing node, such as a campus PC."""
        b = cls(settings.CHEAPEST, new=False)
        b.cpu_left, b.mem_left, b.groups = cpu_m, mem_mib, set(groups)
        return b

    @classmethod
    def settling(cls, node: BurstNode) -> "Bin":
        """A node that just turned Ready, minus what already runs on it."""
        b = cls(settings.KNOWN_TYPES.get(node.machine, settings.CHEAPEST), new=False)
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


def pack(pods: list[StuckPod], existing: list[Bin]) -> list[Bin]:
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
            target = Bin(machine, new=True, opener=pod)
            bins.append(target)
        target.place(pod)
    return [b for b in bins if b.new]


def _copy_bin(b: Bin) -> Bin:
    c = Bin(b.machine, new=b.new, opener=b.opener)
    c.cpu_left, c.mem_left, c.groups = b.cpu_left, b.mem_left, set(b.groups)
    return c


def plan_machines(
    pods: list[StuckPod], booting: list[MachineType]
) -> list[MachineType]:
    return [b.machine for b in pack(pods, [Bin(m, new=False) for m in booting])]
