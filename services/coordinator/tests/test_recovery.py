"""
Life-cycle tests for long jobs (migration 016): evictions don't use up a
copy's retries, lost nodes and missing GPUs do, checkpoints are recorded, and
jobs older than their type's max_age_hours fail.

The database tests need a scratch Postgres 16 with nothing else in it:
    IDLECLOUD_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/t pytest services/coordinator/tests
They are skipped without it. Every migration is applied first.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.settings import MAX_ATTEMPTS, RETRY_ATTEMPTS
from services.coordinator.app import restore_order
from services.coordinator.crud import (
    ensure_node,
    evict_replica,
    expire_old_jobs,
    record_checkpoint,
    recover_replicas,
    requeue_without_gpu,
)
from services.coordinator.loops.pod_watcher import decide
from shared.db.models import (
    Event,
    Job,
    JobStatus,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
    User,
)

DB_URL = os.environ.get("IDLECLOUD_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DB_URL, reason="set IDLECLOUD_TEST_DATABASE_URL to a scratch Postgres"
)


# ---------- without a database ----------


def test_restore_order_tries_the_reported_checkpoint_first():
    replica = Replica(attempt=5, checkpoint_attempt=2)
    assert restore_order(replica) == [2, 4, 3, 1]


def test_restore_order_newest_first_without_a_report():
    assert restore_order(Replica(attempt=4, checkpoint_attempt=None)) == [3, 2, 1]
    assert restore_order(Replica(attempt=1, checkpoint_attempt=None)) == []


def test_restore_order_ignores_the_current_attempts_own_report():
    # a report from the attempt now running isn't an earlier archive
    assert restore_order(Replica(attempt=3, checkpoint_attempt=3)) == [2, 1]


def test_pod_stopped_on_its_node_counts_as_an_eviction():
    # the node agent's local stop: SIGTERM, the runner exits 143
    assert decide("MODIFIED", "Failed", "Error", None, True, 143) == (
        ReplicaStatus.EVICTED,
        "StoppedOnNode",
    )


def test_other_failed_pods_still_fail():
    assert decide("MODIFIED", "Failed", "Error", None, True, 1) == (
        ReplicaStatus.FAILED,
        "Error",
    )
    assert decide("MODIFIED", "Failed", None, None, True, None) == (
        ReplicaStatus.FAILED,
        "PodFailed",
    )


# ---------- with a database ----------


@pytest.fixture(scope="module")
def migrated():
    from shared.migrate import run_migrations

    run_migrations(DB_URL)


def run(test):
    """Runs test(session) in a transaction that is rolled back afterwards."""

    async def main():
        engine = create_async_engine(
            DB_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
        )
        try:
            async with engine.connect() as conn:
                outer = await conn.begin()
                session = async_sessionmaker(bind=conn, expire_on_commit=False)()
                try:
                    await test(session)
                finally:
                    await session.close()
                    await outer.rollback()
        finally:
            await engine.dispose()

    asyncio.run(main())


async def make_replica(
    session,
    job_type="pytorch-gpu",
    status=ReplicaStatus.RUNNING,
    attempt=1,
    created_at=None,
) -> tuple[Job, Task, Replica]:
    user = User(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    session.add(user)
    await ensure_node(session, "pc-1")
    await session.flush()
    job = Job(
        owner_id=user.id,
        job_type=job_type,
        status=JobStatus.RUNNING,
        task_count=1,
        replicas_per_task=1,
    )
    if created_at is not None:
        job.created_at = created_at
    session.add(job)
    await session.flush()
    task = Task(
        job_id=job.id,
        task_index=0,
        input={},
        status=TaskStatus.RUNNING,
        cpu_request=1000,
        mem_request=512,
    )
    session.add(task)
    await session.flush()
    replica = Replica(
        task_id=task.id,
        attempt=attempt,
        status=status,
        node_name="pc-1",
        token_hash="hash",
    )
    session.add(replica)
    await session.flush()
    return job, task, replica


@needs_db
def test_evictions_never_use_up_retries(migrated):
    async def test(session):
        _, _, replica = await make_replica(session)
        for _ in range(RETRY_ATTEMPTS + 2):
            await evict_replica(session, replica)
            assert replica.status == ReplicaStatus.EVICTED
            await recover_replicas(session)
            await session.refresh(replica)
            assert replica.status == ReplicaStatus.SCHEDULED
            replica.status = ReplicaStatus.RUNNING  # as if launched again
        assert replica.failures == 0
        assert replica.attempt == RETRY_ATTEMPTS + 3

    run(test)


@needs_db
def test_eviction_at_max_attempts_fails_the_copy(migrated):
    async def test(session):
        _, _, replica = await make_replica(session, attempt=MAX_ATTEMPTS)
        await evict_replica(session, replica)
        assert replica.status == ReplicaStatus.FAILED
        assert replica.ended_at is not None

    run(test)


@needs_db
def test_lost_copies_use_up_retries(migrated):
    async def test(session):
        _, task, replica = await make_replica(session)
        for lost in range(1, RETRY_ATTEMPTS + 1):
            replica.status = ReplicaStatus.LOST
            await session.flush()
            await recover_replicas(session)
            await session.refresh(replica)
            assert replica.failures == lost
            if lost < RETRY_ATTEMPTS:
                assert replica.status == ReplicaStatus.SCHEDULED
                assert replica.attempt == lost + 1
        assert replica.status == ReplicaStatus.FAILED
        await session.refresh(task)
        assert task.status != TaskStatus.RUNNING  # voting took over

    run(test)


@needs_db
def test_no_gpu_moves_the_copy_and_remembers_the_node(migrated):
    async def test(session):
        _, _, replica = await make_replica(session)
        assert await requeue_without_gpu(session, replica)
        assert replica.status == ReplicaStatus.EVICTED
        assert replica.bad_nodes == ["pc-1"]
        assert replica.failures == 1
        assert replica.token_hash is None and replica.node_name is None
        await recover_replicas(session)
        await session.refresh(replica)
        assert replica.status == ReplicaStatus.SCHEDULED
        assert replica.attempt == 2

    run(test)


@needs_db
def test_no_gpu_on_the_last_retry_lets_the_result_stand(migrated):
    async def test(session):
        _, _, replica = await make_replica(session)
        replica.failures = RETRY_ATTEMPTS - 1
        assert not await requeue_without_gpu(session, replica)
        # the caller records exit code 69 as the copy's result
        assert replica.status == ReplicaStatus.RUNNING
        assert replica.failures == RETRY_ATTEMPTS

    run(test)


@needs_db
def test_record_checkpoint(migrated):
    async def test(session):
        _, _, replica = await make_replica(session, attempt=3)
        await record_checkpoint(session, replica, 3, "ab" * 32, 1234, 77)
        await session.refresh(replica)
        assert replica.checkpoint_attempt == 3
        assert replica.checkpoint_sha256 == "ab" * 32
        assert replica.checkpoint_bytes == 1234
        assert replica.checkpoint_step == 77
        assert replica.checkpoint_at is not None
        assert restore_order(replica) == [2, 1]  # its own attempt isn't earlier

    run(test)


@needs_db
def test_jobs_older_than_max_age_fail(migrated):
    async def test(session):
        now = datetime.now(UTC)
        old, old_task, old_replica = await make_replica(
            session, created_at=now - timedelta(hours=25)
        )
        young, _, _ = await make_replica(session, created_at=now - timedelta(hours=23))
        # no max_age_hours on CPU job types
        cpu, _, _ = await make_replica(
            session, job_type="python", created_at=now - timedelta(days=30)
        )
        expired = await expire_old_jobs(session, now)
        assert old.id in expired
        assert young.id not in expired and cpu.id not in expired
        for row in (old, old_task, old_replica, young):
            await session.refresh(row)
        assert old.status == JobStatus.FAILED and old.finished_at is not None
        assert old_task.status == TaskStatus.FAILED
        assert old_replica.status == ReplicaStatus.CANCELLED
        assert old_replica.token_hash is None
        assert young.status == JobStatus.RUNNING
        await session.execute(delete(Event))  # nothing to check, keep it clean

    run(test)
