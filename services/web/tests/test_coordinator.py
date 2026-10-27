from datetime import date
from uuid import uuid4

import httpx
import pytest

from services.web.coordinator import CoordinatorClient


def make_client(handler) -> CoordinatorClient:
    return CoordinatorClient(
        "http://coordinator", 5, transport=httpx.MockTransport(handler)
    )


@pytest.mark.asyncio
async def test_login_posts_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/auth/login"
        assert request.read() == b'{"email":"a@b.c","password":"secret123"}'
        return httpx.Response(200, json={"access_token": "t"})

    client = make_client(handler)
    assert (await client.login("a@b.c", "secret123"))["access_token"] == "t"


@pytest.mark.asyncio
async def test_me_sends_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer abc"
        return httpx.Response(200, json={"email": "a@b.c"})

    assert (await make_client(handler).me("abc"))["email"] == "a@b.c"


@pytest.mark.asyncio
async def test_error_status_raises():
    client = make_client(lambda request: httpx.Response(401))
    with pytest.raises(httpx.HTTPStatusError):
        await client.me("bad")


@pytest.mark.asyncio
async def test_get_job_uses_uuid_in_path():
    job_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/jobs/{job_id}"
        return httpx.Response(200, json={"id": str(job_id)})

    assert (await make_client(handler).get_job("t", job_id))["id"] == str(job_id)


@pytest.mark.asyncio
async def test_admin_events_sends_only_given_filters():
    job_id = uuid4()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/events"
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = make_client(handler)
    await client.admin_events("t", limit=51, offset=0)
    await client.admin_events("t", job_id, "pc 1", 51, 50)
    assert seen == [
        {"limit": "51", "offset": "0"},
        {"limit": "51", "offset": "50", "job_id": str(job_id), "node_name": "pc 1"},
    ]


@pytest.mark.asyncio
async def test_list_jobs_repeats_status_and_omits_it_when_empty():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get_list("status"))
        return httpx.Response(200, json=[])

    client = make_client(handler)
    await client.list_jobs("t", 21, 0, ["RUNNING", "FAILED"])
    await client.list_jobs("t", 21, 0)
    assert seen == [["RUNNING", "FAILED"], []]


@pytest.mark.asyncio
async def test_list_jobs_sends_dates_only_when_given():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    client = make_client(handler)
    await client.list_jobs("t", 21, 0, [], date(2026, 10, 20), date(2026, 10, 25))
    await client.list_jobs("t", 21, 0)
    assert seen[0]["created_from"] == "2026-10-20"
    assert seen[0]["created_to"] == "2026-10-25"
    assert "created_from" not in seen[1] and "created_to" not in seen[1]
