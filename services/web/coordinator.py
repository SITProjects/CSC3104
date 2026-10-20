"""Client for the coordinator's internal API (shared contracts, port 8001)."""

from typing import Any
from uuid import UUID

import httpx

from shared.db import JobView


class CoordinatorError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"coordinator replied {status}: {detail}")
        self.status = status
        self.detail = detail


class CoordinatorClient:
    def __init__(self, base_url: str) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=10)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise CoordinatorError(503, f"coordinator unreachable: {error}") from error
        if response.is_error:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text
            raise CoordinatorError(response.status_code, detail)
        return response

    async def create_job(self, body: dict[str, Any]) -> dict[str, Any]:
        """POST /internal/jobs. body follows the coordinator's CreateJobRequest."""
        return (await self._call("POST", "/internal/jobs", json=body)).json()

    async def get_job(self, job_id: UUID) -> JobView | None:
        """GET /internal/jobs/{id}, or None if the coordinator does not know it."""
        try:
            response = await self._call("GET", f"/internal/jobs/{job_id}")
        except CoordinatorError as error:
            if error.status == 404:
                return None
            raise
        return JobView.model_validate(response.json())

    async def cancel_job(self, job_id: UUID) -> None:
        await self._call("POST", f"/internal/jobs/{job_id}/cancel")
