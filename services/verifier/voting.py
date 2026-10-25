from collections import Counter

MAX_REPLICAS = 5


def winning_hash(successful_hashes: list[str], total_replicas: int) -> str | None:
    """
    Returns the result hash supported by a strict majority of replicas.

    A hash needs more than half of all replicas: two out of three, for example.
    Failed replicas count towards the total but contribute no hash, so failures
    never lower the majority threshold. The caller must wait for all replicas
    to finish and supply one valid hash per successful replica (DONE with exit
    code 0). This helper only compares hashes; it does not change task status.

    args:
        successful_hashes: The SHA-256 result hashes from successful replicas.
        total_replicas: All replicas for the task, including failures (1 to 5).
    returns:
        str | None: The winning hash, or None when there is no strict majority.
    raises:
        ValueError: If the total is outside 1 to 5, or there are too many hashes.
    """
    if not 1 <= total_replicas <= MAX_REPLICAS:
        raise ValueError(f"total_replicas must be between 1 and {MAX_REPLICAS}")
    if len(successful_hashes) > total_replicas:
        raise ValueError("successful hashes cannot outnumber replicas")

    threshold = total_replicas // 2 + 1
    for result_hash, count in Counter(successful_hashes).items():
        if count >= threshold:
            return result_hash
    return None


def dispute_exclusions(previous_nodes: list[str | None]) -> list[str]:
    """
    Builds the PC exclusion list for additional replicas of a disputed task.

    Include current replica assignments and assignments from previous retries.
    Unknown PCs are rejected: an incomplete list cannot guarantee a fresh PC.
    This is a placement rule only; assigning PCs is left to later integration.

    args:
        previous_nodes: PCs that already processed the task, including retries.
    returns:
        list[str]: Distinct PC names in sorted order.
    raises:
        ValueError: If there are no assignments or any PC name is missing.
    """
    if not previous_nodes or any(
        not isinstance(node, str) or not node.strip() for node in previous_nodes
    ):
        raise ValueError("Every previous replica must have a known PC assignment")
    return sorted(set(previous_nodes))
