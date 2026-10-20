"""S3 access for the web app. Keys follow the shared contracts."""

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from services.web.settings import S3_BUCKET, S3_ENDPOINT_URL

_s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT_URL,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)


def input_key(job_id: str) -> str:
    return f"jobs/{job_id}/input.pdf"


def page_name(page: int) -> str:
    """Relative to jobs/{job_id}/, the form the coordinator's task input expects."""
    return f"pages/{page:04d}.png"


def result_key(job_id: str) -> str:
    return f"jobs/{job_id}/output/result.txt"


def put_object(key: str, body: bytes, content_type: str) -> None:
    _s3.put_object(Bucket=S3_BUCKET, Key=key, Body=body, ContentType=content_type)


def get_object(key: str) -> bytes | None:
    """Returns the object's bytes, or None if it does not exist."""
    try:
        return _s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def page_sizes(job_id: str) -> list[int]:
    """Sizes in bytes of the job's rendered page PNGs."""
    sizes = []
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=f"jobs/{job_id}/pages/"):
        sizes.extend(obj["Size"] for obj in page.get("Contents", []))
    return sizes
