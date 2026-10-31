"""
Trust-based spot checks, so trusted PCs don't need extra copies of every task.

Every task starts with one copy. When it finishes, accept_on_trust decides:
a trusted PC's result is accepted, except for a random SPOT_CHECK_RATE share
that gets a second copy on another PC. A PC that isn't trusted always gets the
second copy. Copies that disagree go to the normal dispute.

When a PC loses a vote, the results it got accepted on trust alone are
reopened at once (reopen_trusted_results). This only covers jobs still
running: a finished job's output has already been published.
"""

import logging
import os
import random
from collections.abc import Callable

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.crud import create_event, create_replicas
from shared.db.models import Job, JobStatus, JobType, Node, Replica, Task, TaskStatus

from .trust import HIGH_TRUST_THRESHOLD

log = logging.getLogger("verifier")

ACTIVE_JOBS = (JobStatus.PENDING, JobStatus.RUNNING)


def read_rate(value: str | None) -> float:
    """
    Parses SPOT_CHECK_RATE: the share of trusted results that get a second copy.

    0 accepts every trusted result, 1 checks every result. Defaults to 0.1.

    raises:
        ValueError: If the value is not a number between 0 and 1.
    """
    rate = float(value) if value not in (None, "") else 0.1
    if not 0 <= rate <= 1:
        raise ValueError(f"SPOT_CHECK_RATE must be between 0 and 1, got {rate}")
    return rate


SPOT_CHECK_RATE = read_rate(os.environ.get("SPOT_CHECK_RATE"))
# Unpredictable, so a PC can't guess which of its results will be checked.
_draw = random.SystemRandom().random


async def accept_on_trust(
    session: AsyncSession,
    replica: Replica,
    rate: float | None = None,
    draw: Callable[[], float] | None = None,
) -> bool:
    """
    Decides whether a single successful copy can be accepted without a check.

    Takes a share lock on the PC's node row, so a trust change being voted at
    the same time is either seen or waits for this decision.

    args:
        session: The verifier's transaction, holding the task lock.
        replica: The task's only copy, DONE with exit code 0.
        rate: The share of trusted results to check, SPOT_CHECK_RATE by default.
        draw: Returns a number in [0, 1), replaced in tests.
    returns:
        bool: True to accept, False to run a second copy on another PC.
    """
    if not replica.node_name:
        return False
    trust = await session.scalar(
        select(Node.trust_score)
        .where(Node.name == replica.node_name)
        .with_for_update(read=True)
    )
    if trust is None or trust < HIGH_TRUST_THRESHOLD:
        return False
    rate = SPOT_CHECK_RATE if rate is None else rate
    return (draw or _draw)() >= rate


async def reopen_trusted_results(
    session: AsyncSession, node_names: list[str]
) -> list[int]:
    """
    Reopens results these PCs got accepted on trust alone, in running jobs.

    Called in the same transaction that lowers the PCs' trust, so a re-check
    can't be lost or skipped. Each task goes back to DISPUTED with one more
    copy, which the scheduler keeps off the PC that ran the first one.

    Locks each affected job too. A job being completed at the same moment
    either completes first (and is left alone, its output is published) or
    sees the reopened task and waits.

    args:
        session: The verifier's transaction, holding the voted task's lock.
        node_names: PCs that just lost a vote.
    returns:
        list[int]: The reopened task ids.
    """
    copies = (
        select(func.count(Replica.id))
        .where(Replica.task_id == Task.id)
        .correlate(Task)
        .scalar_subquery()
    )
    ran_on_them = exists().where(
        Replica.task_id == Task.id, Replica.node_name.in_(node_names)
    )
    tasks = (
        await session.scalars(
            select(Task)
            .join(Job, Task.job_id == Job.id)
            .join(JobType, Job.job_type == JobType.name)
            .where(
                # a job type without voting also runs once, but on purpose
                JobType.verify,
                Task.status == TaskStatus.VERIFIED,
                Job.status.in_(ACTIVE_JOBS),
                copies == 1,
                ran_on_them,
            )
            .order_by(Task.id)
            .with_for_update(of=(Task, Job))
        )
    ).all()
    for task in tasks:
        task.status = TaskStatus.DISPUTED
        task.final_hash = None
        extra = await create_replicas(session, [task], 1)
        await create_event(
            session,
            "task_rechecked",
            task.job_id,
            task.id,
            detail={"replica_ids": [replica.id for replica in extra]},
        )
        log.info("Re-checking task %s: its PC lost a vote", task.id)
    return [task.id for task in tasks]
