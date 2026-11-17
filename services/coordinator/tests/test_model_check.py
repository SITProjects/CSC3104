"""
Platform scoring of trained models (migration 017): a successful GPU training
copy with an evaluation file gets a check copy before its task is VERIFIED,
checker failures never fail the owner's task, and check copies run only on
checker nodes.

The database tests need IDLECLOUD_TEST_DATABASE_URL, like test_recovery.py.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from services.coordinator.crud import (
    expire_old_jobs,
    fail_unplaced,
    record_check,
    set_voting,
)
from services.coordinator.k8s import build_check_job_manifest
from services.coordinator.models import CheckIn
from services.coordinator.tests.test_recovery import (
    make_replica,
    migrated,  # noqa: F401 - a fixture
    needs_db,
    run,
)
from services.verifier.controller import process_vote
from services.verifier.model_check import CHECK_ATTEMPTS
from shared.db.models import (
    Event,
    JobType,
    Replica,
    ReplicaStatus,
    Task,
    TaskStatus,
)

SCORED = {"status": "scored", "score": 0.93, "samples": 100, "evaluator": "t/1"}
SCORED["model_sha256"] = "a" * 64
SCORED["eval_sha256"] = "b" * 64


# ---------- without a database ----------


def test_check_pod_runs_only_on_checker_nodes():
    job_type = JobType(
        name="pytorch-gpu", image="gpu:main", check_image="check:main", gpu=True
    )
    task = Task(id=7, job_id="00000000-0000-0000-0000-000000000001", task_index=0)
    replica = Replica(id=9, attempt=1, check_of=8)
    manifest = build_check_job_manifest(
        replica, task, job_type, "tok", "http://c", avoid_nodes=["pc-1"]
    )
    spec = manifest["spec"]["template"]["spec"]
    assert spec["nodeSelector"] == {"idlecloud/checker": "true"}
    assert spec["activeDeadlineSeconds"] == 600
    assert "runtimeClassName" not in spec and "affinity" not in spec
    container = spec["containers"][0]
    assert container["image"] == "check:main"
    assert container["resources"]["limits"] == {"cpu": "1", "memory": "1024Mi"}
    assert manifest["metadata"]["labels"]["idlecloud/check"] == "true"


def test_check_outcome_must_be_complete():
    CheckIn(**SCORED)
    CheckIn(status="not_scored", evaluator="t/1", reason="no_model: missing")
    with pytest.raises(ValidationError):
        CheckIn(status="scored", evaluator="t/1", score=0.5)  # no samples or digest
    with pytest.raises(ValidationError):
        CheckIn(status="not_scored", evaluator="t/1")  # no reason
    with pytest.raises(ValidationError):
        CheckIn(**{**SCORED, "score": 1.5})


# ---------- with a database ----------


async def trained_task(session, eval_file=True, exit_code=0):
    """A pytorch-gpu task whose training copy has finished, now VOTING."""
    job, task, trained = await make_replica(session)
    task.input = {"command": ["python", "t.py"]}
    if eval_file:
        task.input["eval_file"] = "eval/eval.npz"
    trained.status = ReplicaStatus.DONE
    trained.exit_code = exit_code
    trained.result_hash = "c" * 64
    task.status = TaskStatus.VOTING
    await session.flush()
    return job, task, trained


async def checks_of(session, task):
    return (
        await session.scalars(
            select(Replica)
            .where(Replica.task_id == task.id, Replica.check_of.is_not(None))
            .order_by(Replica.id)
        )
    ).all()


async def event_types(session, task):
    return [
        e.type
        for e in (
            await session.scalars(
                select(Event).where(Event.task_id == task.id).order_by(Event.id)
            )
        ).all()
    ]


@needs_db
def test_trained_model_gets_a_check_before_it_is_accepted(migrated):  # noqa: F811
    async def test(session):
        _, task, trained = await trained_task(session)
        await process_vote(session, task)
        assert task.status == TaskStatus.SCORING
        [check] = await checks_of(session, task)
        assert check.check_of == trained.id
        assert check.status == ReplicaStatus.SCHEDULED

        # the checker reports a score
        check.status = ReplicaStatus.RUNNING
        await record_check(session, check, task, SCORED, "d" * 64)
        await set_voting(session, task)
        assert task.status == TaskStatus.VOTING
        await process_vote(session, task)
        assert task.status == TaskStatus.VERIFIED
        assert task.final_hash == trained.result_hash  # the training's, not the check's
        await session.refresh(check)
        assert check.check_status == "scored" and check.check_score == 0.93
        assert "task_scored" in await event_types(session, task)

    run(test)


@needs_db
def test_not_scored_model_still_completes(migrated):  # noqa: F811
    async def test(session):
        _, task, _ = await trained_task(session)
        await process_vote(session, task)
        [check] = await checks_of(session, task)
        check.status = ReplicaStatus.RUNNING
        outcome = {"status": "not_scored", "evaluator": "t/1", "reason": "no_model: x"}
        await record_check(session, check, task, outcome, "d" * 64)
        await set_voting(session, task)
        await process_vote(session, task)
        assert task.status == TaskStatus.VERIFIED
        assert "task_not_scored" in await event_types(session, task)

    run(test)


@needs_db
def test_checker_failures_are_retried_then_never_fail_the_task(migrated):  # noqa: F811
    async def test(session):
        _, task, _ = await trained_task(session)
        for number in range(1, CHECK_ATTEMPTS + 1):
            await process_vote(session, task)
            assert task.status == TaskStatus.SCORING
            checks = await checks_of(session, task)
            assert len(checks) == number
            checks[-1].status = ReplicaStatus.FAILED  # crashed or timed out
            await session.flush()
            await set_voting(session, task)
        await process_vote(session, task)
        assert task.status == TaskStatus.VERIFIED
        assert len(await checks_of(session, task)) == CHECK_ATTEMPTS
        assert (await event_types(session, task))[-1] == "task_not_scored"

    run(test)


@needs_db
def test_no_evaluation_file_means_no_check(migrated):  # noqa: F811
    async def test(session):
        _, task, _ = await trained_task(session, eval_file=False)
        await process_vote(session, task)
        assert task.status == TaskStatus.VERIFIED
        assert await checks_of(session, task) == []

    run(test)


@needs_db
def test_failed_training_is_not_checked(migrated):  # noqa: F811
    async def test(session):
        _, task, _ = await trained_task(session, exit_code=1)
        await process_vote(session, task)
        assert task.status == TaskStatus.FAILED
        assert await checks_of(session, task) == []

    run(test)


@needs_db
def test_waiting_for_a_checker_never_expires_or_fails(migrated):  # noqa: F811
    async def test(session):
        now = datetime.now(UTC)
        job, task, _ = await make_replica(session, created_at=now - timedelta(days=3))
        task.input = {"eval_file": "eval/eval.npz"}
        task.status = TaskStatus.SCORING
        job.task_count = 1
        _, _, trained = await make_replica(session)  # any finished copy will do
        check = Replica(
            task_id=task.id,
            attempt=1,
            status=ReplicaStatus.RUNNING,
            check_of=trained.id,
        )
        session.add(check)
        await session.flush()
        # training is over: only the platform's checker is missing
        for r in await session.scalars(
            select(Replica).where(Replica.task_id == task.id, Replica.id != check.id)
        ):
            r.status = ReplicaStatus.DONE
        await session.flush()
        assert job.id not in await expire_old_jobs(session, now)
        assert await fail_unplaced(session, check.id, 1) is None
        await session.refresh(check)
        assert check.status == ReplicaStatus.RUNNING

    run(test)
