from collections import Counter


def winning_hash(successful_hashes: list[str], total_replicas: int) -> str | None:
    """
    Returns the result hash supported by a strict majority of replicas.

    Two matching hashes are needed out of three replicas, or three out of five.
    Failed replicas count towards the total but contribute no hash, so failures
    never lower the majority threshold. The caller must wait for all replicas
    to finish and supply one valid hash per successful replica (DONE with exit
    code 0). This helper only compares hashes; it does not change task status.

    args:
        successful_hashes: The SHA-256 result hashes from successful replicas.
        total_replicas: All replicas for the task, including failed ones (1 to 5).
    returns:
        str | None: The winning hash, or None when there is no strict majority.
    raises:
        ValueError: If the total is outside 1 to 5 or there are too many hashes.
    """
    if not 1 <= total_replicas <= 5:
        raise ValueError("total_replicas must be between 1 and 5")
    if len(successful_hashes) > total_replicas:
        raise ValueError("successful hashes cannot outnumber replicas")

    threshold = total_replicas // 2 + 1
    for result_hash, count in Counter(successful_hashes).items():
        if count >= threshold:
            return result_hash
    return None
