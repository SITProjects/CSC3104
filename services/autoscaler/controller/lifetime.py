"""The lifetime limit on burst workers (plan Step 5, cost control).

No burst worker should run longer than MAX_LIFETIME_S (2 hours). Every limit is
worked out from the instance's EC2 launch time, so a restarted autoscaler keeps
the same schedule and needs no stored state. With the defaults:

- 110 min: the node is cordoned and never reopened, so running pods can finish
  but nothing new starts on it.
- 118 min: the instance gets the EC2 tag reclaim-at = launch time + 120 min,
  the same warning as an injected Spot reclaim. The Spot watcher on the node
  then taints it, writes its annotations and reports BUSY.
- 120 min: the reclaim rule ends that exact instance. This uses the launch
  time directly, so it still happens if the tag write failed or the watcher
  is not running.

The boot script's `shutdown -h +MAX_LIFETIME_MIN` stays as a backstop. It counts
from boot, which is after the launch time, so the autoscaler acts first.
120 min is a target. The autoscaler checks every INTERVAL_S plus the time a
tick takes, so it can be up to one tick late, longer if AWS calls fail.

Pure functions only.
"""

from datetime import datetime, timedelta

from services.autoscaler.controller import settings


def end_of_life(launched_at: datetime) -> datetime:
    """When this worker must be gone."""
    return launched_at + timedelta(seconds=settings.MAX_LIFETIME_S)


def retiring(launched_at: datetime, now: datetime) -> bool:
    """Close enough to the end that it should take no new pods."""
    return now >= end_of_life(launched_at) - timedelta(
        seconds=settings.RETIRE_CORDON_LEAD_S
    )


def warning_due(launched_at: datetime, now: datetime) -> bool:
    """Close enough to the end to warn it like a Spot reclaim."""
    return now >= end_of_life(launched_at) - timedelta(
        seconds=settings.RETIRE_WARN_LEAD_S
    )


def expired(launched_at: datetime, now: datetime) -> bool:
    return now >= end_of_life(launched_at)
