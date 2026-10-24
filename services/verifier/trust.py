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
