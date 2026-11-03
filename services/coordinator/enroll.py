"""Client for the enrollment helper (infra/enroll) on the hub.

The helper is a root service that adds WireGuard peers and issues K3s join
tokens and node-agent credentials. It listens on a Unix socket that is mounted
into this Pod (coordinator.yml). Its errors become HTTP errors for the web app:
its 400, 404 and 409 pass through with its message, a failed command becomes
502, and a helper that can't be reached becomes 503.
"""

import logging
from typing import Any

import httpx
from fastapi import HTTPException

from core.settings import ENROLL_SOCKET

log = logging.getLogger("coordinator")

# Removing a worker drains its node first, which takes up to half a minute
TIMEOUT_S = 90
PASSED_THROUGH = {400, 404, 409}


class EnrollClient:
    def __init__(self, socket_path: str = ENROLL_SOCKET, transport=None):
        self.socket_path = socket_path
        self.transport = transport

    async def request(self, method: str, path: str, body: dict | None = None) -> Any:
        """The helper's JSON answer, or None for 204."""
        transport = self.transport or httpx.AsyncHTTPTransport(uds=self.socket_path)
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://enroll", timeout=TIMEOUT_S
            ) as client:
                response = await client.request(method, path, json=body)
        except httpx.HTTPError as error:
            log.warning("enrollment helper unreachable: %r", error)
            raise HTTPException(
                503, "The enrollment helper is not reachable."
            ) from None
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.is_success:
            return payload
        detail = payload.get("detail") if isinstance(payload, dict) else None
        if response.status_code in PASSED_THROUGH:
            raise HTTPException(response.status_code, detail or "Rejected.")
        log.warning("enrollment helper %s %s: %s", method, path, detail)
        raise HTTPException(502, detail or "The enrollment helper failed.")

    async def peer(self, name: str) -> dict:
        return await self.request("GET", f"/peers/{name}")

    async def peers(self) -> list[dict]:
        return await self.request("GET", "/peers")

    async def add_worker(self, name: str) -> dict:
        return await self.request("POST", "/peers", {"name": name, "type": "worker"})

    async def issue_join_token(self, name: str) -> dict:
        return await self.request("POST", f"/peers/{name}/join-token")

    async def make_identity(self, name: str) -> dict:
        return await self.request("POST", f"/peers/{name}/identity")

    async def issue_heartbeat_token(self, name: str) -> dict:
        return await self.request("POST", f"/peers/{name}/heartbeat-token")

    async def files(self, name: str) -> dict:
        return await self.request("GET", f"/peers/{name}/files")

    async def remove(self, name: str) -> None:
        await self.request("DELETE", f"/peers/{name}")


def get_enroll_client() -> EnrollClient:
    return EnrollClient()
