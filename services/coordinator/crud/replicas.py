import hashlib
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from core.settings import MAX_ATTEMPTS, RETRY_ATTEMPTS
from shared.db.models import (
    Job,
    JobStatus,
    JobType,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
)

from .tasks import set_voting


async def fetch_running_replicas(
    session: AsyncSession, older_than: datetime
) -> list[tuple[Replica, UUID]]:
    """Snapshot running replicas past the launch grace period, with their job ids."""
    result = await session.execute(
        select(Replica, Task.job_id)
        .join(Task, Replica.task_id == Task.id)
        .where(
            Replica.status == ReplicaStatus.RUNNING,
            Replica.started_at < older_than,
        )
    )
    return [(replica, job_id) for replica, job_id in result.all()]


async def mark_lost(session: AsyncSession, replica_id: int, attempt: int) -> bool:
    """Invalidate only the running attempt observed by housekeeping."""
    changed = await session.scalar(
        update(Replica)
        .where(
            Replica.id == replica_id,
            Replica.attempt == attempt,
            Replica.status == ReplicaStatus.RUNNING,
        )
        .values(status=ReplicaStatus.LOST, token_hash=None, node_name=None)
        .returning(Replica.id)
    )
    return changed is not None


async def fail_unplaced(
    session: AsyncSession, replica_id: int, attempt: int
) -> int | None:
    """
    Fails a RUNNING attempt whose Pod never got a node, so its task can be voted
    on, and returns its task id. A Pod placed since housekeeping looked is left
    alone (None). So is a check copy: it waits for a checker node, and the task
    shows "awaiting score" meanwhile, because a model is never marked scored
    without a check.
    """
    return await session.scalar(
        update(Replica)
        .where(
            Replica.id == replica_id,
            Replica.attempt == attempt,
            Replica.status == ReplicaStatus.RUNNING,
            Replica.node_name.is_(None),
            Replica.check_of.is_(None),
        )
        .values(
            status=ReplicaStatus.FAILED, token_hash=None, ended_at=datetime.now(UTC)
        )
        .returning(Replica.task_id)
    )


