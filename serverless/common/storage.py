"""S3 key layout from the shared contracts, plus an S3 client that also works with MinIO.

jobs/{job_id}/input.pdf              uploaded by the web app, triggers the splitter
jobs/{job_id}/pages/{page:04d}.png   written by the renderer, 1-based page numbers
"""

import os
import re
from urllib.parse import unquote_plus

import boto3

_INPUT_KEY = re.compile(r"^jobs/([A-Za-z0-9_-]+)/input\.pdf$")


def s3_client():
    # S3_ENDPOINT_URL is only set when running against MinIO in the dev container
    return boto3.client("s3", endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None)


def input_key(job_id: str) -> str:
    return f"jobs/{job_id}/input.pdf"


def page_key(job_id: str, page: int) -> str:
    return f"jobs/{job_id}/pages/{page:04d}.png"


def job_id_from_key(raw_key: str) -> str | None:
    """Return the job id, or None if the key is not a job's input.pdf.

    S3 events URL-encode the key, so decode it before matching.
    """
    match = _INPUT_KEY.match(unquote_plus(raw_key))
    return match.group(1) if match else None


def read_object(s3, bucket: str, key: str) -> bytes:
    """Download an object into memory. Input PDFs are at most 50 MB."""
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
