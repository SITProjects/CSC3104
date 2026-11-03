"""WireGuard on the hub: keys, address allocation, and the hub's wg0.conf.

The file formats match infra/wireguard/add-peer.sh exactly, so peers added by
either one look the same.
"""

import os
import re
import shutil
import tempfile
from collections.abc import Collection, Iterable
from pathlib import Path

from enroll.errors import Conflict
from enroll.run import Runner

SUBNET = "10.10.0"
HUB_IP = f"{SUBNET}.1"
_IP32 = re.compile(rf"(?<![0-9.]){re.escape(SUBNET)}\.(\d{{1,3}})/32(?![0-9])")
_ROW_IP = re.compile(rf"\|\s*{re.escape(SUBNET)}\.(\d{{1,3}})\s*\|")
_SAVE_CONFIG = re.compile(r"^\s*SaveConfig\s*=\s*true", re.IGNORECASE | re.MULTILINE)
_SECTION = re.compile(r"^\s*\[(\w+)\]\s*$")


def hub_public_key(run: Runner, interface: str) -> str:
    key = run(["wg", "show", interface, "public-key"]).strip()
    if not key or key == "(none)":
        raise Conflict(f"The hub's {interface} interface has no key configured.")
    return key


def generate_keypair(run: Runner) -> tuple[str, str]:
    private = run(["wg", "genkey"]).strip()
    public = run(["wg", "pubkey"], stdin=private + "\n").strip()
    return private, public


def used_octets(*texts: str) -> set[int]:
    """Last octets already taken in wg0.conf, the live peers or peers.md."""
    used = set()
    for text in texts:
        used.update(int(m) for m in _IP32.findall(text))
        used.update(int(m) for m in _ROW_IP.findall(text))
    return used


def allocate(octets: Iterable[int], used: Collection[int]) -> str:
    for octet in octets:
        if octet not in used:
            return f"{SUBNET}.{octet}"
    raise Conflict("No free address left for this peer type.")


def check_config(text: str) -> None:
    # wg-quick would overwrite our edits with its own copy on the next restart
    if _SAVE_CONFIG.search(text):
        raise Conflict("Remove SaveConfig=true from the hub configuration first.")


def client_config(private_key: str, ip: str, hub_key: str, endpoint: str) -> str:
    """The peer's own wg0.conf."""
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        f"Address = {ip}/24\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {hub_key}\n"
        f"Endpoint = {endpoint}\n"
        f"AllowedIPs = {SUBNET}.0/24\n"
        "PersistentKeepalive = 25\n"
    )


def peer_block(name: str, public_key: str, ip: str) -> str:
    """What the hub's wg0.conf gets for one peer."""
    return f"\n# {name}\n[Peer]\nPublicKey = {public_key}\nAllowedIPs = {ip}/32\n"


def has_peer(text: str, public_key: str) -> bool:
    return any(public_key in _keys(section) for section in _sections(text))


def remove_peer(text: str, public_key: str) -> str:
    """The hub config without the [Peer] section holding this key, including
    the comment lines (and one blank line) written just above it."""
    kept = [s for s in _sections(text) if public_key not in _keys(s)]
    return "".join(line for section in kept for line in section)


def _sections(text: str) -> list[list[str]]:
    """Lines split before each [Section]. Comments and one blank line directly
    above a header belong to that header's section."""
    sections: list[list[str]] = [[]]
    for line in text.splitlines(keepends=True):
        if _SECTION.match(line):
            current = sections[-1]
            start = len(current)
            while start > 0 and current[start - 1].lstrip().startswith("#"):
                start -= 1
            if start > 0 and not current[start - 1].strip():
                start -= 1
            sections.append(current[start:])
            del current[start:]
        sections[-1].append(line)
    return sections


def _keys(section: list[str]) -> set[str]:
    if not section or not any(_SECTION.match(line) for line in section):
        return set()
    header = next(line for line in section if _SECTION.match(line))
    if header.strip().lower() != "[peer]":
        return set()
    keys = set()
    for line in section:
        key, _, value = line.partition("=")
        if key.strip().lower() == "publickey":
            keys.add(value.strip())
    return keys


def set_peer(run: Runner, interface: str, public_key: str, ip: str) -> None:
    run(["wg", "set", interface, "peer", public_key, "allowed-ips", f"{ip}/32"])


def unset_peer(run: Runner, interface: str, public_key: str) -> None:
    """Removing a peer that isn't there is not an error."""
    run(["wg", "set", interface, "peer", public_key, "remove"])


def write_atomic(
    path: Path, text: str, mode: int = 0o600, backup: bool = False
) -> None:
    """Replace the file in one step, so a crash never leaves half of it."""
    if backup and path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
