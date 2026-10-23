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
