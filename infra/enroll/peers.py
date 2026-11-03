"""Each peer's folder in /etc/wireguard/peers/<name>/ and the peers.md table.

The folder layout is add-peer.sh's: privatekey, publickey, wg0.conf, and for
peers that join the cluster, agent-token. agent-token.expires holds the token's
expiry time. Everything is root-only.
"""

import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from enroll.wg import HUB_IP, write_atomic

PEERS_HEADER = (
    "# WireGuard peers\n\n"
    "| Name | VPN IP | Role |\n"
    "|---|---|---|\n"
    f"| hub | {HUB_IP} | server |\n"
)
_ADDRESS = re.compile(r"^\s*Address\s*=\s*([0-9.]+)/", re.MULTILINE)


@dataclass(frozen=True)
class Peer:
    name: str
    peer_type: str
    ip: str
    public_key: str


class PeerStore:
    def __init__(self, peers_dir: Path, peers_file: Path):
        self.dir = peers_dir
        self.file = peers_file

    def folder(self, name: str) -> Path:
        return self.dir / name

    def names(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(
            p.name
            for p in self.dir.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )

    def load(self, name: str) -> Peer | None:
        folder = self.folder(name)
        if not (folder / "wg0.conf").is_file():
            return None
        address = _ADDRESS.search((folder / "wg0.conf").read_text())
        return Peer(
            name=name,
            peer_type=self.row_type(name) or "unknown",
            ip=address.group(1) if address else "",
            public_key=(folder / "publickey").read_text().strip(),
        )

    def create(self, name: str, files: dict[str, str]) -> None:
        """Write every file into a staging folder, then move it into place, so
        a failure halfway leaves no folder behind."""
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self.dir, prefix=f".{name}."))
        try:
            for filename, text in files.items():
                write_atomic(staging / filename, text)
            staging.rename(self.folder(name))
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def read(self, name: str, filename: str) -> str | None:
        path = self.folder(name) / filename
        return path.read_text() if path.is_file() else None

    def write(self, name: str, filename: str, text: str) -> None:
        write_atomic(self.folder(name) / filename, text)

    def remove_file(self, name: str, filename: str) -> None:
        (self.folder(name) / filename).unlink(missing_ok=True)

    def delete(self, name: str) -> None:
        shutil.rmtree(self.folder(name), ignore_errors=True)

    # ---------- peers.md ----------

    def table(self) -> str:
        return self.file.read_text() if self.file.is_file() else ""

    def row_type(self, name: str) -> str | None:
        for cells in self._rows():
            if cells[0] == name:
                return cells[2]
        return None

    def add_row(self, name: str, ip: str, peer_type: str) -> None:
        if self.row_type(name) is not None:
            return
        text = self.table() or PEERS_HEADER
        if not text.endswith("\n"):
            text += "\n"
        write_atomic(self.file, text + f"| {name} | {ip} | {peer_type} |\n", mode=0o644)

    def remove_row(self, name: str) -> None:
        text = self.table()
        lines = text.splitlines(keepends=True)
        kept = [line for line in lines if _cells(line)[:1] != [name]]
        if len(kept) != len(lines):
            write_atomic(self.file, "".join(kept), mode=0o644)

    def _rows(self) -> list[list[str]]:
        rows = (_cells(line) for line in self.table().splitlines())
        return [cells for cells in rows if len(cells) >= 3]


def _cells(line: str) -> list[str]:
    line = line.strip()
    if not line.startswith("|"):
        return []
    return [cell.strip() for cell in line.strip("|").split("|")]
