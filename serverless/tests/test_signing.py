from serverless.common.signing import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    signed_headers,
    verify,
)

SECRET = "test-secret"
BODY = b'{"job_id":"abc","page_start":1}'


def headers_at(now):
    headers = signed_headers(SECRET, BODY, now=now)
    return headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER]


def test_round_trip():
    assert verify(SECRET, BODY, *headers_at(1_000_000), now=1_000_010)


def test_tampered_body_fails():
    assert not verify(SECRET, BODY + b" ", *headers_at(1_000_000), now=1_000_000)


def test_wrong_secret_fails():
    assert not verify("other", BODY, *headers_at(1_000_000), now=1_000_000)


def test_old_request_is_rejected():
    assert not verify(SECRET, BODY, *headers_at(1_000_000), now=1_000_301)


def test_missing_headers_fail():
    assert not verify(SECRET, BODY, None, None)
