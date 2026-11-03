"""The web app's only way of talking to the coordinator service."""

import re
from datetime import datetime
from uuid import UUID

import httpx

from services.web.settings import settings


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# A Kubernetes node name, as the coordinator requires for a campus worker
WORKER_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def _worker(name: str) -> str:
    """/admin/workers/<name>, checked so a value like "../users" can't change the path."""
    if not WORKER_NAME.fullmatch(name):
        raise ValueError(f"not a worker name: {name!r}")
    return f"/admin/workers/{name}"


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

    async def list_jobs(
        self,
        token: str,
        limit: int = 20,
        offset: int = 0,
        statuses: list[str] = (),
        created_after: datetime | None = None,
        created_before: datetime | None = None,
    ) -> list:
        """
        The user's jobs, newest first: only those in statuses if any are given,
        and only those created at or after created_after and before
        created_before, if given.
        """
        params = {"limit": limit, "offset": offset, "status": list(statuses)}
        for name, when in (
            ("created_after", created_after),
            ("created_before", created_before),
        ):
            if when is not None:
                params[name] = when.isoformat()
        return await self._send("GET", "/jobs", token, params=params)

    async def get_job(self, token: str, job_id: UUID) -> dict:
        # typed as UUID so a value like "../admin/jobs" can't change the path
        return await self._send("GET", f"/jobs/{job_id}", token)

    async def create_job(self, token: str, spec: dict) -> dict:
        return await self._send("POST", "/jobs", token, json=spec)

    async def add_tasks(self, token: str, job_id: UUID, tasks: list[dict]) -> dict:
        """More tasks for a job created with a larger task_count; repeats are skipped."""
        return await self._send(
            "POST", f"/jobs/{job_id}/tasks", token, json={"tasks": tasks}
        )

    async def capacity(self, token: str, cpu: int, mem: int) -> dict:
        """{"slots": copies of this size that fit now, "nodes": usable workers}"""
        return await self._send(
            "GET", "/capacity", token, params={"cpu": cpu, "mem": mem}
        )

    async def cancel_job(self, token: str, job_id: UUID) -> None:
        await self._send("POST", f"/jobs/{job_id}/cancel", token)

    async def delete_files(self, token: str, job_id: UUID) -> None:
        await self._send("POST", f"/jobs/{job_id}/delete-files", token)

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

    async def admin_workers(self, token: str) -> list:
        return await self._send("GET", "/admin/workers", token)

    async def admin_worker(self, token: str, name: str) -> dict:
        return await self._send("GET", _worker(name), token)

    async def add_worker(self, token: str, name: str) -> dict:
        return await self._send("POST", "/admin/workers", token, json={"name": name})

    async def remove_worker(self, token: str, name: str) -> None:
        await self._send("DELETE", _worker(name), token)

    async def new_join_token(self, token: str, name: str) -> dict:
        return await self._send("POST", f"{_worker(name)}/join-token", token)

    async def new_heartbeat_token(self, token: str, name: str) -> dict:
        return await self._send("POST", f"{_worker(name)}/heartbeat-token", token)

    async def worker_zip(self, token: str, name: str, which: str) -> bytes:
        """The worker's "setup" or "agent-credentials" zip, as bytes."""
        if which not in ("setup", "agent-credentials"):
            raise ValueError(f"no such zip: {which}")
        resp = await self._http.get(
            f"{_worker(name)}/{which}.zip", headers=_bearer(token)
        )
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        await self._http.aclose()


coordinator = CoordinatorClient(settings.coordinator_url, settings.coordinator_timeout)
