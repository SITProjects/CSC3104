"""
OCR worker: runs one replica of an OCR task inside its Pod.

Fetches the replica manifest, resumes from the previous attempt's checkpoint,
OCRs each input page with Tesseract, saves a checkpoint and reports progress
after every page, then uploads result.json and reports its hash. On SIGTERM it
stops between pages and calls checkpoint.evict, which saves, reports /evicted
and exits with 143 so the coordinator can rerun it elsewhere.
"""

import json
import os
from io import BytesIO
from typing import Any
from urllib import request

import pytesseract
from dotenv import load_dotenv
from PIL import Image

from shared import checkpoint
from shared.normalized_text import canonical_json, normalize_text, result_hash

REQUEST_TIMEOUT_SECONDS = 20
# per page; keep well under the Pod's 30 s grace period minus the final save
OCR_TIMEOUT_SECONDS = 15


def read_environment() -> dict[str, str]:
    """
    Reads the variables the coordinator sets on the Pod.

    returns:
        dict[str, str]: replica_id, replica_token, coordinator_url and task_index.
    raises:
        RuntimeError: If any of them is missing.
    """
    config = {
        "replica_id": os.environ.get("REPLICA_ID", ""),
        "replica_token": os.environ.get("REPLICA_TOKEN", ""),
        "coordinator_url": os.environ.get("COORDINATOR_URL", "").rstrip("/"),
        "task_index": os.environ.get("TASK_INDEX", ""),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {missing}")
    return config


def call_coordinator(
    config: dict[str, str], method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """
    Calls the coordinator's internal API for this replica.

    args:
        config: From read_environment.
        method: "GET" or "POST".
        path: The part after /internal/replicas/{id}/, e.g. "manifest".
        body: JSON body to send, if any.
    returns:
        dict: The JSON response.
    raises:
        urllib.error.HTTPError: On a non-2xx reply, e.g. 401 for a wrong token.
    """
    headers = {"Authorization": f"Bearer {config['replica_token']}"}
    data = None
    if body is not None:
        # without it urllib sends form encoding and FastAPI answers 422
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = request.Request(
        f"{config['coordinator_url']}/internal/replicas/{config['replica_id']}/{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read())


def download(url: str) -> bytes:
    """
    Downloads one input file through its presigned URL.

    args:
        url: A presigned GET URL from manifest["input_urls"].
    returns:
        bytes: The file's contents.
    """
    with request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read()


def ocr_page(image: bytes) -> str:
    """
    Reads the text on one page image with Tesseract.

    args:
        image: The page as PNG (or any format Pillow reads).
    returns:
        str: Tesseract's raw text, before normalization.
    """
    with Image.open(BytesIO(image)) as img:
        return pytesseract.image_to_string(
            img.convert("L"), lang="eng", timeout=OCR_TIMEOUT_SECONDS
        )


def upload_result(manifest: dict[str, Any], body: bytes) -> None:
    """
    Uploads result.json through the presigned PUT URL.

    args:
        manifest: The replica manifest, for output_upload_url.
        body: The exact bytes from canonical_json, which are also hashed.
    """
    req = request.Request(
        manifest["output_upload_url"],
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    # S3 and MinIO answer a PUT with an empty body, so only the status matters
    with request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS):
        pass


def main() -> None:
    load_dotenv(".env.worker")  # local runs only; in a Pod the env is already set
    stop = checkpoint.StopFlag()
    config = read_environment()
    manifest = call_coordinator(config, "GET", "manifest")

    # input_urls is keyed by the task's input.files, e.g. "pages/0001.png";
    # zero-padded names sort into page order
    names = sorted(manifest["input_urls"])
    state = checkpoint.load(manifest) or {"next": 0, "pages": {}}

    for name in names[state["next"] :]:
        if stop.stop:
            checkpoint.evict(
                manifest, state, config["coordinator_url"], config["replica_token"]
            )
        text = ocr_page(download(manifest["input_urls"][name]))
        state["pages"][name] = normalize_text(text)
        state["next"] += 1
        checkpoint.save(manifest, state)
        # 100 is set by the coordinator when /result arrives
        progress = min(99, state["next"] * 100 // len(names))
        call_coordinator(config, "POST", "progress", {"progress": progress})

    result = {"pages": state["pages"]}
    upload_result(manifest, canonical_json(result))
    call_coordinator(
        config, "POST", "result", {"result_hash": result_hash(result), "exit_code": 0}
    )


if __name__ == "__main__":
    main()
