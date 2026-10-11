"""Signed POST to the web app's /hooks/pages-ready (member 5's endpoint).

Two kinds of body, both with a "status" field:
  ok      one batch of pages is in S3 and its tasks can start
  failed  the PDF cannot be processed, with a short reason to show the user
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request

from serverless.common.signing import signed_headers

log = logging.getLogger(__name__)

RETRIES = 3


def post(payload: dict) -> None:
    url = os.environ.get("WEBHOOK_URL", "")
    secret = os.environ.get("HMAC_SECRET", "")
    if not url:
        # Only allowed on purpose (local runs, or a deploy before the web app
        # exists). Otherwise a missing setting would silently drop every callback.
        if os.environ.get("ALLOW_NO_WEBHOOK") == "1":
            log.warning("WEBHOOK_URL not set, would send: %s", json.dumps(payload))
            return
        raise RuntimeError(
            "WEBHOOK_URL is not set. Set it, or set ALLOW_NO_WEBHOOK=1 to only log callbacks."
        )
    if not secret:
        raise RuntimeError("HMAC_SECRET must be set when WEBHOOK_URL is set")

    body = json.dumps(payload, separators=(",", ":")).encode()
    for attempt in range(1, RETRIES + 1):
        # Sign on every attempt so the timestamp stays fresh
        request = urllib.request.Request(
            url, data=body, method="POST", headers=signed_headers(secret, body)
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                log.info("callback ok for job %s", payload["job_id"])
                return
        except urllib.error.HTTPError as error:
            if error.code < 500:
                # 4xx means a bad signature or payload. Retrying will not fix it.
                raise RuntimeError(f"callback rejected with {error.code}") from error
            log.warning("callback attempt %d got %s", attempt, error.code)
        except (urllib.error.URLError, TimeoutError) as error:
            log.warning("callback attempt %d failed: %s", attempt, error)
        if attempt < RETRIES:
            time.sleep(2**attempt)
    # Raising lets Lambda retry the whole batch later. That is safe because
    # rendering overwrites the same S3 keys and the web app ignores repeats.
    raise RuntimeError(f"callback failed after {RETRIES} attempts")
