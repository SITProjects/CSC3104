"""Which EC2 market a burst worker uses (BURST_MARKET), and what a launch
request's answer means.

on-demand  Never tries Spot.
spot       Spot only. A refused Spot launch fails, and the next tick retries.
auto       Spot first. After one of the refusals below the same launch is
           repeated once as On-Demand, and the chooser remembers why:
           - Spot not allowed for the account: On-Demand for good.
           - Spot not allowed for one size (checked with a dry run): On-Demand
             for good for that size.
           - No Spot capacity for one size: that size waits a cooldown.
           - Spot quota used up: every size waits a cooldown (all T3 sizes
             share the account's Standard Spot vCPU quota).

Every answer to a launch request is one of three things:
- started: AWS returned the instance.
- refused: an error whose documented meaning is that AWS started nothing
  (see REFUSED). Only these may fall back or be forgotten.
- unknown: anything else, such as a network error, a timeout, an AWS server
  error, an unrecognised code, or SpotMaxPriceTooLow (AWS leaves that request
  open). LaunchUnknown carries the exact request, so the caller can keep it and
  replay it with the same ClientToken (controller/pending.py).

The chooser's memory lives in this process only. After a restart one Spot
launch may be refused again, and the chooser learns again. Time comes from a
monotonic clock, so a change of the wall clock cannot end a cooldown early.
"""

import logging
import time
from collections.abc import Callable

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

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

# Errors whose documented meaning is that the request was rejected and nothing
# started: credentials and permissions, throttling, account limits, capacity,
# and bad requests (codes starting with Invalid, Missing or Unsupported).
REFUSED = (
    NOT_ALLOWED
    | CHECK_WITH_DRY_RUN
    | NO_CAPACITY
    | QUOTA_FULL
    | {
        "AuthFailure",
        "Blocked",
        "ExpiredToken",
        "InvalidClientTokenId",
        "MissingAuthenticationToken",
        "OptInRequired",
        "PendingVerification",
        "RequestExpired",
        "SignatureDoesNotMatch",
        "RequestLimitExceeded",
        "Throttling",
        "InstanceLimitExceeded",
        "VcpuLimitExceeded",
        "InsufficientFreeAddressesInSubnet",
    }
)
REFUSED_PREFIXES = ("Invalid", "Missing", "Unsupported")
# Never a refusal, whatever the lists above say: the Spot request stays open
# and may still start an instance later
LEFT_OPEN = {"SpotMaxPriceTooLow"}


class LaunchUnknown(Exception):
    """A launch request whose outcome is unknown. It may have started an
    instance. `request` is exactly what was sent, ClientToken included."""

    def __init__(self, request: dict, market: str, cause: Exception):
        super().__init__(f"launch outcome unknown ({market}): {cause!r}")
        self.request = request
        self.market = market
        self.cause = cause


def error_code(error: ClientError) -> str:
    return error.response.get("Error", {}).get("Code", "")


def is_refusal(error: Exception) -> bool:
    """Whether this error means AWS started nothing for this request. Only
    true for a documented refusal. It says nothing about earlier requests."""
    if not isinstance(error, ClientError):
        return False
    code = error_code(error)
    if code in LEFT_OPEN:
        return False
    return code in REFUSED or code.startswith(REFUSED_PREFIXES)


def single_attempt(ec2) -> bool:
    """Whether this client makes exactly one attempt per call (aws.ONE_ATTEMPT).
    A client without boto3 settings (a test fake) makes one."""
    meta = getattr(ec2, "meta", None)
    if meta is None:
        return True
    retries = getattr(meta.config, "retries", None) or {}
    return retries.get("total_max_attempts") == 1


def send(ec2, request: dict, market: str) -> str:
    """One run_instances call. Returns the instance id, raises the ClientError
    for a refusal, and LaunchUnknown for anything else.

    An error is only trusted as a refusal when the client made one attempt.
    With boto3's own retries, an earlier attempt may have timed out after
    starting an instance, and only the last answer is reported."""
    try:
        return ec2.run_instances(**request)["Instances"][0]["InstanceId"]
    except ParamValidationError:
        raise  # boto3 rejected the request before sending it: nothing started
    except Exception as error:
        if is_refusal(error) and single_attempt(ec2):
            raise
        raise LaunchUnknown(request, market, error) from error


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


def _ignore(request: dict, market: str) -> None:
    return None


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
        self,
        ec2,
        params_for: Callable[[bool], dict],
        instance_type: str,
        on_request: Callable[[dict, str], None] = _ignore,
    ) -> tuple[str, str]:
        """Start one instance. params_for(spot) builds the full request, the
        same apart from the market, with a new ClientToken each time.
        on_request(request, market) is called just before each request is sent,
        so the caller always holds the request that may have started a machine.
        Returns (instance id, market used)."""
        if not self.wants_spot(instance_type):
            return self._send(ec2, params_for(False), "on-demand", on_request)
        try:
            return self._send(ec2, params_for(True), "spot", on_request)
        except ClientError as error:
            # Only a refusal reaches here (send turns everything else into
            # LaunchUnknown), and only auto may fall back after one it knows.
            if self.mode != "auto":
                raise
            reason = self._learn(ec2, error, params_for, instance_type)
            if reason is None:
                raise
            log.warning("Spot launch of %s refused: %s", instance_type, reason)
        # A new request with a new ClientToken. The Spot one started nothing.
        return self._send(ec2, params_for(False), "on-demand", on_request)

    @staticmethod
    def _send(ec2, request: dict, market: str, on_request) -> tuple[str, str]:
        on_request(request, market)
        return send(ec2, request, market), market

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
