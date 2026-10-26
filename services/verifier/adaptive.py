import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from kubernetes import client
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.coordinator.crud import create_event, set_voting
from services.coordinator.k8s import cancel_k3_job
from shared.db.models import (
    Job,
    JobStatus,
    Node,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
)

from .k8s import create_kubernetes_client
from .trust import HIGH_TRUST_THRESHOLD
from .voting import MAX_REPLICAS, winning_hash

log = logging.getLogger("verifier")


def candidate_tasks() -> Select[tuple[Task, int]]:
    """
    Selects running or disputed tasks with a supported actual replica count.

    Includes expanded disputes, using the total copies currently assigned to
    the task. Shared by polling and locked rechecks for consistent eligibility.

    returns:
        Select: Tasks and their total replica counts, before ordering or locking.
    """
    replica_count = (
        select(func.count(Replica.id))
        .where(Replica.task_id == Task.id)
        .correlate(Task)
        .scalar_subquery()
    )
    return (
        select(Task, replica_count.label("replica_count"))
        .join(Job, Task.job_id == Job.id)
        .where(
            Task.status.in_([TaskStatus.RUNNING, TaskStatus.DISPUTED]),
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            replica_count.between(1, MAX_REPLICAS),
        )
    )


async def lock_candidate(
    session: AsyncSession, task_id: int
) -> tuple[Task, Sequence[Replica]] | None:
    """
    Locks and rechecks a candidate without waiting on the coordinator's locks.

    Locks the job, replicas and task, in that order. Rejects partial replica
    selections so SKIP LOCKED cannot accidentally lower the majority total.

    args:
        session: The caller's transaction, kept open through cancellation.
        task_id: The task to recheck under lock.
    returns:
        tuple[Task, Sequence[Replica]] | None: The locked task and all its copies,
            or None if ineligible or busy.
    """
    candidate = await session.scalar(
        candidate_tasks()
        .where(Task.id == task_id)
        .with_for_update(of=Job, skip_locked=True)
    )
    if candidate is None:
        return None
    replicas = (
        await session.scalars(
            select(Replica)
            .where(Replica.task_id == task_id)
            .order_by(Replica.id)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not replicas:
        return None
    row = (
        await session.execute(
            candidate_tasks()
            .where(Task.id == task_id)
            .with_for_update(of=Task, skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    task, total = row
    if len(replicas) != total:
        return None
    return task, replicas


async def find_early_agreement(
    session: AsyncSession, replicas: Sequence[Replica]
) -> tuple[str, list[Replica]] | None:
    """
    Finds a strict majority supported by distinct known high-trust PCs.

    Each PC contributes at most one vote to this early-stop decision. Failed,
    unfinished and cancelled copies still count towards the majority total.
    Shared node locks preserve the current trust scores until cancellation.

    args:
        session: The caller's transaction, holding the task and replica locks.
        replicas: Every replica for the task, including unsuccessful copies.
    returns:
        tuple[str, list[Replica]] | None: The winning hash and its high-trust
            supporting copies, or None if no eligible majority exists.
    """
    by_node = {
        replica.node_name: replica
        for replica in replicas
        if replica.status == ReplicaStatus.DONE
        and replica.exit_code == 0
        and replica.node_name
        and replica.node_name.strip()
    }
    if not by_node:
        return None
    trusted_names = (
        await session.scalars(
            select(Node.name)
            .where(Node.name.in_(by_node), Node.trust_score >= HIGH_TRUST_THRESHOLD)
            .order_by(Node.name)
            .with_for_update(read=True, skip_locked=True)
        )
    ).all()
    trusted = [by_node[name] for name in trusted_names]
    winner = winning_hash([replica.result_hash for replica in trusted], len(replicas))
    if winner is None:
        return None
    return winner, [replica for replica in trusted if replica.result_hash == winner]


async def cancel_redundant_replicas(
    session_factory: async_sessionmaker[AsyncSession], task_id: int
) -> list[tuple[int, int]]:
    """
    Cancels unfinished copies once a high-trust majority cannot be overturned.

    Preserves every replica row, so normal voting keeps the original majority
    threshold and scores all completed results. Invalidates cancelled tokens
    and records each attempt before moving the task to VOTING. Kubernetes
    deletion happens after commit, with existing housekeeping as recovery.

    args:
        session_factory: The shared async database session factory.
        task_id: The running or disputed task to consider for early stopping.
    returns:
        list[tuple[int, int]]: Cancelled replica ids and attempts after commit,
            or an empty list if no cancellation was made.
    """
    async with session_factory.begin() as session:
        candidate = await lock_candidate(session, task_id)
        if candidate is None:
            return []
        task, replicas = candidate
        unfinished = [
            replica
            for replica in replicas
            if replica.status
            in (
                ReplicaStatus.SCHEDULED,
                ReplicaStatus.RUNNING,
                ReplicaStatus.EVICTED,
                ReplicaStatus.LOST,
            )
        ]
        if not unfinished:
            return []
        agreement = await find_early_agreement(session, replicas)
        if agreement is None:
            return []
        winner, agreeing = agreement
        agreeing_ids = [replica.id for replica in agreeing]
        now = datetime.now(UTC)
        cancelled = []
        for replica in unfinished:
            replica.status = ReplicaStatus.CANCELLED
            replica.token_hash = None
            replica.ended_at = now
            # Keep the PC in details to avoid locking its node via the event FK.
            # create_event flushes each change before set_voting queries it.
            await create_event(
                session,
                "replica_cancelled",
                task.job_id,
                task.id,
                detail={
                    "reason": "high_trust_agreement",
                    "replica_id": replica.id,
                    "attempt": replica.attempt,
                    "node_name": replica.node_name,
                    "agreeing_replica_ids": agreeing_ids,
                    "result_hash": winner,
                    "total_replicas": len(replicas),
                },
            )
            cancelled.append((replica.id, replica.attempt))
        await set_voting(session, task)
    return cancelled


async def adaptive_loop(
    session_factory: async_sessionmaker[AsyncSession], interval: float = 5
) -> None:
    """
    Checks early-stop candidates and deletes cancelled replica Jobs after commit.

    Reuses Kubernetes configuration and deletion helpers. Blocking API calls
    run in threads. Database errors retry on later passes; deletion failures
    are recovered by the coordinator's existing stale-Job housekeeping.

    args:
        session_factory: The shared async database session factory.
        interval: Seconds between adaptive checks, including failed attempts.
    """
    api = None
    try:
        while True:
            try:
                if api is None:
                    core_api = await asyncio.to_thread(create_kubernetes_client)
                    api = client.BatchV1Api(core_api.api_client)
                async with session_factory() as session:
                    task_ids = (
                        await session.scalars(
                            candidate_tasks()
                            .with_only_columns(Task.id)
                            .order_by(Task.id)
                        )
                    ).all()
                for task_id in task_ids:
                    try:
                        cancelled = await cancel_redundant_replicas(
                            session_factory, task_id
                        )
                    except Exception:
                        log.exception(
                            "Adaptive check failed for task %s; will retry", task_id
                        )
                        continue
                    for replica_id, attempt in cancelled:
                        job_name = f"replica-{replica_id}-a{attempt}"
                        try:
                            await cancel_k3_job(job_name, api)
                            log.info(
                                "Stopped redundant replica %s for task %s",
                                job_name,
                                task_id,
                            )
                        except Exception:
                            log.exception(
                                "Could not delete %s; coordinator housekeeping will retry",
                                job_name,
                            )
            except Exception:
                log.exception("Adaptive replication failed; will retry")
            await asyncio.sleep(interval)
    finally:
        if api is not None:
            await asyncio.to_thread(api.api_client.close)
