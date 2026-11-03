"""The helper's HTTP API on a Unix socket, for the coordinator (the web admin
pages). There is no command-line client.

    GET    /healthz
    GET    /peers                     every peer
    POST   /peers                     {"name", "type"}: add one (or return it)
    GET    /peers/{name}
    DELETE /peers/{name}              remove it everywhere
    POST   /peers/{name}/join-token   replace its K3s join token
    DELETE /peers/{name}/join-token   revoke it
    POST   /peers/{name}/identity     make the node agent's kubeconfig (workers)
    POST   /peers/{name}/heartbeat-token  a new coordinator.token (joined workers)
    GET    /peers/{name}/files        before joining: wg0.conf, agent-token and
                                      node-agent.kubeconfig, for the setup zip;
                                      after: node-agent.kubeconfig and
                                      coordinator.token, for the agent credentials

Each connection gets its own thread, so a slow client never holds up the
others. Every change runs under the same file lock as add-peer.sh, so changes
still happen one at a time; reads never see half a file, since every write
replaces its file in one step.
Every action can be repeated: adding an existing peer returns it, and finishes
any step a crash left undone.
"""

import fcntl
import json
import logging
import os
import re
import signal
import socketserver
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from enroll import identity, k3s, names, wg
from enroll.config import Config, load
from enroll.errors import BadRequest, CommandFailed, Conflict, HelperError, NotFound
from enroll.peers import Peer, PeerStore
from enroll.run import Runner, run

log = logging.getLogger("enroll")

MAX_BODY = 16 * 1024
# A client that stops sending has its connection dropped after this long, so
# its thread does not wait forever
CLIENT_TIMEOUT_S = 10
TOKEN_FILE = "agent-token"
EXPIRES_FILE = "agent-token.expires"
KUBECONFIG_FILE = "node-agent.kubeconfig"
HEARTBEAT_FILE = "coordinator.token"
HEARTBEAT_EXPIRES_FILE = "coordinator.token.expires"


