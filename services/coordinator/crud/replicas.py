import hashlib
from datetime import UTC, datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import RETRY_ATTEMPTS
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

    args:
        session: The scheduler's database session.
        limit: The most replicas to return.
    returns:
        list[tuple[Replica, Task, JobType]]: Each replica with its task and job type,
            oldest first.
    """
    stmt = (
        select(Replica, Task, JobType)
        .join(Replica.task)
        .join(Task.job)
        .join(Job.job_type_obj)
        .where(
            Replica.status == ReplicaStatus.SCHEDULED,
            Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            Task.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .order_by(Replica.id)
        .limit(limit)
        # of Replica to only lock the Replica table, not the Task or Job tables
        .with_for_update(skip_locked=True, of=Replica)
    )
    result = await session.execute(stmt)
    return [(replica, task, job_type) for replica, task, job_type in result.all()]


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
    scheduler tick, or FAILED once it has used RETRY_ATTEMPTS attempts. Either
    way its token stops working, so the dying Pod cannot report anything else.

    args:
        session: The database session for this request.
        replica: The RUNNING replica to stop, locked by the caller.
    """
    replica.token_hash = None
    replica.node_name = None
    if replica.attempt >= RETRY_ATTEMPTS:
        replica.status = ReplicaStatus.FAILED
        replica.ended_at = datetime.now(UTC)
    else:
        replica.status = ReplicaStatus.EVICTED
    await session.flush()


async def recover_replicas(session: AsyncSession) -> None:
    """
    Puts EVICTED and LOST replicas of running tasks back in the queue.

    Each one keeps its id and moves to the next attempt, so the new Pod's
    manifest points at the same checkpoint and the work resumes. A replica that
    has used RETRY_ATTEMPTS attempts becomes FAILED instead. Called at the start
    of every scheduler tick.

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
            Task.status.in_([TaskStatus.RUNNING, TaskStatus.DISPUTED]),
        )
        .with_for_update(of=Replica, skip_locked=True)
    )
    replicas = (await session.execute(stmt)).scalars()
    affected_task_ids = set()
    for replica in replicas:
        if replica.attempt >= RETRY_ATTEMPTS:
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
    if replica.attempt != attempt:
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
