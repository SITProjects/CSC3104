"""
This module allows the worker pods to save checkpoints to S3 when they receive a termination signal.
"""

import json
import sys
from signal import signal
from typing import NoReturn
from urllib import error, request

VERSION = 1
SAVE_DEADLINE_SECONDS = 20
EXIT_EVICTED = 143
LOAD_DEADLINE_SECONDS = 60


class StopFlag:
    def __init__(self) -> None:
        self.stop = False
        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGINT, self._handle_sigterm)

    def _handle_sigterm(self, signum: int, frame) -> None:
        self.stop = True


def _send(req: request.Request, deadline_seconds: float) -> bytes:
    """
    Sends the given request and raises an exception if it fails.
    """
    try:
        with request.urlopen(req, timeout=deadline_seconds) as response:
            return response.read()
    except error.HTTPError as e:
        if e.code == 403:
            raise RuntimeError(
                "Failed to send request: the pre-signed URL has expired"
            ) from e
        raise


def load(
    manifest: dict, deadline_seconds: float = LOAD_DEADLINE_SECONDS
) -> dict | None:
    url = manifest.get("checkpoint_get_url")
    if not url:
        return None
    try:
        body = _send(request.Request(url, method="GET"), deadline_seconds)
    except error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    try:
        envelop = json.loads(body)
    except ValueError:
        return None
    if (
        not isinstance(envelop, dict)
        or envelop.get("v") != VERSION
        or envelop.get("task_index") != manifest["task_index"]
        or envelop.get("attempt") != manifest["attempt"]
        or not isinstance(envelop.get("state"), dict)
    ):
        return None
    return envelop["state"]


def save(
    manifest: dict, state: dict, deadline_seconds: float = SAVE_DEADLINE_SECONDS
) -> None:
    if not manifest.get("checkpoint_put_url"):
        raise ValueError("checkpoint_put_url is None, cannot save checkpoint")

    envelop = {
        "v": VERSION,
        "task_index": manifest["task_index"],
        "attempt": manifest["attempt"],
        "state": state,
    }
    body = json.dumps(envelop, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        manifest["checkpoint_put_url"],
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    _send(req, deadline_seconds)


def _report_evicted(coordinator_url: str, replica_id: int, token: str) -> None:
    req = request.Request(
        f"{coordinator_url.rstrip('/')}/internal/replicas/{replica_id}/evicted",
        method="POST",
        data=b"",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        _send(req, 5)
    except Exception as e:  # noqa: BLE001
        print(f"Failed to report eviction: {e}")


def evict(manifest: dict, state: dict, coordinator_url: str, token: str) -> NoReturn:
    """
    Saves the checkpoint and notifies the coordinator that the replica has been evicted.

    args:
        manifest: The manifest for the replica.
        state: The state to save in the checkpoint.
        coordinator_url: The URL of the coordinator to notify.
        token: The REPLICA_TOKEN to use for authentication.
    """
    try:
        save(manifest, state)
    except Exception as e:  # noqa: BLE001
        print(f"Failed to save checkpoint: {e}")
    _report_evicted(coordinator_url, manifest["replica_id"], token)
    sys.exit(EXIT_EVICTED)
