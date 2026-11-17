"""The web app's S3 access: the uploaded PDF, upload bookkeeping and results.

boto3 is blocking, so the async callers run these functions in a thread.
"""

import json
from functools import lru_cache
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from services.web.settings import settings


@lru_cache
def _client():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url or None,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def input_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/input.pdf"


def page_key(job_id: UUID, page: int) -> str:
    return f"jobs/{job_id}/pages/{page:04d}.png"


def input_file_key(job_id: UUID, name: str) -> str:
    """A generic job's uploaded file, as the runner downloads it (task input files)."""
    return f"jobs/{job_id}/inputs/{name}"


# A scored job's evaluation file, under jobs/{id}/. Only the platform's checker
# reads it (worker/model-check); the training PC isn't given it.
EVAL_FILE = "eval/eval.npz"


def eval_file_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/{EVAL_FILE}"


def replica_keys(job_id: UUID, replica_id: int, attempt: int, name: str) -> list[str]:
    """
    Where a copy's result.json or output.tar.gz may be, newest layout first.

    The coordinator signs a key per attempt (attempts/{n}/), so an old attempt's
    late upload can't replace a newer one's. Jobs from before that wrote it
    straight under the replica.
    """
    prefix = f"jobs/{job_id}/replicas/{replica_id}"
    return [f"{prefix}/attempts/{attempt}/{name}", f"{prefix}/{name}"]


def output_archive_keys(job_id: UUID, replica_id: int, attempt: int) -> list[str]:
    """What a generic job's copy returns: its output folder and log (worker/runner)."""
    return replica_keys(job_id, replica_id, attempt, "output.tar.gz")


def pending_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/pending.json"


def pending_index_key(owner_id: str, job_id: UUID | str) -> str:
    # one empty object per upload that has no coordinator job yet, so "My jobs"
    # can list a user's uploads without scanning every job in the bucket
    return f"pending-index/{owner_id}/{job_id}"


def result_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/output/result.txt"


def replica_result_keys(job_id: UUID, replica_id: int, attempt: int) -> list[str]:
    # the keys the coordinator signs for each replica's result.json
    return replica_keys(job_id, replica_id, attempt, "result.json")


def put_bytes(key: str, body: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=settings.s3_bucket, Key=key, Body=body, ContentType=content_type
    )


def get_bytes(key: str) -> bytes | None:
    """Returns the object, or None if it doesn't exist."""
    try:
        return _client().get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def get_first(keys: list[str]) -> bytes | None:
    """The first of the objects that exists, or None if none does."""
    for key in keys:
        data = get_bytes(key)
        if data is not None:
            return data
    return None


def put_json(key: str, value: dict[str, Any]) -> None:
    put_bytes(key, json.dumps(value).encode(), "application/json")


def get_json(key: str) -> dict[str, Any] | None:
    raw = get_bytes(key)
    return json.loads(raw) if raw is not None else None


def count_pages(job_id: UUID) -> int:
    """How many rendered page images exist for the job so far."""
    paginator = _client().get_paginator("list_objects_v2")
    prefix = f"jobs/{job_id}/pages/"
    return sum(
        len(page.get("Contents", []))
        for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix)
    )


def page_numbers(job_id: UUID) -> set[int]:
    """The numbers of the page images rendered for the job so far."""
    paginator = _client().get_paginator("list_objects_v2")
    prefix = f"jobs/{job_id}/pages/"
    numbers = set()
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            name = item["Key"].removeprefix(prefix).removesuffix(".png")
            if name.isdigit():
                numbers.add(int(name))
    return numbers


def delete(key: str) -> None:
    _client().delete_object(Bucket=settings.s3_bucket, Key=key)


def list_pending(owner_id: str, limit: int = 20) -> list[str]:
    """The ids of this user's uploads that have no coordinator job yet, newest first."""
    paginator = _client().get_paginator("list_objects_v2")
    found = []
    for page in paginator.paginate(
        Bucket=settings.s3_bucket, Prefix=f"pending-index/{owner_id}/"
    ):
        for obj in page.get("Contents", []):
            found.append((obj["LastModified"], obj["Key"].rsplit("/", 1)[1]))
    found.sort(reverse=True)
    return [job_id for _, job_id in found[:limit]]
