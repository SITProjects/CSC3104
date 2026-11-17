"""
Platform scoring of trained models (GPU plan, phase 3).

A job type with a check_image (pytorch-gpu) whose task has an evaluation file
is not accepted on the training copy's exit code alone. Once training
succeeded, a check copy scores the returned model on a checker node
(worker/model-check), and only then is the task VERIFIED. The score is a
result, not a vote: no PC's trust changes, however low it is.
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.coordinator.crud import create_event
from shared.db.models import Job, JobType, Replica, ReplicaStatus, Task, TaskStatus

log = logging.getLogger("verifier")

# Checks of one model that may fail (crash, time out, lose their node) before
# the task is accepted as "not scored". A checker failure is never the user's.
CHECK_ATTEMPTS = 3


async def is_scored(session: AsyncSession, task: Task) -> bool:
    """Whether this task's model gets a check: its type has a checker image
    and the owner gave an evaluation file."""
    if not (task.input or {}).get("eval_file"):
        return False
    check_image = await session.scalar(
        select(JobType.check_image)
        .join(Job, Job.job_type == JobType.name)
        .where(Job.id == task.job_id)
    )
    return bool(check_image)


async def score_or_check(
    session: AsyncSession, task: Task, trained: Replica, checks: list[Replica]
) -> None:
    """
    Finishes a successful training copy's task once its model was checked,
    otherwise starts a check copy and leaves the task SCORING.

    args:
        session: The verifier's current transaction.
        task: The locked VOTING task.
        trained: The successful training copy whose result is accepted.
        checks: The task's check copies, all finished.
    """
    mine = [c for c in checks if c.check_of == trained.id]
    done = [c for c in mine if c.status == ReplicaStatus.DONE and c.check_status]
    if done:
        check = max(done, key=lambda c: c.id)
        task.status = TaskStatus.VERIFIED
        task.final_hash = trained.result_hash
        await create_event(
            session,
            "task_scored" if check.check_status == "scored" else "task_not_scored",
            task.job_id,
            task.id,
            check.node_name,
            {
                "replica_id": trained.id,
                "check_id": check.id,
                "score": check.check_score,
                "samples": check.check_samples,
                "model_sha256": check.check_model_sha256,
                "eval_sha256": check.check_eval_sha256,
                "evaluator": check.check_evaluator,
                "reason": check.check_reason,
            },
        )
        return
    if len(mine) >= CHECK_ATTEMPTS:
        # the training worked; only the platform's checker didn't
        task.status = TaskStatus.VERIFIED
        task.final_hash = trained.result_hash
        await create_event(
            session,
            "task_not_scored",
            task.job_id,
            task.id,
            None,
            {"replica_id": trained.id, "reason": "checker_failed", "checks": len(mine)},
        )
        log.warning("Task %s: %d checks failed, not scored", task.id, len(mine))
        return
    check = Replica(
        task_id=task.id,
        attempt=1,
        status=ReplicaStatus.SCHEDULED,
        check_of=trained.id,
    )
    session.add(check)
    await session.flush()
    task.status = TaskStatus.SCORING
    task.final_hash = None
    await create_event(
        session,
        "task_check_started",
        task.job_id,
        task.id,
        None,
        {"replica_id": trained.id, "check_id": check.id, "check_number": len(mine) + 1},
    )
