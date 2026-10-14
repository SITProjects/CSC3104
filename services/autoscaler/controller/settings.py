"""Autoscaler tuning. Each value can be overridden with an environment variable.

Hard numbers here are facts (machine sizes, AWS prices, AWS Academy quotas) or
safety limits. How many workers to run, and which size, is worked out every
tick from the waiting pods. Nothing here fixes the number of workers.
"""

import os
from dataclasses import dataclass

from core.settings import JOB_NAMESPACE  # noqa: F401  (re-exported for the controller)
from services.autoscaler.burst import config as burst_config


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# ---------- timing ----------

INTERVAL_S = _int("AUTOSCALER_INTERVAL_S", 30)
# How long one stuck pod waits for a campus PC before we pay for EC2. The wait
# shrinks as the backlog grows (see decide.pending_grace), down to the floor.
PENDING_GRACE_S = _int("AUTOSCALER_PENDING_GRACE_S", 120)
MIN_PENDING_GRACE_S = _int("AUTOSCALER_MIN_PENDING_GRACE_S", 30)
# For this long after a burst node turns Ready, the planner still counts its free
# room. The scheduler can take a few tries to place waiting pods on a new node,
# and without this the same pods would trigger a second launch.
READY_SETTLE_S = _int("AUTOSCALER_READY_SETTLE_S", 90)
# A burst node with no task pods this long is terminated
EMPTY_GRACE_S = _int("AUTOSCALER_EMPTY_GRACE_S", 300)
# A burst worker that has not joined the cluster by then is treated as failed
BOOT_TIMEOUT_S = _int("AUTOSCALER_BOOT_TIMEOUT_S", 600)
# An EC2 instance or slot row younger than this is never treated as an orphan,
# so a launch that is still being recorded is not cleaned up by mistake
ORPHAN_GRACE_S = _int("AUTOSCALER_ORPHAN_GRACE_S", 120)
# How long after a reclaim deadline its replicas still count as demand for
# proactive replacement. By then they should be requeued as waiting pods.
RESCUE_KEEP_S = _int("AUTOSCALER_RESCUE_KEEP_S", 120)
# A launch whose outcome is unknown keeps its slot until its instance is found
# (controller/pending.py). It is never released automatically. After this long,
# and again every time this passes, it is logged as a warning.
PENDING_WARN_S = _int("AUTOSCALER_PENDING_WARN_S", 600)
# Log what would happen, but change nothing in AWS, Kubernetes or the database
DRY_RUN = os.environ.get("AUTOSCALER_DRY_RUN", "") == "1"


# ---------- machine sizes ----------


@dataclass(frozen=True)
class MachineType:
    name: str
    vcpu: int
    mem_mib: int
    # On-Demand Linux, us-east-1. Spot workers are budgeted at this price too.
    # We never set a Spot maximum price, so AWS caps Spot at the On-Demand
    # price and this is a safe upper bound for compute. It is not the real
    # cost per job.
    usd_per_hour: float

    @property
    def cpu_m(self) -> int:
        """Millicores left for task pods after K3s, WireGuard and the OS."""
        return self.vcpu * 1000 - RESERVED_CPU_M

    @property
    def free_mem_mib(self) -> int:
        return self.mem_mib - RESERVED_MEM_MIB

    @property
    def cost_per_hour(self) -> float:
        """Instance plus its EBS disk (gp3 at US$0.08 per GB-month)."""
        return self.usd_per_hour + burst_config.DISK_GB * 0.08 / 730


# What a node keeps back from task pods. This must match what the scheduler
# sees, or the plan and the scheduler disagree. K3s reserves no CPU by default,
# so a 2-core pod does fit a 2 vCPU machine. Memory is lower than the advertised
# size: the kernel keeps some, and K3s evicts pods below 100 MiB free.
# The Spot watcher pod on each burst node requests no CPU and this much memory
# (spotwatch/spot-watcher.yml). Change both together.
SPOT_WATCHER_MEM_MIB = 32
RESERVED_CPU_M = 0
RESERVED_MEM_MIB = 400 + SPOT_WATCHER_MEM_MIB

