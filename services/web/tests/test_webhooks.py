import httpx
import pytest

from serverless.common.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify
from services.web import webhooks
from services.web.jobs import UserError
from services.web.webhooks import (
    check_webhook_url,
    deliver,
    webhook_payload,
    webhook_secret,
)


@pytest.mark.parametrize("url", ["http://example.com:99999/hook", "http://[abc/hook"])
def test_webhook_url_with_a_bad_port_or_host_is_a_user_error_not_a_crash(url):
    with pytest.raises(UserError) as error:
        check_webhook_url(url)
    assert error.value.status == 400


def test_webhook_secret_is_per_owner_and_never_the_server_secret(monkeypatch):
    monkeypatch.setattr(webhooks, "HMAC_SECRET", "server-secret")
    alice = webhook_secret("alice@x.co")
    assert alice == webhook_secret("alice@x.co")
    assert alice != webhook_secret("bob@x.co")
    assert "server-secret" not in alice


def test_webhook_secret_is_empty_without_a_server_secret(monkeypatch):
    monkeypatch.setattr(webhooks, "HMAC_SECRET", "")
    assert webhook_secret("alice@x.co") == ""


def test_webhook_payload_links_only_when_public_url_is_set(monkeypatch):
    job_id = "6f0c1e5e-0000-4000-8000-000000000001"
    monkeypatch.setattr(webhooks, "PUBLIC_URL", "")
    assert webhook_payload(job_id, "COMPLETED") == {
        "job_id": job_id,
        "status": "COMPLETED",
    }
    monkeypatch.setattr(webhooks, "PUBLIC_URL", "https://ic.example")
    payload = webhook_payload(job_id, "COMPLETED")
    assert payload["status_url"] == f"https://ic.example/jobs/{job_id}"
    assert payload["result_url"] == f"https://ic.example/api/jobs/{job_id}/result"
    assert "result_url" not in webhook_payload(job_id, "FAILED")


def client_with(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def skip_dns(monkeypatch):
    monkeypatch.setattr(webhooks, "check_webhook_url", lambda url: url)


@pytest.mark.asyncio
async def test_deliver_signs_the_body():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async with client_with(handler) as http:
        ok = await deliver(
            http, "https://x.co/hook", {"job_id": "j", "status": "COMPLETED"}, "s3cret"
        )
    assert ok
    request = seen[0]
    assert verify(
        "s3cret",
        request.content,
        request.headers[TIMESTAMP_HEADER],
        request.headers[SIGNATURE_HEADER],
    )
    assert request.headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_deliver_without_a_secret_sends_unsigned():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with client_with(handler) as http:
        assert await deliver(http, "https://x.co/hook", {"job_id": "j"}, "")
    assert SIGNATURE_HEADER not in seen[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500])
async def test_deliver_reports_a_non_2xx_answer_as_failed(status):
    async with client_with(lambda request: httpx.Response(status)) as http:
        assert not await deliver(http, "https://x.co/hook", {"job_id": "j"}, "s")


@pytest.mark.asyncio
async def test_deliver_reports_a_network_error_as_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with client_with(handler) as http:
        assert not await deliver(http, "https://x.co/hook", {"job_id": "j"}, "s")


@pytest.mark.asyncio
async def test_deliver_does_not_post_to_a_url_that_became_private(monkeypatch):
    def refuse(url):
        raise UserError(400, "The webhook must point at a public address.")

    monkeypatch.setattr(webhooks, "check_webhook_url", refuse)
    posted = []
    async with client_with(
        lambda request: posted.append(request) or httpx.Response(200)
    ) as http:
        assert not await deliver(http, "https://x.co/hook", {"job_id": "j"}, "s")
    assert posted == []
