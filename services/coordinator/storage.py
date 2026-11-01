import boto3
from botocore.config import Config

from core.settings import S3_BUCKET, S3_PUBLIC_ENDPOINT_URL

_s3 = boto3.client(
    "s3",
    endpoint_url=S3_PUBLIC_ENDPOINT_URL or None,
    config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
)


def presign(op: str, key: str, ttl: int) -> str:
    return _s3.generate_presigned_url(
        ClientMethod=op,
        Params={"Bucket": S3_BUCKET, "Key": key},
        ExpiresIn=ttl,
    )


def delete_prefix(prefix: str) -> int:
    """Delete every key under a job prefix, raising on partial deletion failures."""
    deleted = 0
    paginator = _s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        keys = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            response = _s3.delete_objects(
                Bucket=S3_BUCKET, Delete={"Objects": batch, "Quiet": True}
            )
            if errors := response.get("Errors"):
                raise RuntimeError(f"Could not delete {errors[0]['Key']}")
            deleted += len(batch)
    return deleted