# Every T3 size, so the budget can price any burst instance it finds, even one
# the autoscaler would not launch itself (started by hand, or a size removed
# from BURST_INSTANCE_TYPES since). T3 only, because the launch template sets
# T-family CPU credits.
KNOWN_TYPES = {
    t.name: t
    for t in [
        MachineType("t3.nano", 2, 512, 0.0052),
        MachineType("t3.micro", 2, 1024, 0.0104),
        MachineType("t3.small", 2, 2048, 0.0208),
        MachineType("t3.medium", 2, 4096, 0.0416),
        MachineType("t3.large", 2, 8192, 0.0832),
        MachineType("t3.xlarge", 4, 16384, 0.1664),
        MachineType("t3.2xlarge", 8, 32768, 0.3328),
    ]
}
# Sizes the autoscaler launches. AWS Academy allows up to large, and a nano
# has almost no memory left for pods. Bigger pods than t3.large never burst.
_LAUNCHABLE = ["t3.micro", "t3.small", "t3.medium", "t3.large"]
_allowed = os.environ.get("BURST_INSTANCE_TYPES", "")
CATALOG = {
    name: KNOWN_TYPES[name]
    for name in _LAUNCHABLE
    if not _allowed or name in _allowed.split(",")
}
CHEAPEST = min(CATALOG.values(), key=lambda t: t.cost_per_hour)
DEAREST = max(CATALOG.values(), key=lambda t: t.cost_per_hour)


def cost_per_hour(machine: str) -> float | None:
    """What a burst instance of this size costs per hour, or None if unknown.
    An empty name is a worker we launched whose size is not visible yet. It is
    one of the sizes we launch, so it costs at most the dearest of those. Any
    other size outside the T3 table has no known price, and the autoscaler
    launches nothing more until it is gone or added to KNOWN_TYPES."""
    if machine in KNOWN_TYPES:
        return KNOWN_TYPES[machine].cost_per_hour
    if not machine:
        return DEAREST.cost_per_hour
    return None


# ---------- Spot or On-Demand ----------

# on-demand, spot or auto (see burst/market.py). On-Demand by default until the
# coordinator requeues evicted replicas. Without that, a real Spot reclaim
# loses the running work.
BURST_MARKET = os.environ.get("BURST_MARKET", "on-demand")
# How long auto skips Spot after AWS has no Spot capacity (or quota) for it
SPOT_COOLDOWN_S = _int("BURST_SPOT_COOLDOWN_S", 1800)


# ---------- spending and quotas ----------

# Each task runs 3 replicas on 3 different nodes. The default budget is the least
# that still lets one task run entirely in the cloud: 3 of the cheapest machine,
# about US$0.04 an hour. Raise it to let bigger or more jobs burst.
REPLICAS_PER_TASK = 3
BUDGET_USD_PER_HOUR = float(
    os.environ.get(
        "BURST_BUDGET_USD_PER_HOUR",
        round(REPLICAS_PER_TASK * CHEAPEST.cost_per_hour, 4),
    )
)
# AWS Academy Learner Lab: at most 9 instances and 32 vCPUs running in a region,
# counting the control plane and anything else in the account
ACCOUNT_MAX_INSTANCES = _int("AWS_MAX_INSTANCES", 9)
ACCOUNT_MAX_VCPU = _int("AWS_MAX_VCPU", 32)


# ---------- cluster names from the implementation plan ----------

ROLE_LABEL = "idlecloud/role"
POOL_LABEL = "idlecloud/pool"
TASK_LABEL = "idlecloud/task-id"
REPLICA_LABEL = "idlecloud/replica-id"  # set by the coordinator on each pod
TYPE_LABEL = "idlecloud/instance-type"
# The EC2 instance id a burst node runs on, set by the node itself at join
INSTANCE_ID_LABEL = "idlecloud/instance-id"
OWNER_TAINT = "idlecloud/owner-active"
# Node annotations the autoscaler keeps on burst nodes. Kept on the node, not in
# memory, so a restarted autoscaler carries on where the last one stopped.
EMPTY_SINCE_ANNOTATION = "idlecloud/empty-since"
DRAINING_ANNOTATION = "idlecloud/draining"
# Written by the Spot watcher (spotwatch/watcher.py) when a reclaim warning arrives
RECLAIM_AT_ANNOTATION = "idlecloud/reclaim-at"
RECLAIM_SOURCE_ANNOTATION = "idlecloud/reclaim-source"

# Postgres advisory lock, so only one autoscaler copy acts per tick
LOCK_ID = 31046
