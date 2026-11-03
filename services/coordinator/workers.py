"""Campus workers, added and removed from the web admin pages. Admins only.

The enrollment helper on the hub (infra/enroll, client in enroll.py) does the
work and keeps each worker's files; this module adds the admin checks, joins in
the node's state from the nodes table, builds the zips the installer takes
(agent/install-worker.ps1 -Bundle / -AgentCredentials), and records an event
for every change and download.

    GET    /admin/workers
    POST   /admin/workers                       {"name"}: add one
    GET    /admin/workers/{name}
    DELETE /admin/workers/{name}
    POST   /admin/workers/{name}/join-token     replace an expired join token
    POST   /admin/workers/{name}/heartbeat-token after joining: a new one
    GET    /admin/workers/{name}/setup.zip      before joining
    GET    /admin/workers/{name}/agent-credentials.zip  after joining
"""

import io
import json
import logging
import zipfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, Field, StrictBool, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from core.settings import NODE_AGENT_COORDINATOR_URL
from services.coordinator.auth import AdminUser
from services.coordinator.crud import create_event, list_nodes
from services.coordinator.enroll import EnrollClient, get_enroll_client
from shared.db import User, session_manager
from shared.db.models import NodeState

log = logging.getLogger("coordinator")
router = APIRouter(prefix="/admin/workers", tags=["admin"])
_BAD_ANSWER = "The enrollment helper sent an unexpected answer."
DbSession = Annotated[AsyncSession, Depends(session_manager.get_session)]
Enroll = Annotated[EnrollClient, Depends(get_enroll_client)]
# A Kubernetes node name, as infra/enroll/names.py and join-worker.sh require
NAME_PATTERN = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
WorkerName = Annotated[str, Path(pattern=NAME_PATTERN, max_length=63)]

# The installer's zip format (Read-Zip in agent/install-worker.ps1)
ZIP_VERSION = 1
ZIP_FILES = {
    "wg_config": "wg0.conf",
    "join_token": "agent-token",
    "kubeconfig": "node-agent.kubeconfig",
    "coordinator_token": "coordinator.token",
}


class WorkerIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN, max_length=63)


class WorkerView(BaseModel):
    name: str
    ip: str
    # None when the cluster could not be asked
    joined: bool | None
    join_token_expires_at: datetime | None
    agent_kubeconfig: bool
    heartbeat_token_expires_at: datetime | None
    # From the nodes table, once the node agent has sent a heartbeat
    state: NodeState | None = None
    last_heartbeat: datetime | None = None


class HelperFiles(BaseModel):
    """The helper's GET /peers/{name}/files answer."""

    # Strict: "yes" or 1 must not pass for joined and hand out the wrong files
    joined: StrictBool
    files: dict[str, str]
    join_token_expires_at: datetime | None = None
    heartbeat_token_expires_at: datetime | None = None


class JoinTokenOut(BaseModel):
    name: str
    join_token_expires_at: datetime


class HeartbeatTokenOut(BaseModel):
    name: str
    heartbeat_token_expires_at: datetime


def _valid[T: BaseModel](model: type[T], data: Any) -> T:
    """The helper's answer as a model; a 502 if it isn't the shape expected."""
    try:
        return model.model_validate(data)
    except ValidationError:
        log.warning("enrollment helper sent an unexpected %s: %r", model.__name__, data)
        raise HTTPException(502, _BAD_ANSWER) from None


def _view(peer: dict, nodes: dict[str, Any]) -> WorkerView:
    view = _valid(WorkerView, peer)
    if node := nodes.get(view.name):
        view.state, view.last_heartbeat = node.state, node.last_heartbeat
    return view


async def _nodes(session: AsyncSession) -> dict[str, Any]:
    async with session:
        return {node.name: node for node in await list_nodes(session)}


async def _worker(enroll: EnrollClient, name: str) -> dict:
    """The helper's peer, if it is a campus worker (not a laptop or burst slot)."""
    peer = await enroll.peer(name)
    if not isinstance(peer, dict) or peer.get("type") != "worker":
        raise HTTPException(404, f"No worker named {name}.")
    return peer


async def _files(enroll: EnrollClient, name: str) -> HelperFiles:
    return _valid(HelperFiles, await enroll.files(name))


async def _record(
    session: AsyncSession, event_type: str, admin: User, name: str, **detail
) -> None:
    # node_name stays empty: the events table points it at nodes, and a worker
    # that has not joined has no row there yet
    async with session.begin():
        await create_event(
            session,
            event_type,
            None,
            None,
            None,
            {"worker": name, "by": admin.email, **detail},
        )