async def lock_stale_replica_job(
    session: AsyncSession, replica_id: int, attempt: int
) -> bool:
    """Guard job deletion against scheduling; finished jobs use Kubernetes TTL."""
    stale_id = await session.scalar(
        select(Replica.id)
        .where(
            Replica.id == replica_id,
            or_(
                Replica.attempt > attempt,
                (Replica.attempt == attempt)
                & Replica.status.in_(
                    [ReplicaStatus.CANCELLED, ReplicaStatus.LOST, ReplicaStatus.EVICTED]
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )
    # A SCHEDULED replica may be in the middle of creating this very Job.
    return stale_id is not None


async def create_replicas(
    session: AsyncSession, tasks: list[Task], replicas_per_task: int
) -> list[Replica]:
    """
    Creates the replicas for new tasks, all SCHEDULED at attempt 1.

    args:
        session: The database session for this request.
        tasks: The tasks to create replicas for.
        replicas_per_task: How many replicas each task gets, for voting.
    returns:
        list[Replica]: The new replicas, with ids assigned.
    """
    replicas = [
        Replica(task_id=task.id, attempt=1, status=ReplicaStatus.SCHEDULED)
        for task in tasks
        for _ in range(replicas_per_task)
    ]
    session.add_all(replicas)
    await session.flush()
    return replicas


async def fetch_scheduled_replicas(
    session: AsyncSession, limit: int
) -> list[tuple[Replica, Task, JobType]]:
    """
    Fetches SCHEDULED replicas of active jobs and tasks for the scheduler to launch.

    Rows already locked by another scheduler are skipped, so two coordinators
    never launch the same replica.

    A task's copies launch one after another: a copy waits while an earlier copy
    of its task is still unplaced (SCHEDULED, or RUNNING with no node yet).
    Once that copy has a node, sibling_nodes() can exclude it for this one.
    Kubernetes' pod anti-affinity alone only keeps copies apart while they run,
    so a copy started after a sibling finished could land on the same PC.

    args:
        session: The scheduler's database session.
        limit: The most replicas to return.
    returns:
        list[tuple[Replica, Task, JobType]]: Each replica with its task and job type,
            oldest first.
    """
    earlier = aliased(Replica)
    unplaced_earlier_sibling = exists().where(
        earlier.task_id == Replica.task_id,
        earlier.id < Replica.id,
        earlier.node_name.is_(None),
        earlier.status.in_([ReplicaStatus.SCHEDULED, ReplicaStatus.RUNNING]),
    )
    stmt = (
        select(Replica, Task, JobType)
        .join(Replica.task)
        .join(Task.job)
        .join(Job.job_type_obj)
        .where(
            Replica.status == ReplicaStatus.SCHEDULED,
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            Task.status.in_(
                [
                    TaskStatus.PENDING,
                    TaskStatus.RUNNING,
                    TaskStatus.DISPUTED,
                    TaskStatus.SCORING,
                ]
            ),
            ~unplaced_earlier_sibling,
        )
        .order_by(Replica.id)
        .limit(limit)
        # of Replica to only lock the Replica table, not the Task or Job tables
        .with_for_update(skip_locked=True, of=Replica)
    )
    result = await session.execute(stmt)
    return [(replica, task, job_type) for replica, task, job_type in result.all()]


async def sibling_nodes(session: AsyncSession, replica: Replica) -> list[str]:
    """
    The nodes the other copies of this replica's task have used, finished or
    not, so a new copy can be kept off all of them and the vote stays between
    different PCs.
    """
    rows = await session.scalars(
        select(Replica.node_name)
        .where(
            Replica.task_id == replica.task_id,
            Replica.id != replica.id,
            Replica.node_name.is_not(None),
        )
        .distinct()
    )
    return sorted(rows.all())


async def get_replica_with_task(
    session: AsyncSession, replica_id: int, lock: bool
) -> tuple[Replica, Task, JobType] | None:
    """
    Fetches a replica together with its task and job type, in one query.

    args:
        session: The database session for this request.
        replica_id: The id of the replica.
        lock: Lock the replica row (FOR UPDATE) until the transaction ends, for
            endpoints that change it.
    returns:
        tuple[Replica, Task, JobType] | None: The replica, its task and job type, or
            None if the replica does not exist.
    """
    stmt = (
        select(Replica, Task, JobType)
        .join(Replica.task)
        .join(Task.job)
        .join(Job.job_type_obj)
        .where(Replica.id == replica_id)
    )
    if lock:
        stmt = stmt.with_for_update(of=(Replica, Task))
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    replica, task, job_type = row
    return replica, task, job_type


async def evict_replica(session: AsyncSession, replica: Replica) -> None:
    """
    Stops a replica whose Pod reported an eviction.

    The replica becomes EVICTED, so recover_replicas reruns it on the next
    scheduler tick. An owner eviction is not the copy's fault, so it doesn't
    count towards RETRY_ATTEMPTS; only MAX_ATTEMPTS (and the job type's
    max_age_hours, in housekeeping) end a copy that keeps getting evicted.
    Either way its token stops working, so the dying Pod cannot report anything
    else.

    args:
        session: The database session for this request.
        replica: The RUNNING replica to stop, locked by the caller.
    """
    replica.token_hash = None
    replica.node_name = None
    if replica.attempt >= MAX_ATTEMPTS:
        replica.status = ReplicaStatus.FAILED
        replica.ended_at = datetime.now(UTC)
    else:
        replica.status = ReplicaStatus.EVICTED
    await session.flush()


def out_of_retries(replica: Replica) -> bool:
    """Whether a copy has used up its failures or, with evictions, its attempts."""
    return replica.failures >= RETRY_ATTEMPTS or replica.attempt >= MAX_ATTEMPTS


async def recover_replicas(session: AsyncSession) -> None:
    """
    Puts EVICTED and LOST replicas of running tasks back in the queue.

    Each one keeps its id and moves to the next attempt, so the new Pod's
    manifest points at the same checkpoint and the work resumes. A LOST copy
    (its node or Pod went away) counts as a failure; an EVICTED one (the PC's
    owner came back) doesn't. A replica out of retries becomes FAILED instead.
    Called at the start of every scheduler tick.

    args:
        session: The scheduler's database session.
    """
    stmt = (
        select(Replica)
        .join(Task, Replica.task_id == Task.id)
        .where(
            or_(
                Replica.status == ReplicaStatus.EVICTED,
                Replica.status == ReplicaStatus.LOST,
            ),
            Task.status.in_(
                [TaskStatus.RUNNING, TaskStatus.DISPUTED, TaskStatus.SCORING]
            ),
        )
        .with_for_update(of=Replica, skip_locked=True)
    )
    replicas = (await session.execute(stmt)).scalars()
    affected_task_ids = set()
    for replica in replicas:
        if replica.status == ReplicaStatus.LOST:
            replica.failures += 1
        if out_of_retries(replica):
            replica.status = ReplicaStatus.FAILED
            replica.ended_at = datetime.now(UTC)
            affected_task_ids.add(replica.task_id)
            continue
        replica.attempt += 1
        replica.status = ReplicaStatus.SCHEDULED
        replica.started_at = None
        replica.ended_at = None
    await session.flush()
    for task_id in affected_task_ids:
        task = await session.get(Task, task_id)
        if task is not None:
            await set_voting(session, task)


async def requeue_without_gpu(session: AsyncSession, replica: Replica) -> bool:
    """
    Handles a GPU copy whose runner found no working GPU on its node (exit code
    69): counts a failure, keeps the copy off that node from now on and, unless
    it is out of retries, makes it EVICTED so recover_replicas runs it again on
    another node. Returns False, changing nothing but the counters, when it is
    out of retries; the caller then records the result as usual and the task
    fails with the runner's log.

    args:
        session: The database session for this request.
        replica: The RUNNING replica, locked by the caller.
    """
    replica.failures += 1
    if replica.node_name and replica.node_name not in replica.bad_nodes:
        replica.bad_nodes = [*replica.bad_nodes, replica.node_name]
    if out_of_retries(replica):
        await session.flush()
        return False
    replica.status = ReplicaStatus.EVICTED
    replica.token_hash = None
    replica.node_name = None
    await session.flush()
    return True


async def record_checkpoint(
    session: AsyncSession,
    replica: Replica,
    attempt: int,
    sha256: str,
    size: int,
    step: int | None,
) -> None:
    """
    Records the newest checkpoint archive a running copy published, for "last
    saved" on the job page and the lost-work measurement. The next attempt's
    manifest tries this archive first.

    args:
        session: The database session for this request.
        replica: The RUNNING replica, locked by the caller.
        attempt: The attempt whose key the archive was uploaded to.
        sha256: The archive's SHA-256, hex.
        size: The archive's size in bytes.
        step: The training step the script recorded with it, if any.
    """
    replica.checkpoint_attempt = attempt
    replica.checkpoint_sha256 = sha256
    replica.checkpoint_bytes = size
    replica.checkpoint_step = step
    replica.checkpoint_at = datetime.now(UTC)
    await session.flush()


async def record_check(
    session: AsyncSession,
    replica: Replica,
    task: Task,
    outcome: dict,
    result_hash: str,
) -> None:
    """
    Records a check copy's outcome and marks it DONE (exit code 0): scored, or
    not scored with the reason. Call set_voting afterwards, so the verifier
    finishes the task (services/verifier/model_check.py).

    args:
        session: The database session for this request.
        replica: The RUNNING check copy, locked by the caller.
        task: Its task.
        outcome: The check's fields (models.CheckIn as a dict).
        result_hash: SHA-256 of the outcome as canonical JSON.
    """
    replica.check_status = outcome["status"]
    replica.check_score = outcome.get("score")
    replica.check_samples = outcome.get("samples")
    replica.check_model_sha256 = outcome.get("model_sha256")
    replica.check_eval_sha256 = outcome.get("eval_sha256")
    replica.check_evaluator = outcome["evaluator"]
    replica.check_reason = outcome.get("reason")
    await mark_replica_done(session, replica, task, result_hash, 0)


async def schedule_replica(session: AsyncSession, replica: Replica, token: str) -> None:
    """
    Records that a replica's Kubernetes Job was created: RUNNING with a new token.

    Only the token's hash is stored. The Pod gets the token itself as
    REPLICA_TOKEN and sends it on every call.

    args:
        session: The scheduler's database session.
        replica: The replica that was just launched.
        token: The plain token given to the Pod.
    """
    replica.status = ReplicaStatus.RUNNING
    replica.token_hash = hashlib.sha256(token.encode()).hexdigest()
    replica.progress = 0
    replica.started_at = datetime.now(UTC)
    await session.flush()


async def mark_replica_done(
    session: AsyncSession,
    replica: Replica,
    task: Task,
    result_hash: str,
    exit_code: int,
) -> None:
    """
    Records a replica's result: DONE, progress 100, its result hash and exit code.

    Call set_voting afterwards to move the task on once all replicas finished.

    args:
        session: The database session for this request.
        replica: The RUNNING replica that finished, locked by the caller.
        task: The replica's task.
        result_hash: SHA-256 hex of the replica's result.json, compared when voting.
        exit_code: The worker's exit code.
    """
    if task is None:
        raise ValueError(f"task {replica.task_id} not found")
    replica.status = ReplicaStatus.DONE
    replica.progress = 100
    replica.result_hash = result_hash
    replica.exit_code = exit_code
    replica.ended_at = datetime.now(UTC)
    await session.flush()


async def record_placement(
    session: AsyncSession,
    replica_id: int,
    attempt: int,
    node_name: str,
    started_at: datetime | None,
) -> None:
    """
    Records the node and start time of a RUNNING replica's Pod, once.

    Only the current attempt's first placement is recorded, so an older
    attempt's Pod never writes its node onto a requeued replica.

    args:
        session: The database session for this request.
        replica_id: The id of the replica whose Pod was placed.
        attempt: The attempt the Pod belongs to.
        node_name: The node the Pod was placed on.
        started_at: When the Pod started, or None if not known yet.
    """
    values: dict = {"node_name": node_name}
    if started_at is not None:
        values["started_at"] = started_at
    await session.execute(
        update(Replica)
        .where(
            Replica.id == replica_id,
            Replica.attempt == attempt,
            Replica.status == ReplicaStatus.RUNNING,
            Replica.node_name.is_(None),
        )
        .values(**values)
    )


async def handle_replica_completed(
    session: AsyncSession,
    replica_id: int,
    attempt: int,
    status: ReplicaStatus,
    exit_code: int | None = None,
) -> tuple[int, str | None] | None:
    """
    Handles a replica that has completed its execution.

    args:
        session: The database session for this request.
        replica_id: The id of the completed replica.
        attempt: The attempt number of the completed replica.
        status: The status of the completed replica.
        exit_code: The exit code of the completed replica, if known.
    returns:
        tuple[int, str | None] | None: The task id and node name of the completed replica, or None if the replica was not found or already completed.
    """
    replica = await session.scalar(
        select(Replica)
        .where(
            Replica.id == replica_id,
            Replica.attempt == attempt,
            Replica.status == ReplicaStatus.RUNNING,
        )
        .with_for_update()
    )
    if replica is None:
        return None
    node_name = replica.node_name  # before it is cleared, for the event
    replica.status = status
    replica.token_hash = None
    if status == ReplicaStatus.FAILED:
        replica.ended_at = datetime.now(UTC)
        replica.exit_code = exit_code
    else:
        replica.node_name = None  # requeued; the next attempt records its own node
    await session.flush()
    return replica.task_id, node_name
