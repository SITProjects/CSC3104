"""A worker's node-agent credentials, as agent/node-agent/make-kubeconfig.sh
makes them, plus removing a worker's node and credentials.

- node-agent.kubeconfig: its own ServiceAccount, allowed only to get and patch
  its own Node (resourceNames), and node-agent-policy.yml limits the patch to
  the owner-active taint. It can be made before the PC joins: the rules name the
  node, so they do nothing until it exists. The token does not expire; removing
  the worker revokes it.
- coordinator.token: a token for the shared node-agent-heartbeat account, bound
  to the worker's Node, for the coordinator's audience only. Binding needs the
  Node, so it can only be made after the PC joins. It stops working when the
  node is deleted.
"""

import base64
import binascii
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from enroll.errors import CommandFailed
from enroll.k3s import parse_ttl
from enroll.run import Runner

log = logging.getLogger("enroll")

NAMESPACE = "idlecloud-system"
HEARTBEAT_ACCOUNT = "node-agent-heartbeat"
HEARTBEAT_AUDIENCE = "idlecloud-coordinator"  # HEARTBEAT_AUDIENCE in core/settings.py
SECRET_TRIES = 20
SECRET_WAIT_S = 0.5
DRAIN_TIMEOUT = "25s"  # inside run.py's 30 s limit


def account(node: str) -> str:
    return f"node-agent-{node}"


def objects(node: str) -> dict:
    """Everything apply() creates for one node, as a kubectl List."""
    name = account(node)
    return {
        "apiVersion": "v1",
        "kind": "List",
        "items": [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": name, "namespace": NAMESPACE},
            },
            # Nodes are cluster-wide, so this must be a ClusterRole. resourceNames
            # limits it to this one node.
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRole",
                "metadata": {"name": name},
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["nodes"],
                        "resourceNames": [node],
                        "verbs": ["get", "patch"],
                    }
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "ClusterRoleBinding",
                "metadata": {"name": name},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": name,
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": name, "namespace": NAMESPACE}
                ],
            },
            # A token that does not expire, so lab PCs keep working without renewal
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "type": "kubernetes.io/service-account-token",
                "metadata": {
                    "name": f"{name}-token",
                    "namespace": NAMESPACE,
                    "annotations": {"kubernetes.io/service-account.name": name},
                },
            },
            # Shared by all agents for coordinator heartbeats only; no RBAC rules
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": HEARTBEAT_ACCOUNT, "namespace": NAMESPACE},
            },
        ],
    }


def make_kubeconfig(
    run: Runner,
    kubeconfig: Path,
    policy: Path,
    node: str,
    server: str,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Create the node agent's account and return its kubeconfig."""
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    # RBAC says which node; the policy says what may change on it
    run([*kubectl, "apply", "-f", str(policy)])
    run([*kubectl, "apply", "-f", "-"], stdin=json.dumps(objects(node)))

    # The token controller fills the Secret in a moment
    secret = f"{account(node)}-token"
    for _ in range(SECRET_TRIES):
        listed = run([*kubectl, "-n", NAMESPACE, "get", "secret", secret, "-o", "json"])
        data = json.loads(listed).get("data") or {}
        if data.get("token") and data.get("ca.crt"):
            try:
                token = base64.b64decode(data["token"]).decode()
            except (binascii.Error, UnicodeDecodeError):
                raise CommandFailed("kubectl", 0) from None
            return render_kubeconfig(server, data["ca.crt"], account(node), token)
        sleep(SECRET_WAIT_S)
    log.warning("the token for %s was never created", account(node))
    raise CommandFailed("kubectl", 1)


def render_kubeconfig(server: str, ca_data: str, user: str, token: str) -> str:
    """ca_data is the Secret's base64 ca.crt, as kubeconfig wants it."""
    return (
        "apiVersion: v1\n"
        "kind: Config\n"
        "clusters:\n"
        "  - name: idlecloud\n"
        "    cluster:\n"
        f"      server: {server}\n"
        f"      certificate-authority-data: {ca_data}\n"
        "users:\n"
        f"  - name: {user}\n"
        "    user:\n"
        f"      token: {token}\n"
        "contexts:\n"
        f"  - name: {user}\n"
        "    context:\n"
        "      cluster: idlecloud\n"
        f"      user: {user}\n"
        f"current-context: {user}\n"
    )


def heartbeat_token(
    run: Runner, kubeconfig: Path, node: str, duration: str, now: datetime
) -> tuple[str, datetime]:
    """A new heartbeat token for this node and when it expires. Earlier ones
    keep working until they expire or the node is deleted."""
    expires = now + parse_ttl(duration)
    output = run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "-n",
            NAMESPACE,
            "create",
            "token",
            HEARTBEAT_ACCOUNT,
            "--audience",
            HEARTBEAT_AUDIENCE,
            "--bound-object-kind",
            "Node",
            "--bound-object-name",
            node,
            "--duration",
            duration,
        ]
    )
    token = output.strip()
    if not token or any(c.isspace() for c in token):
        raise CommandFailed("kubectl", 0)
    return token, expires


def remove_node(run: Runner, kubeconfig: Path, node: str) -> None:
    """Drain the node, so task Pods get their checkpoint time, then delete it."""
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    try:
        run(
            [
                *kubectl,
                "drain",
                node,
                "--ignore-daemonsets",
                "--delete-emptydir-data",
                "--force",
                f"--timeout={DRAIN_TIMEOUT}",
            ]
        )
    except CommandFailed:
        # Deleting the node still removes its Pods, and the coordinator requeues them
        log.warning("draining %s did not finish; deleting it anyway", node)
    run([*kubectl, "delete", "node", node, "--ignore-not-found"])


def delete_account(run: Runner, kubeconfig: Path, node: str) -> None:
    """Revoke the node agent's access. The shared heartbeat account stays."""
    kubectl = ["kubectl", "--kubeconfig", str(kubeconfig)]
    namespaced = [*kubectl, "-n", NAMESPACE]
    name = account(node)
    # --ignore-not-found belongs to delete, so it must come after it
    run([*kubectl, "delete", "clusterrolebinding", name, "--ignore-not-found"])
    run([*kubectl, "delete", "clusterrole", name, "--ignore-not-found"])
    run([*namespaced, "delete", "secret", f"{name}-token", "--ignore-not-found"])
    run([*namespaced, "delete", "serviceaccount", name, "--ignore-not-found"])