class Service:
    def __init__(
        self,
        config: Config,
        runner: Runner = run,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.run = runner
        self.clock = clock
        self.sleep = sleep
        self.store = PeerStore(config.peers_dir, config.peers_file)

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.config.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    # ---------- reads ----------

    def health(self) -> dict:
        wg.hub_public_key(self.run, self.config.wg_interface)
        return {"ok": True}

    def list_peers(self) -> list[dict]:
        joined = self._joined_nodes()
        return [
            self._describe(peer, joined)
            for name in self.store.names()
            if (peer := self.store.load(name))
        ]

    def get_peer(self, name: str) -> dict:
        return self._describe(self._peer(name), self._joined_nodes())

    def files(self, name: str) -> dict:
        peer = self._peer(name)
        worker = peer.peer_type == "worker"
        joined = names.joins_cluster(peer.peer_type) and self._has_joined(name)
        if not names.joins_cluster(peer.peer_type):
            wanted = ["wg0.conf"]
        elif joined:
            if not worker:
                raise Conflict(f"{name} has already joined the cluster.")
            # Its keys and join token are only needed to join; after that they
            # stay on the hub
            wanted = [KUBECONFIG_FILE, HEARTBEAT_FILE]
        else:
            wanted = ["wg0.conf", TOKEN_FILE] + ([KUBECONFIG_FILE] if worker else [])
        files = {}
        for filename in wanted:
            if (text := self.store.read(name, filename)) is not None:
                files[filename] = text
        return {
            "name": name,
            "joined": joined,
            "files": files,
            "join_token_expires_at": self.store.read(name, EXPIRES_FILE),
            "heartbeat_token_expires_at": self.store.read(name, HEARTBEAT_EXPIRES_FILE),
        }

    # ---------- changes ----------

    def create_peer(self, name: str, peer_type: str) -> dict:
        octets = names.octets_for(name, peer_type)
        with self.locked():
            existing = self.store.load(name)
            if existing:
                if existing.peer_type not in (peer_type, "unknown"):
                    raise Conflict(f"{name} already exists as a {existing.peer_type}.")
                self._register(name, existing.public_key, existing.ip, peer_type)
                return self.get_peer(name)
            self._check_name_free(name)

            interface = self.config.wg_interface
            hub_conf = self.config.wg_config.read_text()
            wg.check_config(hub_conf)
            hub_key = wg.hub_public_key(self.run, interface)
            live = self.run(["wg", "show", interface, "allowed-ips"])
            ip = wg.allocate(octets, wg.used_octets(hub_conf, live, self.store.table()))
            private, public = wg.generate_keypair(self.run)
            self.store.create(
                name,
                {
                    "privatekey": private + "\n",
                    "publickey": public + "\n",
                    "wg0.conf": wg.client_config(
                        private, ip, hub_key, self.config.hub_endpoint
                    ),
                },
            )
            self._register(name, public, ip, peer_type)
            log.info("added %s %s at %s", peer_type, name, ip)
        return self.get_peer(name)

    def issue_join_token(self, name: str) -> dict:
        with self.locked():
            peer = self._peer(name)
            if not names.joins_cluster(peer.peer_type):
                raise Conflict(
                    f"{name} is a {peer.peer_type} and never joins the cluster."
                )
            self._revoke(name)
            token, expires = k3s.create_join_token(
                self.run,
                self.config.kubeconfig,
                self.config.join_token_ttl,
                f"idlecloud worker {name}",
                self.clock(),
            )
            self.store.write(name, TOKEN_FILE, token + "\n")
            self.store.write(name, EXPIRES_FILE, expires.isoformat())
            log.info("issued a join token for %s, valid until %s", name, expires)
        return {"name": name, "join_token_expires_at": expires.isoformat()}

    def revoke_join_token(self, name: str) -> None:
        with self.locked():
            self._peer(name)
            self._revoke(name)

    def make_agent_identity(self, name: str) -> dict:
        with self.locked():
            self._worker(name)
            kubeconfig = identity.make_kubeconfig(
                self.run,
                self.config.kubeconfig,
                self.config.node_agent_policy,
                name,
                self.config.api_server,
                self.sleep,
            )
            self.store.write(name, KUBECONFIG_FILE, kubeconfig)
            log.info("made the node agent's kubeconfig for %s", name)
        return self.get_peer(name)

    def issue_heartbeat_token(self, name: str) -> dict:
        with self.locked():
            self._worker(name)
            if not self._has_joined(name):
                # The token is bound to the Node, which exists only after joining
                raise Conflict(f"{name} has not joined the cluster yet.")
            token, expires = identity.heartbeat_token(
                self.run,
                self.config.kubeconfig,
                name,
                self.config.heartbeat_duration,
                self.clock(),
            )
            self.store.write(name, HEARTBEAT_FILE, token + "\n")
            self.store.write(name, HEARTBEAT_EXPIRES_FILE, expires.isoformat())
            log.info("issued a heartbeat token for %s, valid until %s", name, expires)
        return {"name": name, "heartbeat_token_expires_at": expires.isoformat()}

    def delete_peer(self, name: str) -> None:
        names.check_name(name)
        with self.locked():
            peer = self.store.load(name)
            if peer:
                self._revoke(name)
                # Node first: without the node, its heartbeat token stops working
                if names.joins_cluster(peer.peer_type):
                    identity.remove_node(self.run, self.config.kubeconfig, name)
                if peer.peer_type == "worker":
                    identity.delete_account(self.run, self.config.kubeconfig, name)
                wg.unset_peer(self.run, self.config.wg_interface, peer.public_key)
                hub_conf = self.config.wg_config.read_text()
                if wg.has_peer(hub_conf, peer.public_key):
                    wg.write_atomic(
                        self.config.wg_config,
                        wg.remove_peer(hub_conf, peer.public_key),
                        backup=True,
                    )
            self.store.remove_row(name)
            self.store.delete(name)
            log.info("removed %s", name)

    # ---------- helpers ----------

    def _peer(self, name: str) -> Peer:
        peer = self.store.load(names.check_name(name))
        if peer is None:
            raise NotFound(f"No peer named {name}.")
        return peer

    def _worker(self, name: str) -> Peer:
        peer = self._peer(name)
        if peer.peer_type != "worker":
            # Burst workers run no node agent, and laptops never join
            raise Conflict(f"{name} is a {peer.peer_type}, not a campus worker.")
        return peer

    def _check_name_free(self, name: str) -> None:
        # A row without a folder: added by hand, or a removal that stopped halfway
        if self.store.row_type(name) is not None:
            raise Conflict(f"{name} is already in {self.store.file.name}.")

    def _register(self, name: str, public_key: str, ip: str, peer_type: str) -> None:
        """Make sure the hub knows the peer: wg0.conf, peers.md and the live wg0."""
        hub_conf = self.config.wg_config.read_text()
        if not wg.has_peer(hub_conf, public_key):
            wg.check_config(hub_conf)
            wg.write_atomic(
                self.config.wg_config,
                hub_conf + wg.peer_block(name, public_key, ip),
                backup=True,
            )
        self.store.add_row(name, ip, peer_type)
        wg.set_peer(self.run, self.config.wg_interface, public_key, ip)

    def _revoke(self, name: str) -> None:
        token = self.store.read(name, TOKEN_FILE)
        if token:
            try:
                k3s.delete_join_token(self.run, self.config.kubeconfig, token)
            except ValueError:
                log.warning("%s's agent-token is not a K3s token; removing it", name)
        self.store.remove_file(name, TOKEN_FILE)
        self.store.remove_file(name, EXPIRES_FILE)

    def _joined_nodes(self) -> set[str] | None:
        """Node names, or None when the cluster can't be asked right now."""
        try:
            return k3s.node_names(self.run, self.config.kubeconfig)
        except CommandFailed:
            return None

    def _has_joined(self, name: str) -> bool:
        nodes = self._joined_nodes()
        if nodes is None:
            raise CommandFailed("kubectl", 1)
        return name in nodes

    def _describe(self, peer: Peer, joined_nodes: set[str] | None) -> dict:
        joined: bool | None = False  # laptops never join
        if names.joins_cluster(peer.peer_type):
            joined = None if joined_nodes is None else peer.name in joined_nodes
        return {
            "name": peer.name,
            "type": peer.peer_type,
            "ip": peer.ip,
            "public_key": peer.public_key,
            "joined": joined,
            "join_token_expires_at": self.store.read(peer.name, EXPIRES_FILE),
            "agent_kubeconfig": self.store.read(peer.name, KUBECONFIG_FILE) is not None,
            "heartbeat_token_expires_at": self.store.read(
                peer.name, HEARTBEAT_EXPIRES_FILE
            ),
        }


# ---------- HTTP ----------

_PEER = re.compile(r"/peers/([^/]+)")
_PEER_TOKEN = re.compile(r"/peers/([^/]+)/join-token")
_PEER_FILES = re.compile(r"/peers/([^/]+)/files")
_PEER_IDENTITY = re.compile(r"/peers/([^/]+)/identity")
_PEER_HEARTBEAT = re.compile(r"/peers/([^/]+)/heartbeat-token")


def route(service: Service, method: str, path: str, body: dict) -> tuple[int, object]:
    """(status, JSON body) for one request."""
    if path == "/healthz" and method == "GET":
        return 200, service.health()
    if path == "/peers":
        if method == "GET":
            return 200, service.list_peers()
        if method == "POST":
            name, peer_type = body.get("name"), body.get("type")
            if not isinstance(name, str) or not isinstance(peer_type, str):
                raise BadRequest('Send {"name": ..., "type": ...}.')
            return 200, service.create_peer(name, peer_type)
    if match := _PEER_TOKEN.fullmatch(path):
        if method == "POST":
            return 200, service.issue_join_token(match.group(1))
        if method == "DELETE":
            service.revoke_join_token(match.group(1))
            return 204, None
    if (match := _PEER_FILES.fullmatch(path)) and method == "GET":
        return 200, service.files(match.group(1))
    if (match := _PEER_IDENTITY.fullmatch(path)) and method == "POST":
        return 200, service.make_agent_identity(match.group(1))
    if (match := _PEER_HEARTBEAT.fullmatch(path)) and method == "POST":
        return 200, service.issue_heartbeat_token(match.group(1))
    if match := _PEER.fullmatch(path):
        if method == "GET":
            return 200, service.get_peer(match.group(1))
        if method == "DELETE":
            service.delete_peer(match.group(1))
            return 204, None
    raise NotFound("No such endpoint.")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Applied to the socket by StreamRequestHandler
    timeout = CLIENT_TIMEOUT_S
    service: Service  # set by serve()

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            status, payload = route(self.service, method, self.path, self._body())
        except HelperError as error:
            status, payload = error.status, {"detail": str(error)}
        except Exception:
            log.exception("%s %s failed", method, self.path)
            status, payload = 500, {"detail": "Internal error."}
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        # One request per connection, so no thread lingers on an idle client
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("Invalid Content-Length.") from None
        if length < 0:
            raise BadRequest("Invalid Content-Length.")
        if length > MAX_BODY:
            raise BadRequest("Request body too large.")
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            raise BadRequest("Request body is not JSON.") from None
        if not isinstance(body, dict):
            raise BadRequest("Request body must be a JSON object.")
        return body

    # A Unix socket has no client address to log
    def address_string(self) -> str:
        return "unix"

    def log_message(self, format: str, *args) -> None:
        log.info("%s", format % args)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    # A thread per connection; Service.locked() keeps changes one at a time
    daemon_threads = True
    allow_reuse_address = False


def serve(service: Service, socket_path: Path, gid: int | None) -> UnixServer:
    """Bind the socket (owner root, group gid, mode 0660) and return the server."""
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    handler = type("BoundHandler", (Handler,), {"service": service})
    old_umask = os.umask(0o117)
    try:
        server = UnixServer(str(socket_path), handler)
    finally:
        os.umask(old_umask)
    if gid is not None:
        os.chown(socket_path, -1, gid)
    os.chmod(socket_path, 0o660)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load()
    server = serve(Service(config), config.socket_path, config.socket_gid)
    log.info("listening on %s", config.socket_path)
    # systemd stops the service with SIGTERM; exit normally so the socket is removed
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        server.serve_forever()
    finally:
        config.socket_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
