"""
Image pulls (issue #189): the pod watcher tells a copy whose node is still
downloading its image apart from one that is running, and records it on the
replica for the job page.

The database tests need a scratch Postgres, as in test_recovery.py:
    IDLECLOUD_TEST_DATABASE_URL=postgresql://postgres@localhost:55432/t pytest services/coordinator/tests
"""

import os

import pytest
from kubernetes.client import (
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStateTerminated,
    V1ContainerStateWaiting,
    V1ContainerStatus,
)

from services.coordinator.crud import (
    ImagePull,
    record_image_pull,
    schedule_replica,
)
from services.coordinator.loops.pod_watcher import image_pull, image_refs
from shared.db.models import ReplicaStatus

GPU_IMAGE = "ghcr.io/sitprojects/idlecloud-pytorch-gpu:main"
DB_URL = os.environ.get("IDLECLOUD_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(
    not DB_URL, reason="set IDLECLOUD_TEST_DATABASE_URL to a scratch Postgres"
)


def worker(state: V1ContainerState | None) -> V1ContainerStatus:
    return V1ContainerStatus(
        name="worker",
        image=GPU_IMAGE,
        image_id="",
        ready=False,
        restart_count=0,
        state=state,
    )


def waiting(reason: str, message: str | None = None) -> V1ContainerStatus:
    return worker(
        V1ContainerState(
            waiting=V1ContainerStateWaiting(reason=reason, message=message)
        )
    )


def never_called():
    raise AssertionError("the node's images were read when they didn't matter")


# ---------- without a database ----------


def test_image_refs_full_names():
    assert image_refs(GPU_IMAGE) == {GPU_IMAGE}
    assert image_refs("python:3.12") == {
        "python:3.12",
        "docker.io/library/python:3.12",
    }
    assert image_refs("someone/tool") == {
        "someone/tool",
        "docker.io/someone/tool:latest",
    }
    assert image_refs("localhost:5000/tool:1") == {"localhost:5000/tool:1"}


def test_creating_without_the_image_on_the_node_is_pulling():
    assert image_pull(waiting("ContainerCreating"), lambda: False) == ImagePull(
        "pulling"
    )


def test_creating_with_the_image_on_the_node_is_not_pulling():
    assert image_pull(waiting("ContainerCreating"), lambda: True) == ImagePull("done")


def test_unreadable_node_says_nothing():
    assert image_pull(waiting("ContainerCreating"), lambda: None) is None


def test_failed_pull_keeps_the_kubelets_message():
    assert image_pull(
        waiting("ImagePullBackOff", "Back-off pulling image"), never_called
    ) == ImagePull("failed", "Back-off pulling image")
    assert image_pull(waiting("ErrImagePull"), never_called) == ImagePull(
        "failed", "ErrImagePull"
    )


def test_started_or_finished_container_is_done():
    running = worker(V1ContainerState(running=V1ContainerStateRunning()))
    ended = worker(V1ContainerState(terminated=V1ContainerStateTerminated(exit_code=0)))
    assert image_pull(running, never_called) == ImagePull("done")
    assert image_pull(ended, never_called) == ImagePull("done")
    # waits that only come after a pull
    assert image_pull(waiting("CrashLoopBackOff"), never_called) == ImagePull("done")


def test_no_container_state_yet_says_nothing():
    assert image_pull(None, never_called) is None
    assert image_pull(worker(None), never_called) is None


# ---------- with a database ----------


@pytest.fixture(scope="module")
def migrated():
    from shared.migrate import run_migrations

    run_migrations(DB_URL)


@needs_db
def test_pull_is_recorded_kept_and_cleared(migrated):
    from services.coordinator.tests.test_recovery import make_replica, run

    async def test(session):
        _, _, replica = await make_replica(session)
        await record_image_pull(session, replica.id, 1, ImagePull("pulling"))
        await session.refresh(replica)
        started = replica.pulling_since
        assert started is not None and replica.pull_error is None

        # a failed pull keeps when it started; the kubelet retries it
        await record_image_pull(session, replica.id, 1, ImagePull("failed", "no"))
        await session.refresh(replica)
        assert replica.pulling_since == started and replica.pull_error == "no"

        await record_image_pull(session, replica.id, 1, ImagePull("done"))
        await session.refresh(replica)
        assert replica.pulling_since is None and replica.pull_error is None

    run(test)


@needs_db
def test_older_attempts_pods_are_ignored(migrated):
    from services.coordinator.tests.test_recovery import make_replica, run

    async def test(session):
        _, _, replica = await make_replica(session, attempt=2)
        await record_image_pull(session, replica.id, 1, ImagePull("pulling"))
        await session.refresh(replica)
        assert replica.pulling_since is None

    run(test)


@needs_db
def test_a_new_launch_clears_the_last_pull(migrated):
    from services.coordinator.tests.test_recovery import make_replica, run

    async def test(session):
        _, _, replica = await make_replica(session)
        await record_image_pull(session, replica.id, 1, ImagePull("failed", "no"))
        await session.refresh(replica)
        replica.status = ReplicaStatus.SCHEDULED
        replica.attempt = 2
        await schedule_replica(session, replica, "token")
        assert replica.pulling_since is None and replica.pull_error is None

    run(test)
