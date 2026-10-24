from decimal import Decimal

INITIAL_TRUST = Decimal("0.5")
AGREEMENT_REWARD = Decimal("0.05")
DISAGREEMENT_PENALTY = Decimal("0.2")
HIGH_TRUST_THRESHOLD = Decimal("0.8")
LOW_TRUST_THRESHOLD = Decimal("0.3")
QUARANTINE_THRESHOLD = Decimal("0.1")


def update_trust_score(score: Decimal, agrees: bool) -> Decimal:
    """
    Calculates a PC's new trust score after comparing a result with the majority.

    The caller supplies a valid database score and only scores successful results
    once a majority exists. This helper does not save scores or change nodes.

    args:
        score: The PC's current trust score.
        agrees: Whether the replica's result hash matches the winning hash.
    returns:
        Decimal: The updated score, clamped between zero and one.
    """
    change = AGREEMENT_REWARD if agrees else -DISAGREEMENT_PENALTY
    return max(Decimal(0), min(Decimal(1), score + change))


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


async def score_replicas(session, replicas, winner: str) -> None:
    """Score successful final attempts; caller holds the task lock/transaction.

    Lock nodes in name order to serialize concurrent tasks without lost updates.
    Validate all assignments before changing any score. Task verification and
    these writes commit together, so normal polling cannot award twice.
    """
    from sqlalchemy import select

    from shared.db.models import Node

    names = {replica.node_name for replica in replicas}
    if None in names or any(not name.strip() for name in names):
        raise ValueError("Successful replica has no node assignment")
    nodes = (
        await session.scalars(
            select(Node)
            .where(Node.name.in_(names))
            .order_by(Node.name)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).all()
    by_name = {node.name: node for node in nodes}
    if set(by_name) != names:
        raise ValueError("Successful replica's node record is missing")
    for replica in sorted(replicas, key=lambda replica: replica.id):
        node = by_name[replica.node_name]
        agrees = replica.result_hash == winner
        node.trust_score = update_trust_score(node.trust_score, agrees)
        if agrees:
            node.total_agree += 1
        else:
            node.total_disagree += 1
