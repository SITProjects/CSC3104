"""Reconcile database trust into Kubernetes without blocking voting."""

import asyncio
import logging

from kubernetes import client, config
from sqlalchemy import select

from shared.db.models import Node

from .trust import should_quarantine, trust_label

log = logging.getLogger("verifier")
TRUST_LABEL = "idlecloud/trust"
QUARANTINE_TAINT = "idlecloud/quarantined"


def sync_node(api, name, score):
    """Optimistically patch only our label/taint; retry conflicts next poll.

    resourceVersion protects the read/modify/write taint list against concurrent
    owner-active or Spot watcher changes. Unrelated taints are preserved.
    """
    node = api.read_node(name, _request_timeout=10)
    labels = node.metadata.labels or {}
    current = client.ApiClient().sanitize_for_serialization(node.spec.taints or [])
    desired = [taint for taint in current if taint["key"] != QUARANTINE_TAINT]
    if should_quarantine(score):
        desired.append(
            {"key": QUARANTINE_TAINT, "value": "true", "effect": "NoSchedule"}
        )
    if labels.get(TRUST_LABEL) == trust_label(score) and current == desired:
        return
    api.patch_node(
        name,
        {
            "metadata": {
                "resourceVersion": node.metadata.resource_version,
                "labels": {TRUST_LABEL: trust_label(score)},
            },
            "spec": {"taints": desired},
        },
        _request_timeout=10,
        _content_type="application/merge-patch+json",
    )


async def trust_sync_loop(session_factory, interval=5):
    """Retry configuration/API failures; DB voting continues independently.

    Read current database scores every pass, including after service restarts.
    A later pass corrects a score changed while an API request was in flight.
    """
    api = None
    while True:
        try:
            if api is None:

                def connect():
                    try:
                        config.load_incluster_config()
                    except config.ConfigException:
                        config.load_kube_config()
                    return client.CoreV1Api()

                api = await asyncio.to_thread(connect)
            async with session_factory() as session:
                scores = (
                    await session.execute(select(Node.name, Node.trust_score))
                ).all()
            for name, score in scores:
                try:
                    await asyncio.to_thread(sync_node, api, name, score)
                except Exception:
                    log.exception(
                        "Could not synchronize trust for node %s; will retry", name
                    )
        except Exception:
            log.exception("Trust synchronization failed; will retry")
        await asyncio.sleep(interval)
