"""
Task Pods stuck Terminating (issue #195) or on lost nodes (issue #213): a Pod deleted while its node was
still pulling the image holds its memory until the pull returns, so
housekeeping removes it by force once nothing of it can be running.
"""

from datetime import UTC, datetime, timedelta

from kubernetes.client import (
    V1Container,
    V1ContainerState,
    V1ContainerStateRunning,
    V1ContainerStateWaiting,
    V1ContainerStatus,
    V1Node,
    V1NodeCondition,
    V1NodeStatus,
    V1ObjectMeta,
    V1Pod,
    V1PodSpec,
    V1PodStatus,
)

from services.coordinator.loops.housekeeping import (
    NODE_LOST_AFTER,
    STUCK_TERMINATING,
    lost_node_pods,
    node_ready,
    not_ready_since,
    stuck_terminating_pods,
)

NOW = datetime(2026, 11, 20, 8, 50, tzinfo=UTC)


def pod(
    name="replica-1-a1-x",
    deleted_ago: timedelta | None = timedelta(minutes=10),
    node: str | None = "desktop-nvidia",
    running: bool = False,
) -> V1Pod:
    state = (
        V1ContainerState(running=V1ContainerStateRunning())
        if running
        else V1ContainerState(
            waiting=V1ContainerStateWaiting(reason="ContainerCreating")
        )
    )
    return V1Pod(
        metadata=V1ObjectMeta(
            name=name,
            uid="u",
            deletion_timestamp=NOW - deleted_ago if deleted_ago else None,
        ),
        spec=V1PodSpec(node_name=node, containers=[V1Container(name="worker")]),
        status=V1PodStatus(
            container_statuses=[
                V1ContainerStatus(
                    name="worker",
                    image="i",
                    image_id="",
                    ready=False,
                    restart_count=0,
                    state=state,
                )
            ]
        ),
    )


def names(pods):
    return [p.metadata.name for p in pods]


def test_pod_stuck_mid_pull_is_removed():
    assert names(
        stuck_terminating_pods([pod()], {"desktop-nvidia"}, NOW, STUCK_TERMINATING)
    ) == ["replica-1-a1-x"]


def test_pod_still_within_its_time_is_left():
    fresh = pod(deleted_ago=timedelta(seconds=30))
    assert (
        stuck_terminating_pods([fresh], {"desktop-nvidia"}, NOW, STUCK_TERMINATING)
        == []
    )


def test_pod_not_being_deleted_is_left():
    live = pod(deleted_ago=None)
    assert (
        stuck_terminating_pods([live], {"desktop-nvidia"}, NOW, STUCK_TERMINATING) == []
    )


def test_pod_on_a_node_that_isnt_ready_is_left():
    # the usual lost-node handling covers it; its kubelet can't clean up now
    assert stuck_terminating_pods([pod()], set(), NOW, STUCK_TERMINATING) == []
    assert stuck_terminating_pods([pod(node=None)], set(), NOW, STUCK_TERMINATING) == []


def test_pod_whose_container_runs_is_left():
    # removing it by force could leave the copy running on the PC
    busy = pod(running=True)
    assert (
        stuck_terminating_pods([busy], {"desktop-nvidia"}, NOW, STUCK_TERMINATING) == []
    )


def test_pod_without_container_statuses_is_removed():
    bare = pod()
    bare.status.container_statuses = None
    assert names(
        stuck_terminating_pods([bare], {"desktop-nvidia"}, NOW, STUCK_TERMINATING)
    ) == ["replica-1-a1-x"]


def test_node_ready():
    def node(status):
        return V1Node(
            status=V1NodeStatus(
                conditions=[V1NodeCondition(type="Ready", status=status)]
            )
        )

    assert node_ready(node("True"))
    assert not node_ready(node("Unknown"))
    assert not node_ready(V1Node())


# ---------- copies on lost nodes (issue #213) ----------


def worker_node(name: str, ready: str, since_min: float) -> V1Node:
    return V1Node(
        metadata=V1ObjectMeta(name=name),
        status=V1NodeStatus(
            conditions=[
                V1NodeCondition(
                    type="Ready",
                    status=ready,
                    last_transition_time=NOW - timedelta(minutes=since_min),
                )
            ]
        ),
    )


def task_pod(name: str, node: str | None, phase: str = "Pending") -> V1Pod:
    return V1Pod(
        metadata=V1ObjectMeta(name=name, uid="u"),
        spec=V1PodSpec(node_name=node, containers=[V1Container(name="worker")]),
        status=V1PodStatus(phase=phase),
    )


def test_pods_on_a_long_lost_node_are_given_up():
    nodes = [
        worker_node("gone", "Unknown", 20),
        worker_node("blip", "Unknown", 1),
        worker_node("fine", "True", 60),
    ]
    pods = [
        task_pod("on-gone", "gone"),
        task_pod("on-gone-running", "gone", "Running"),
        task_pod("on-gone-finished", "gone", "Succeeded"),
        task_pod("on-blip", "blip"),
        task_pod("on-fine", "fine"),
        task_pod("unplaced", None),
    ]
    lost = lost_node_pods(pods, nodes, NOW, NODE_LOST_AFTER)
    assert names(lost) == ["on-gone", "on-gone-running"]


def test_not_ready_since():
    assert not_ready_since(worker_node("n", "True", 5)) is None
    assert not_ready_since(worker_node("n", "False", 5)) == NOW - timedelta(minutes=5)
    # no Ready condition at all counts as long gone
    assert not_ready_since(V1Node(metadata=V1ObjectMeta(name="n"))) is not None
