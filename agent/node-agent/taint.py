"""Add or remove the owner-active taint on this worker's own Kubernetes node.

With the taint (NoExecute), Kubernetes evicts the task pods running here and
places no new ones. The OCR worker then saves a checkpoint and the coordinator
requeues the replica on another PC. Without it, the node takes work again.

Other parts taint nodes too (the verifier quarantines low-trust nodes with a
NoSchedule taint), so we read the node, change only our own taint, and send the
patch with the resourceVersion we read. If anything changed the node in
between, the API server answers 409 and we read it again.
"""

import logging
import threading

from kubernetes import client
from kubernetes import config as kube_config
from kubernetes.client.rest import ApiException

log = logging.getLogger("node-agent")

TAINT_KEY = "idlecloud/owner-active"
TAINT_VALUE = "true"
TAINT_EFFECT = "NoExecute"

# Conflicts only happen when something else writes the node in the few
# milliseconds between our read and our patch, so a few tries are plenty
CONFLICT_TRIES = 5


def connect(kubeconfig: str) -> client.CoreV1Api:
    # Not load_incluster_config: the agent runs under systemd on the worker,
    # not as a pod
    return client.CoreV1Api(kube_config.new_client_from_config(config_file=kubeconfig))


def _is_ours(taint: dict) -> bool:
    return taint.get("key") == TAINT_KEY and taint.get("effect") == TAINT_EFFECT


class NodeTainter:
    def __init__(self, core: client.CoreV1Api, node_name: str) -> None:
        self.core = core
        self.node_name = node_name
        # One change at a time, and none after close()
        self._lock = threading.Lock()
        self._closed = False

    def set_owner_active(self, active: bool) -> bool:
        """Make the taint present (active) or absent. Returns True if the node
        was changed, False if it was already right or the tainter is closed.
        Raises ApiException if the API server refuses, so the caller can retry
        later."""
        with self._lock:
            if self._closed:
                return False
            return self._set_owner_active(active)

    def close(self) -> None:
        """Taint the node and ignore every later change. On shutdown a remove
        can still be running, and it must not clear the taint after this."""
        with self._lock:
            self._closed = True
            self._set_owner_active(True)

    def _set_owner_active(self, active: bool) -> bool:
        for _ in range(CONFLICT_TRIES):
            node = self.core.read_node(self.node_name)
            # Plain dicts keep each taint exactly as read, including timeAdded
            taints = self.core.api_client.sanitize_for_serialization(
                node.spec.taints or []
            )
            others = [t for t in taints if not _is_ours(t)]
            has_ours = len(others) != len(taints)
            if has_ours == active:
                return False
            wanted = others
            if active:
                wanted = others + [
                    {"key": TAINT_KEY, "value": TAINT_VALUE, "effect": TAINT_EFFECT}
                ]
            body = {
                "metadata": {"resourceVersion": node.metadata.resource_version},
                # A list here replaces the node's whole taint list
                "spec": {"taints": wanted},
            }
            try:
                self.core.patch_node(self.node_name, body)
            except ApiException as error:
                if error.status == 409:
                    log.info("node %s changed while patching, retrying", self.node_name)
                    continue
                raise
            return True
        raise RuntimeError(
            f"node {self.node_name} kept changing, gave up after {CONFLICT_TRIES} tries"
        )
