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


def pending_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/pending.json"


def pending_index_key(owner_id: str, job_id: UUID | str) -> str:
    # one empty object per upload that has no coordinator job yet, so "My jobs"
    # can list a user's uploads without scanning every job in the bucket
    return f"pending-index/{owner_id}/{job_id}"


def result_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/output/result.txt"


def replica_result_key(job_id: UUID, replica_id: int) -> str:
    # the same prefix the coordinator signs for each replica's result.json
    return f"jobs/{job_id}/replicas/{replica_id}/result.json"


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
