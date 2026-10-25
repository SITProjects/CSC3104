"""Eviction speed trials (member 2). Run on the EC2 control node.

Each trial waits for the worker to be lent (no owner-active taint), starts a test
pod on it, asks you to press a key on the worker, then records when the taint
appears and when the pod stops. Times are this machine's clock. The key press
time comes from the worker's node-agent event log; analyse.py joins the two.

    python run_trials.py --node desktop-cdvabed --trials 20
"""

import argparse
import csv
import sys
import time
import uuid
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.rest import ApiException

TAINT_KEY = "idlecloud/owner-active"
NAMESPACE = "idlecloud-jobs"
POLL_S = 0.05
# Exits as soon as it gets SIGTERM, so we time the eviction, not the pod.
# A plain `sleep` would ignore SIGTERM and always take the full grace period.
POD_COMMAND = "trap 'exit 0' TERM; sleep 3600 & wait"
FIELDS = ["trial", "pod", "prompted", "taint_seen", "pod_deleting", "pod_stopped"]


def tainted(core: client.CoreV1Api, node: str) -> bool:
    taints = core.read_node(node).spec.taints or []
    return any(t.key == TAINT_KEY for t in taints)


def wait_for(check, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout_s:.0f}s waiting for {what}")
        time.sleep(0.5)


def test_pod(name: str, node: str) -> client.V1Pod:
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name=name, labels={"idlecloud/eval": "eviction"}),
        spec=client.V1PodSpec(
            node_selector={"kubernetes.io/hostname": node},
            restart_policy="Never",
            termination_grace_period_seconds=30,
            containers=[
                client.V1Container(
                    name="trial",
                    image="busybox",
                    command=["sh", "-c", POD_COMMAND],
                    resources=client.V1ResourceRequirements(
                        requests={"cpu": "50m", "memory": "16Mi"},
                        limits={"cpu": "50m", "memory": "16Mi"},
                    ),
                )
            ],
        ),
    )


def pod_running(core: client.CoreV1Api, name: str) -> bool:
    pod = core.read_namespaced_pod(name, NAMESPACE)
    statuses = pod.status.container_statuses or []
    return pod.status.phase == "Running" and all(s.ready for s in statuses)


def delete_pod(core: client.CoreV1Api, name: str) -> None:
    try:
        core.delete_namespaced_pod(name, NAMESPACE, grace_period_seconds=0)
    except ApiException as exc:
        if exc.status != 404:
            raise


def measure(core: client.CoreV1Api, node: str, name: str) -> dict:
    """Poll until the pod has stopped. Returns the epoch time of each step."""
    seen = {"taint_seen": None, "pod_deleting": None, "pod_stopped": None}
    deadline = time.monotonic() + 120
    while seen["pod_stopped"] is None:
        if time.monotonic() > deadline:
            raise TimeoutError("pod did not stop within 120s of the prompt")
        now = time.time()
        if seen["taint_seen"] is None and tainted(core, node):
            seen["taint_seen"] = now
        try:
            pod = core.read_namespaced_pod(name, NAMESPACE)
        except ApiException as exc:
            if exc.status != 404:
                raise
            seen["pod_stopped"] = now  # already gone
        else:
            if seen["pod_deleting"] is None and pod.metadata.deletion_timestamp:
                seen["pod_deleting"] = now
            statuses = pod.status.container_statuses or []
            if any(s.state and s.state.terminated for s in statuses):
                seen["pod_stopped"] = now
        time.sleep(POLL_S)
    seen["pod_deleting"] = seen["pod_deleting"] or seen["pod_stopped"]
    return seen


def run_trial(core: client.CoreV1Api, node: str, trial: int, trials: int) -> dict:
    """One trial. Retries itself if the PC stops being lent before the prompt."""
    while True:
        print(f"\nTrial {trial}/{trials}: waiting for {node} to be lent. Step away from the PC.")
        wait_for(lambda: not tainted(core, node), 600, "the worker to go IDLE")

        name = f"evict-{trial:02d}-{uuid.uuid4().hex[:5]}"
        core.create_namespaced_pod(NAMESPACE, test_pod(name, node))
        try:
            wait_for(lambda: pod_running(core, name), 120, f"pod {name} to run")
        except TimeoutError:
            print("  pod did not start (PC taken back?), retrying")
            delete_pod(core, name)
            continue
        if tainted(core, node):
            print("  PC was taken back before the prompt, retrying")
            delete_pod(core, name)
            continue

        print(f"  >>> PRESS A KEY ON THE WORKER NOW <<<\a", flush=True)
        prompt = time.time()
        try:
            seen = measure(core, node, name)
        finally:
            delete_pod(core, name)
        taint_to_stop = seen["pod_stopped"] - seen["taint_seen"]
        print(f"  recorded: pod stopped {taint_to_stop:.2f}s after the taint appeared")
        return {"trial": trial, "pod": name, "prompted": prompt, **seen}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--node", required=True, help="worker node name")
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "results" / "eviction_k8s.csv",
    )
    args = parser.parse_args()

    config.load_kube_config()
    core = client.CoreV1Api()
    core.read_node(args.node)  # fail now if the name is wrong

    new_file = not args.out.exists()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", newline="") as file:
        writer = csv.DictWriter(file, FIELDS)
        if new_file:
            writer.writeheader()
        for trial in range(1, args.trials + 1):
            writer.writerow(run_trial(core, args.node, trial, args.trials))
            file.flush()  # keep finished trials if the run is stopped
    print(f"\nDone. {args.trials} trials appended to {args.out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nstopped; finished trials are already saved")
