import logging
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Node, Replica

log = logging.getLogger("verifier")

INITIAL_TRUST = Decimal("0.5")
AGREEMENT_REWARD = Decimal("0.05")
DISAGREEMENT_PENALTY = Decimal("0.2")
HIGH_TRUST_THRESHOLD = Decimal("0.8")
LOW_TRUST_THRESHOLD = Decimal("0.3")
QUARANTINE_THRESHOLD = Decimal("0.1")


def update_trust_score(score: Decimal, agrees: bool) -> Decimal:
    """
    Calculates a PC's new trust score after comparing a result with the majority.

    A disagreement also drops the score to INITIAL_TRUST at most, so even a PC
    at 1.0 loses its trusted status at once. Its results are then checked again
    until it earns trust back, like BOINC resetting a host after a bad result.

    The caller supplies a valid database score and only scores successful results
    once a majority exists. This helper does not save scores or change nodes.

    args:
        score: The PC's current trust score.
        agrees: Whether the replica's result hash matches the winning hash.
    returns:
        Decimal: The updated score, clamped between zero and one.
    """
    if agrees:
        return min(Decimal(1), score + AGREEMENT_REWARD)
    return max(Decimal(0), min(INITIAL_TRUST, score - DISAGREEMENT_PENALTY))


def trust_label(score: Decimal) -> str:
    """
    Returns the trust label for a valid score between zero and one.

    args:
        score: The PC's trust score.
    returns:
        str: high at 0.8 or above, low below 0.3, otherwise medium.
    """
    if score >= HIGH_TRUST_THRESHOLD:
        return "high"
    if score < LOW_TRUST_THRESHOLD:
        return "low"
    return "medium"


def should_quarantine(score: Decimal) -> bool:
    """
    Checks whether a PC's score is below the quarantine threshold.

    args:
        score: The PC's trust score.
    returns:
        bool: True below 0.1. Applying a NoSchedule taint is left to integration.
    """
    return score < QUARANTINE_THRESHOLD


async def score_replicas(
    session: AsyncSession, replicas: Sequence[Replica], winner: str
) -> None:
    """
    Scores successful final attempts whose node assignments still exist.

    Missing assignments or node records are skipped with a warning, allowing
    the majority result to be verified. Skipped scores are not backfilled.
    Node rows are locked in name order to serialize concurrent score updates.
    Database errors propagate so scoring and verification roll back together.

    args:
        session: The caller's transaction, holding the task lock.
        replicas: Successful replicas with coordinator-validated result hashes.
        winner: The majority result hash.
    """
    names = {
        replica.node_name
        for replica in replicas
        if replica.node_name and replica.node_name.strip()
    }
    nodes = (
        (
            await session.scalars(
                select(Node)
                .where(Node.name.in_(names))
                .order_by(Node.name)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        if names
        else []
    )
    by_name = {node.name: node for node in nodes}
    for replica in sorted(replicas, key=lambda replica: replica.id):
        node = by_name.get(replica.node_name)
        if node is None:
            log.warning(
                "Skipping trust for replica %s (task %s): node %r is unknown",
                replica.id,
                replica.task_id,
                replica.node_name,
            )
            continue
        agrees = replica.result_hash == winner
        node.trust_score = update_trust_score(node.trust_score, agrees)
        if agrees:
            node.total_agree += 1
        else:
            node.total_disagree += 1
