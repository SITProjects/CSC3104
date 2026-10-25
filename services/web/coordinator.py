"""The web app's only way of talking to the coordinator service."""

from uuid import UUID

import httpx

from services.web.settings import settings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class CoordinatorClient:
    """
    Thin wrapper over the coordinator's public endpoints.

    Every method returns the decoded JSON and raises httpx.HTTPStatusError on a
    non-2xx response, so callers decide how to show the error.
    """

    def __init__(self, base_url: str, timeout: float, transport=None):
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=transport
        )

    async def _send(self, method: str, path: str, token: str | None = None, **kw):
        headers = _bearer(token) if token else None
        resp = await self._http.request(method, path, headers=headers, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    async def signup(self, email: str, password: str) -> dict:
        return await self._send(
            "POST", "/auth/signup", json={"email": email, "password": password}
        )

    async def login(self, email: str, password: str) -> dict:
        return await self._send(
            "POST", "/auth/login", json={"email": email, "password": password}
        )

    async def logout(self, token: str) -> None:
        await self._send("POST", "/auth/logout", token)

    async def me(self, token: str) -> dict:
        return await self._send("GET", "/auth/me", token)

    async def job_quota(self, token: str) -> dict:
        """{"active": unfinished jobs, "limit": how many are allowed at once}"""
        return await self._send("GET", "/jobs/quota", token)

    async def list_jobs(self, token: str, limit: int = 20, offset: int = 0) -> list:
        params = {"limit": limit, "offset": offset}
        return await self._send("GET", "/jobs", token, params=params)

    async def get_job(self, token: str, job_id: UUID) -> dict:
        # typed as UUID so a value like "../admin/jobs" can't change the path
        return await self._send("GET", f"/jobs/{job_id}", token)

    async def create_job(self, token: str, spec: dict) -> dict:
        return await self._send("POST", "/jobs", token, json=spec)

    async def cancel_job(self, token: str, job_id: UUID) -> None:
        await self._send("POST", f"/jobs/{job_id}/cancel", token)

    async def admin_nodes(self, token: str) -> list:
        return await self._send("GET", "/admin/nodes", token)

    async def admin_jobs(
        self,
        token: str,
        limit: int = 20,
        offset: int = 0,
        owner_id: UUID | None = None,
    ) -> list:
        params = {"limit": limit, "offset": offset}
        if owner_id is not None:
            params["owner_id"] = str(owner_id)
        return await self._send("GET", "/admin/jobs", token, params=params)

    async def admin_job_counts(self, token: str) -> dict:
        return await self._send("GET", "/admin/job-counts", token)

    async def admin_users(self, token: str, limit: int = 50, offset: int = 0) -> list:
        params = {"limit": limit, "offset": offset}
        return await self._send("GET", "/admin/users", token, params=params)

    async def admin_user(self, token: str, user_id: UUID) -> dict:
        # typed as UUID so the value can't change the path
        return await self._send("GET", f"/admin/users/{user_id}", token)

    async def admin_events(
        self,
        token: str,
        job_id: UUID | None = None,
        node_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list:
        params = {"limit": limit, "offset": offset}
        if job_id is not None:
            params["job_id"] = str(job_id)
        if node_name:
            params["node_name"] = node_name
        return await self._send("GET", "/admin/events", token, params=params)

    async def burst_overview(
        self,
        token: str,
        machine_limit: int = 20,
        machine_offset: int = 0,
        job_limit: int = 20,
        job_offset: int = 0,
    ) -> dict:
        return await self._send(
            "GET",
            "/admin/burst",
            token,
            params={
                "machine_limit": machine_limit,
                "machine_offset": machine_offset,
                "job_limit": job_limit,
                "job_offset": job_offset,
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()


coordinator = CoordinatorClient(settings.coordinator_url, settings.coordinator_timeout)
