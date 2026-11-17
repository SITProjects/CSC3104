"""Stops this node's task containers without the API server.

When the owner comes back, the agent taints the node and Kubernetes evicts the
task Pods. If the API server can't be reached (the WireGuard link or the hub is
down), the taint never lands and the job would keep the PC busy. Then the agent
stops the task containers itself through K3s's container runtime: each gets
SIGTERM, so the runner saves its checkpoint and exits with 143, and the
coordinator counts the copy as evicted, not failed, once it sees the Pod.
"""

import logging
import shlex
import subprocess

import config

log = logging.getLogger("node-agent.localstop")
# Every task Pod runs in this namespace (core/settings.py JOB_NAMESPACE)
JOB_NAMESPACE = "idlecloud-jobs"


def _crictl(*args: str, timeout: float) -> str:
    result = subprocess.run(
        [*shlex.split(config.CRICTL), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def running_task_containers() -> list[str]:
    """The ids of the task containers running on this node."""
    out = _crictl(
        "ps",
        "--quiet",
        "--state",
        "Running",
        "--label",
        f"io.kubernetes.pod.namespace={JOB_NAMESPACE}",
        timeout=15,
    )
    return out.split()


def stop_task_containers() -> int:
    """
    Stops every running task container, giving each the runner's grace period
    after SIGTERM. Returns how many were stopped. Never raises: a failure is
    logged, and the next owner return tries again.
    """
    try:
        ids = running_task_containers()
        if ids:
            _crictl(
                "stop",
                "--timeout",
                str(config.LOCAL_STOP_GRACE_S),
                *ids,
                timeout=config.LOCAL_STOP_GRACE_S + 15,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", None) or exc
        log.error("could not stop the task containers locally: %s", detail)
        return 0
    if ids:
        log.warning("stopped %d task container(s) locally", len(ids))
    return len(ids)
