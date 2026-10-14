"""Which EC2 market a burst worker uses: Spot or On-Demand (BURST_MARKET).

on-demand  Never tries Spot.
spot       Spot only. A refused Spot launch fails, and the next tick retries.
auto       Spot first. After a clear AWS refusal the same launch is repeated
           once as On-Demand, and the chooser remembers why:
           - Spot not allowed for the account: On-Demand for good.
           - Spot not allowed for one size (checked with a dry run): On-Demand
             for good for that size.
           - No Spot capacity for one size: that size waits a cooldown.
           - Spot quota used up: every size waits a cooldown (all T3 sizes
             share the account's Standard Spot vCPU quota).
           Any other error changes nothing and is raised as before.

The memory lives in this process only. After a restart one Spot launch may be
refused again, and the chooser learns again. Time comes from a monotonic clock,
so a change of the wall clock cannot end a cooldown early.
"""

import logging
import time
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError

log = logging.getLogger("autoscaler")

MODES = ("on-demand", "spot", "auto")

# Spot needs the AWSServiceRoleForEC2Spot role. Credentials that may not create
# it can never launch Spot, whatever the size.
NOT_ALLOWED = {"AuthFailure.ServiceLinkedRoleCreationNotPermitted"}
# Also raised for blocked sizes or missing permissions, so a dry run decides
CHECK_WITH_DRY_RUN = {"UnauthorizedOperation"}
# No spare Spot machines of this size right now
NO_CAPACITY = {"InsufficientInstanceCapacity", "UnfulfillableCapacity"}
# The Standard Spot vCPU quota is shared by all our sizes
QUOTA_FULL = {"MaxSpotInstanceCountExceeded"}
# Not handled on purpose: SpotMaxPriceTooLow leaves the Spot request open, so it
# could still start later next to an On-Demand fallback. We never set a maximum
# price, so the default (the On-Demand price) makes it very unlikely anyway.


def error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


def run(ec2, params: dict) -> str:
    """One run_instances call. boto3 adds a ClientToken, so its own retries of
    this call can never start a second instance."""
    return ec2.run_instances(**params)["Instances"][0]["InstanceId"]


def on_demand_allowed(ec2, params: dict) -> bool:
    """Whether this exact On-Demand request would be allowed. A dry run starts
    nothing and costs nothing. Only DryRunOperation means yes. Anything else,
    including a network error, means we cannot tell."""
    try:
        ec2.run_instances(**params, DryRun=True)
    except ClientError as error:
        return error_code(error) == "DryRunOperation"
    except BotoCoreError:
        return False
    return False  # a dry run always raises, so this is not expected


class MarketChooser:
    """Picks the market for each launch and learns from Spot refusals. Keep one
    for the life of the process, so what it learns carries across ticks."""

    def __init__(
        self,
        mode: str,
        cooldown_s: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        if mode not in MODES:
            raise ValueError(f"BURST_MARKET must be one of {', '.join(MODES)}")
        self.mode = mode
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.blocked_all = False
        self.blocked_types: set[str] = set()
        self.cool_all_until: float | None = None
        self.cool_until: dict[str, float] = {}

    def wants_spot(self, instance_type: str) -> bool:
        if self.mode != "auto":
            return self.mode == "spot"
        if self.blocked_all or instance_type in self.blocked_types:
            return False
        now = self.clock()
        if self.cool_all_until is not None and now < self.cool_all_until:
            return False
        until = self.cool_until.get(instance_type)
        return until is None or now >= until

    def launch(
        self, ec2, params_for: Callable[[bool], dict], instance_type: str
    ) -> tuple[str, str]:
        """Start one instance. params_for(spot) builds the full request, the
        same apart from the market. Returns (instance id, market used)."""
        if not self.wants_spot(instance_type):
            return run(ec2, params_for(False)), "on-demand"
        try:
            return run(ec2, params_for(True)), "spot"
        except ClientError as error:
            # Only auto may fall back, and only after one of the specific
            # refusals in _learn, which mean AWS started nothing. A ClientError
            # alone does not prove that (an AWS 5xx may hide a launch), and
            # network errors are not ClientErrors, so neither falls back.
            if self.mode != "auto":
                raise
            reason = self._learn(ec2, error, params_for, instance_type)
            if reason is None:
                raise
            log.warning("Spot launch of %s refused: %s", instance_type, reason)
        return run(ec2, params_for(False)), "on-demand"

    def _learn(
        self,
        ec2,
        error: ClientError,
        params_for: Callable[[bool], dict],
        instance_type: str,
    ) -> str | None:
        """Remember what this refusal means and say why, or None if it is not a
        reason to use On-Demand."""
        code = error_code(error)
        minutes = f"{self.cooldown_s / 60:.0f} min"
        if code in NO_CAPACITY:
            self.cool_until[instance_type] = self.clock() + self.cooldown_s
            return f"{code}, Spot {instance_type} paused for {minutes}"
        if code in QUOTA_FULL:
            self.cool_all_until = self.clock() + self.cooldown_s
            return f"{code}, all Spot sizes paused for {minutes}"
        if code in NOT_ALLOWED:
            self.blocked_all = True
            return f"{code}, On-Demand only from now on"
        if code in CHECK_WITH_DRY_RUN and on_demand_allowed(ec2, params_for(False)):
            self.blocked_types.add(instance_type)
            return f"{code}, On-Demand only for {instance_type} from now on"
        return None
