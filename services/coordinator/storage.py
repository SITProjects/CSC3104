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
