import io
import json
import urllib.error

import pytest

from serverless.common import callback
from serverless.common.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, verify

PAYLOAD = {"status": "ok", "job_id": "abc", "page_start": 1}


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def urlopen(monkeypatch):
    """Replace the network. Set .replies to a list of 'ok' or an HTTP status."""
    calls = []

    def fake(request, timeout):
        calls.append(request)
        reply = fake.replies.pop(0)
        if reply == "ok":
            return Response()
        raise urllib.error.HTTPError(request.full_url, reply, "err", {}, io.BytesIO())

    fake.replies = []
    fake.calls = calls
    monkeypatch.setattr(callback.urllib.request, "urlopen", fake)
    monkeypatch.setattr(callback.time, "sleep", lambda s: None)
    monkeypatch.setenv("WEBHOOK_URL", "http://web/hooks/pages-ready")
    monkeypatch.setenv("HMAC_SECRET", "s3cret")
    return fake


def test_sends_signed_json(urlopen):
    urlopen.replies = ["ok"]
    callback.post(PAYLOAD)
    request = urlopen.calls[0]
    assert json.loads(request.data) == PAYLOAD
    headers = {k.title(): v for k, v in request.header_items()}
    assert verify(
        "s3cret",
        request.data,
        headers[TIMESTAMP_HEADER.title()],
        headers[SIGNATURE_HEADER.title()],
    )


def test_retries_server_errors(urlopen):
    urlopen.replies = [503, "ok"]
    callback.post(PAYLOAD)
    assert len(urlopen.calls) == 2


def test_gives_up_after_three_tries(urlopen):
    urlopen.replies = [500, 500, 500]
    with pytest.raises(RuntimeError):
        callback.post(PAYLOAD)


def test_does_not_retry_rejections(urlopen):
    urlopen.replies = [401]
    with pytest.raises(RuntimeError, match="401"):
        callback.post(PAYLOAD)
    assert len(urlopen.calls) == 1


def test_missing_url_is_an_error(monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALLOW_NO_WEBHOOK", raising=False)
    with pytest.raises(RuntimeError, match="WEBHOOK_URL"):
        callback.post(PAYLOAD)


def test_missing_url_only_logs_when_allowed(monkeypatch):
    monkeypatch.delenv("WEBHOOK_URL", raising=False)
    monkeypatch.setenv("ALLOW_NO_WEBHOOK", "1")
    callback.post(PAYLOAD)  # must not raise