async def _audited[R](
    session: AsyncSession,
    event_type: str,
    admin: User,
    name: str,
    action: Callable[[], Awaitable[R]],
) -> R:
    """
    Records the event, then runs the action on the hub. The record comes first,
    so nothing changes on the hub unrecorded: if the database can't take it,
    the action never runs. If the action then fails, <event_type>_failed is
    recorded with its status.
    """
    await _record(session, event_type, admin, name)
    try:
        return await action()
    except HTTPException as error:
        await _record(
            session, f"{event_type}_failed", admin, name, status=error.status_code
        )
        raise


def _zip(name: str, files: dict[str, str], readme: str) -> bytes:
    """A zip in the installer's format: manifest.json, the files, README.txt."""
    manifest = {
        "version": ZIP_VERSION,
        "node_name": name,
        "coordinator_url": NODE_AGENT_COORDINATOR_URL,
        "files": {key: ZIP_FILES[key] for key in files},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for key, text in files.items():
            archive.writestr(ZIP_FILES[key], text)
        archive.writestr("README.txt", readme)
    return buffer.getvalue()


def _download(data: bytes, filename: str) -> Response:
    return Response(
        data,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # It holds keys and tokens: never keep a copy along the way
            "Cache-Control": "no-store",
        },
    )


def _expired(when: datetime | None) -> bool:
    if when is None:
        return True
    if when.tzinfo is None:  # the helper writes UTC; a bare time is taken as UTC
        when = when.replace(tzinfo=UTC)
    return when <= datetime.now(UTC)


@router.get("")
async def list_workers(
    admin: AdminUser, session: DbSession, enroll: Enroll
) -> list[WorkerView]:
    """Every campus worker, with its node's state once it sends heartbeats."""
    peers = await enroll.peers()
    if not isinstance(peers, list):
        raise HTTPException(502, _BAD_ANSWER)
    nodes = await _nodes(session)
    return [
        _view(p, nodes)
        for p in peers
        if isinstance(p, dict) and p.get("type") == "worker"
    ]


@router.post("", status_code=201)
async def add_worker(
    body: WorkerIn, admin: AdminUser, session: DbSession, enroll: Enroll
) -> WorkerView:
    """
    Adds a campus worker: its WireGuard peer, a join token and its node agent's
    kubeconfig. Adding one that exists finishes any step that failed before,
    and gives it a new join token.

    raises:
        HTTPException: 400 for a bad name, 409 if a laptop or burst slot has
            it, 502 or 503 if the helper failed or can't be reached.
    """

    async def add() -> dict:
        peer = await enroll.add_worker(body.name)
        if not isinstance(peer, dict) or peer.get("type") != "worker":
            kind = peer.get("type") if isinstance(peer, dict) else "different peer"
            raise HTTPException(409, f"{body.name} is a {kind}, not a worker.")
        if not peer.get("joined"):
            await enroll.issue_join_token(body.name)
        await enroll.make_identity(body.name)
        return await enroll.peer(body.name)

    peer = await _audited(session, "worker_added", admin, body.name, add)
    return _view(peer, await _nodes(session))


@router.get("/{name}")
async def get_worker(
    name: WorkerName, admin: AdminUser, session: DbSession, enroll: Enroll
) -> WorkerView:
    return _view(await _worker(enroll, name), await _nodes(session))


@router.delete("/{name}", status_code=204)
async def remove_worker(
    name: WorkerName, admin: AdminUser, session: DbSession, enroll: Enroll
) -> Response:
    """
    Removes a worker everywhere: drains and deletes its node, revokes its node
    agent's access and join token, and removes its WireGuard peer. Its row in
    the nodes table stays, for its history.
    """
    await _worker(enroll, name)
    await _audited(session, "worker_removed", admin, name, lambda: enroll.remove(name))
    return Response(status_code=204)


@router.post("/{name}/join-token")
async def new_join_token(
    name: WorkerName, admin: AdminUser, session: DbSession, enroll: Enroll
) -> JoinTokenOut:
    """Replaces the join token, e.g. once it expired before the PC joined, or
    to rejoin a PC (install-worker.ps1 -Rejoin). The old one stops working."""
    await _worker(enroll, name)
    issued = await _audited(
        session,
        "worker_join_token",
        admin,
        name,
        lambda: enroll.issue_join_token(name),
    )
    return _valid(JoinTokenOut, issued)


