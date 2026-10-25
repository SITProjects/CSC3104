"""Reconcile database trust into Kubernetes without blocking voting."""

import asyncio
import logging

from kubernetes import client, config
from kubernetes.client import ApiException
from sqlalchemy import select

from shared.db.models import Node

from .trust import should_quarantine, trust_label

log = logging.getLogger("verifier")
TRUST_LABEL = "idlecloud/trust"
QUARANTINE_TAINT = "idlecloud/quarantined"


def sync_node(api, name, score):
    """
    Synchronizes the database trust score with the node's label and taint.

    A quarantined node can be manually recovered by resetting its database trust
    score to 0.1 after the node has been checked or fixed. The next sync removes
    the quarantine taint while preserving its existing agreement history.

    resourceVersion protects the taint update from concurrent changes.
    Unrelated taints are preserved.

    args:
        api: The Kubernetes CoreV1Api client.
        name: The Kubernetes node name.
        score: The node's current database trust score.
    raises:
        ApiException: If reading or patching the Kubernetes node fails.
    """
    node = api.read_node(name, _request_timeout=10)
    labels = node.metadata.labels or {}
    current = api.api_client.sanitize_for_serialization(node.spec.taints or [])
    label = trust_label(score)
    desired = [taint for taint in current if taint["key"] != QUARANTINE_TAINT]
    if should_quarantine(score):
        desired.append(
            {"key": QUARANTINE_TAINT, "value": "true", "effect": "NoSchedule"}
        )
    if labels.get(TRUST_LABEL) == label and current == desired:
        return
    api.patch_node(
        name,
        {
            "metadata": {
                "resourceVersion": node.metadata.resource_version,
                "labels": {TRUST_LABEL: label},
            },
            "spec": {"taints": desired},
        },
        _request_timeout=10,
        _content_type="application/merge-patch+json",
    )


def create_kubernetes_client() -> client.CoreV1Api:
    """
    Creates a Kubernetes client using cluster or local credentials.

    returns:
        CoreV1Api: The configured Kubernetes client.
    raises:
        ConfigException: If neither credential source can be loaded.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


async def trust_sync_loop(session_factory, interval=5):
    """
    Synchronizes database trust scores with Kubernetes node labels and taints.

    Retries configuration, API and database failures while voting continues
    independently. Missing Kubernetes nodes are logged at debug level and
    retried on later passes.

    Reads current database scores every pass, including after service restarts.
    A later pass corrects a score changed while an API request was in flight.

    args:
        session_factory: The verifier's async database session factory.
        interval: Seconds to wait between synchronization passes.
    """
    api = None
    while True:
        try:
            if api is None:
                api = await asyncio.to_thread(create_kubernetes_client)
            async with session_factory() as session:
                scores = (
                    await session.execute(select(Node.name, Node.trust_score))
                ).all()
            for name, score in scores:
                try:
                    await asyncio.to_thread(sync_node, api, name, score)
                except Exception as exc:
                    if isinstance(exc, ApiException) and exc.status == 404:
                        log.debug("Node %s is absent from Kubernetes; will retry", name)
                    else:
                        log.exception(
                            "Could not synchronize trust for node %s; will retry", name
                        )
        except Exception:
            log.exception("Trust synchronization failed; will retry")
        await asyncio.sleep(interval)
