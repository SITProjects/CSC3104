"""Point the S3 client at the local MinIO from .devcontainer/docker-compose.yml.

Works in two places:
- inside the dev container, where MinIO is at http://minio:9000
- on Windows directly, where MinIO is at http://localhost:9000

MinIO's root login (admin / password123) is used, because the AWS keys in the
dev container environment do not exist in MinIO.
"""

import os
import socket

from botocore.exceptions import ClientError, EndpointConnectionError

BUCKET = "csc3104-local"


def _default_endpoint() -> str:
    try:
        socket.gethostbyname("minio")
        return "http://minio:9000"  # inside the dev container
    except OSError:
        return "http://localhost:9000"  # on Windows, with MinIO's port published


def use_local_minio() -> str:
    """Set the environment for MinIO and return the bucket name. Call before s3_client()."""
    os.environ.setdefault("S3_ENDPOINT_URL", _default_endpoint())
    os.environ.setdefault("S3_BUCKET", BUCKET)
    os.environ["AWS_ACCESS_KEY_ID"] = os.environ.get("MINIO_USER", "admin")
    os.environ["AWS_SECRET_ACCESS_KEY"] = os.environ.get(
        "MINIO_PASSWORD", "password123"
    )
    os.environ.pop("AWS_SESSION_TOKEN", None)
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-1")
    os.environ.pop("RENDER_FUNCTION_NAME", None)  # render in-process, no real Lambda
    os.environ.setdefault(
        "ALLOW_NO_WEBHOOK", "1"
    )  # no web app locally, just log callbacks
    return os.environ["S3_BUCKET"]


def ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except EndpointConnectionError:
        raise SystemExit(
            f"Cannot reach MinIO at {os.environ['S3_ENDPOINT_URL']}. Start it first "
            "(open the dev container, or run: docker compose -f "
            ".devcontainer/docker-compose.yml up -d minio)"
        ) from None
    except ClientError:
        region = os.environ["AWS_DEFAULT_REGION"]
        s3.create_bucket(
            Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region}
        )
        print(f"Created bucket {bucket} in MinIO")
