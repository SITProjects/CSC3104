"""K3s join tokens and node lookups on the hub.

A join token is a K3s bootstrap token, K10<ca-hash>::<id>.<secret>. It is used
once, when a PC's K3s agent first joins; after that the agent has its own
certificates, so a joined PC keeps working when the token expires.
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

from enroll.errors import BadRequest, CommandFailed
from enroll.run import Runner

_TOKEN = re.compile(r"(?:K10[0-9a-f]+::)?([a-z0-9]{6})\.[a-z0-9]{16}")
_LIST_ID = re.compile(r"^\s*([a-z0-9]{6})\s", re.MULTILINE)
_DURATION = re.compile(r"(\d+)([hms])")


def parse_ttl(ttl: str) -> timedelta:
    """A Go duration as k3s takes it: 24h, 30m, 1h30m."""
    parts = _DURATION.findall(ttl)
    if not parts or "".join(n + u for n, u in parts) != ttl:
        raise BadRequest(f"Invalid duration: {ttl}")
    seconds = {"h": 3600, "m": 60, "s": 1}
    total = timedelta(seconds=sum(int(n) * seconds[u] for n, u in parts))
    if total <= timedelta(0):
        raise BadRequest("The join token's lifetime must be positive.")
    return total


def token_id(token: str) -> str:
    match = _TOKEN.fullmatch(token.strip())
    if not match:
        raise ValueError("not a K3s bootstrap token")
    return match.group(1)


def create_join_token(
    run: Runner, kubeconfig: Path, ttl: str, description: str, now: datetime
) -> tuple[str, datetime]:
    """A new token and when it stops working."""
    expires = now + parse_ttl(ttl)
    output = run(
        [
            "k3s",
            "token",
            "create",
            "--kubeconfig",
            str(kubeconfig),
            "--ttl",
            ttl,
            "--description",
            description,
        ]
    )
    token = output.strip().splitlines()[-1].strip() if output.strip() else ""
    try:
        token_id(token)
    except ValueError:
        raise CommandFailed("k3s", 0) from None
    return token, expires


def delete_join_token(run: Runner, kubeconfig: Path, token: str) -> None:
    """Revoke a token. One that already expired or was deleted is not an error."""
    tid = token_id(token)
    try:
        run(["k3s", "token", "delete", "--kubeconfig", str(kubeconfig), tid])
    except CommandFailed:
        listed = run(["k3s", "token", "list", "--kubeconfig", str(kubeconfig)])
        if tid in _LIST_ID.findall(listed):
            raise


def node_names(run: Runner, kubeconfig: Path) -> set[str]:
    """Every node in the cluster: a worker that has joined is one of them."""
    output = run(
        ["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes", "-o", "name"]
    )
    return {line.removeprefix("node/") for line in output.split()}
