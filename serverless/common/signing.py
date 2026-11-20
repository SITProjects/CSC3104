"""HMAC signing for the pages-ready callback.

Signature = hex(HMAC-SHA256(secret, f"{timestamp}." + body)).
Headers: X-Idlecloud-Timestamp and X-Idlecloud-Signature.
The timestamp stops an old request from being replayed later.
The web app's /hooks/pages-ready endpoint checks requests the same way.
"""

import hashlib
import hmac
import time

TIMESTAMP_HEADER = "X-Idlecloud-Timestamp"
SIGNATURE_HEADER = "X-Idlecloud-Signature"
MAX_AGE_SECONDS = 300


def sign(secret: str, body: bytes, timestamp: int) -> str:
    message = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(secret: str, body: bytes, now: int | None = None) -> dict[str, str]:
    timestamp = int(now if now is not None else time.time())
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: str(timestamp),
        SIGNATURE_HEADER: sign(secret, body, timestamp),
    }


def verify(
    secret: str,
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    now: int | None = None,
    max_age: int = MAX_AGE_SECONDS,
) -> bool:
    if not timestamp_header or not signature_header:
        return False
    try:
        timestamp = int(timestamp_header)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - timestamp) > max_age:
        return False
    return hmac.compare_digest(sign(secret, body, timestamp), signature_header)
