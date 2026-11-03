"""Helper settings, from /etc/idlecloud/enroll.env (NAME=value lines) or the
environment. Real environment variables win over the file."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = Path("/etc/idlecloud/enroll.env")


@dataclass(frozen=True)
class Config:
    # The hub's public WireGuard address that peers connect to, e.g. 1.2.3.4:51820
    hub_endpoint: str
    socket_path: Path = Path("/run/idlecloud/enroll.sock")
    # Group allowed to use the socket (the coordinator Pod's supplemental group)
    socket_gid: int | None = None
    wg_interface: str = "wg0"
    wg_config: Path = Path("/etc/wireguard/wg0.conf")
    # One folder per peer with its keys, wg0.conf and join token (root only)
    peers_dir: Path = Path("/etc/wireguard/peers")
    peers_file: Path = Path("/etc/wireguard/peers.md")
    # Shared with add-peer.sh, so the two never change wg0 at the same time
    lock_path: Path = Path("/run/wireguard-add-peer.lock")
    kubeconfig: Path = Path("/etc/rancher/k3s/k3s.yaml")
    # How long a join token works. A joined PC keeps working after it expires.
    join_token_ttl: str = "24h"


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def load(env: Mapping[str, str] = os.environ, env_file: Path = ENV_FILE) -> Config:
    values = {**read_env_file(env_file), **env}
    endpoint = values.get("HUB_ENDPOINT", "")
    if not endpoint:
        raise SystemExit(f"Set HUB_ENDPOINT (host:port) in {env_file}")
    defaults = Config(hub_endpoint=endpoint)

    def path(name: str, default: Path) -> Path:
        return Path(values[name]) if values.get(name) else default

    gid = values.get("SOCKET_GID")
    return Config(
        hub_endpoint=endpoint,
        socket_path=path("SOCKET_PATH", defaults.socket_path),
        socket_gid=int(gid) if gid else None,
        wg_interface=values.get("WG_INTERFACE") or defaults.wg_interface,
        wg_config=path("WG_CONFIG", defaults.wg_config),
        peers_dir=path("PEERS_DIR", defaults.peers_dir),
        peers_file=path("PEERS_FILE", defaults.peers_file),
        lock_path=path("LOCK_PATH", defaults.lock_path),
        kubeconfig=path("KUBECONFIG", defaults.kubeconfig),
        join_token_ttl=values.get("JOIN_TOKEN_TTL") or defaults.join_token_ttl,
    )