@router.post("/{name}/heartbeat-token")
async def new_heartbeat_token(
    name: WorkerName, admin: AdminUser, session: DbSession, enroll: Enroll
) -> HeartbeatTokenOut:
    """
    Issues a new heartbeat token, for the agent credentials zip. Earlier ones
    keep working until they expire (a year) or the worker is removed.

    raises:
        HTTPException: 409 if the PC has not joined yet.
    """
    await _worker(enroll, name)
    issued = await _audited(
        session,
        "worker_heartbeat_token",
        admin,
        name,
        lambda: enroll.issue_heartbeat_token(name),
    )
    return _valid(HeartbeatTokenOut, issued)


@router.get("/{name}/setup.zip")
async def setup_zip(
    name: WorkerName, admin: AdminUser, session: DbSession, enroll: Enroll
) -> Response:
    """
    The setup zip for install-worker.ps1 -Bundle: wg0.conf, the join token and
    the node agent's kubeconfig. The download is recorded before the zip is
    handed out.

    raises:
        HTTPException: 409 if the PC already joined (its keys stay on the hub;
            use the agent credentials), or the join token expired. 503 if the
            coordinator's address for node agents is unknown.
    """
    if not NODE_AGENT_COORDINATOR_URL:
        # Without it the PC's node agent would never send a heartbeat
        raise HTTPException(
            503,
            "The coordinator's address for node agents is unknown. Set "
            "NODE_AGENT_COORDINATOR_URL on the coordinator.",
        )
    await _worker(enroll, name)
    found = await _files(enroll, name)
    if found.joined:
        raise HTTPException(
            409, f"{name} has already joined. Download its agent credentials."
        )
    if _expired(found.join_token_expires_at):
        raise HTTPException(409, f"{name}'s join token has expired. Request a new one.")
    files = _pick(found, "wg_config", "join_token", "kubeconfig")
    if len(files) < 3:
        raise HTTPException(409, f"{name} is not fully set up. Add it again.")
    data = _zip(name, files, _SETUP_README.format(name=name))
    await _record(session, "worker_setup_downloaded", admin, name)
    return _download(data, f"{name}-setup.zip")


@router.get("/{name}/agent-credentials.zip")
async def agent_credentials_zip(
    name: WorkerName, admin: AdminUser, session: DbSession, enroll: Enroll
) -> Response:
    """
    The agent credentials for install-worker.ps1 -AgentCredentials: the
    heartbeat token and the node agent's kubeconfig. The download is recorded
    before the zip is handed out.

    raises:
        HTTPException: 409 if the PC has not joined yet, or has no heartbeat
            token (POST .../heartbeat-token first).
    """
    await _worker(enroll, name)
    found = await _files(enroll, name)
    if not found.joined:
        raise HTTPException(409, f"{name} has not joined yet.")
    files = _pick(found, "kubeconfig", "coordinator_token")
    if "coordinator_token" not in files:
        raise HTTPException(409, f"{name} has no heartbeat token yet. Issue one first.")
    data = _zip(name, files, _CREDENTIALS_README.format(name=name))
    await _record(session, "worker_credentials_downloaded", admin, name)
    return _download(data, f"{name}-agent-credentials.zip")


def _pick(found: HelperFiles, *keys: str) -> dict[str, str]:
    """The helper's files by the zip's keys, leaving out any it did not send."""
    return {
        key: found.files[ZIP_FILES[key]]
        for key in keys
        if ZIP_FILES[key] in found.files
    }


_SETUP_README = """\
IdleCloud setup files for {name}

They hold this PC's WireGuard private key and a join token: keep them private,
and delete this zip once the PC has joined.

On the PC, in Windows PowerShell from the repo, as the PC's normal user:

  powershell -ExecutionPolicy Bypass -File .\\agent\\install-worker.ps1 `
      -Bundle .\\{name}-setup.zip

Once the admin pages show {name} as joined, download its agent credentials and
run the installer again with -AgentCredentials. See agent/README.md.
"""

_CREDENTIALS_README = """\
IdleCloud agent credentials for {name}

They let this PC's node agent change its own node and send heartbeats: keep
them private, and delete this zip once installed.

On the PC, in Windows PowerShell from the repo:

  powershell -ExecutionPolicy Bypass -File .\\agent\\install-worker.ps1 `
      -AgentCredentials .\\{name}-agent-credentials.zip
"""
