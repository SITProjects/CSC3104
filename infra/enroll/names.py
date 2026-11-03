"""Peer names and types, with the same rules as infra/wireguard/add-peer.sh."""

import re

from enroll.errors import BadRequest

# Each type's last octet in 10.10.0.0/24. The hub is .1.
RANGES = {
    "laptop": range(10, 100),
    "worker": range(100, 200),
    "burst": range(200, 205),
}

_ANY = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}")
# Worker and burst names also become Kubernetes node names
_NODE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_SLOT = re.compile(r"slot-(20[0-4])")


def check_name(name: str) -> str:
    """Any peer name. Also guards paths: no dots or slashes can get through."""
    if not isinstance(name, str) or not _ANY.fullmatch(name):
        raise BadRequest("Invalid peer name.")
    return name


def octets_for(name: str, peer_type: str) -> range:
    """The addresses a new peer of this name and type may get."""
    check_name(name)
    if peer_type not in RANGES:
        raise BadRequest(f"Unknown peer type: {peer_type}")
    if peer_type == "burst":
        slot = _SLOT.fullmatch(name)
        if not slot:
            raise BadRequest("Burst peers must be named slot-200 to slot-204.")
        octet = int(slot.group(1))
        return range(octet, octet + 1)
    if name.startswith("slot-"):
        raise BadRequest("Names starting with slot- are reserved for burst workers.")
    if peer_type == "worker" and not _NODE.fullmatch(name):
        raise BadRequest("Use a lowercase Kubernetes node name.")
    return RANGES[peer_type]


def joins_cluster(peer_type: str) -> bool:
    """Workers and burst slots run a K3s agent; laptops only use the VPN."""
    return peer_type in ("worker", "burst")
