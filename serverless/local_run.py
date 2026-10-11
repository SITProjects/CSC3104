"""Run the whole Step 1 flow against the local MinIO, like the S3 trigger would.

It uploads the PDF to jobs/{job_id}/input.pdf, then runs the splitter. The
renderers run one after another in this process instead of as real Lambdas.

Run from the repo root:
    python -m serverless.local_run path/to/file.pdf
    python -m serverless.local_run path/to/file.pdf --callback   (with dev_receiver running)
"""

import argparse
import json
import logging
import os
import time
import uuid

from serverless.local_minio import ensure_bucket, use_local_minio

BUCKET = use_local_minio()  # must run before the S3 client is made

logging.basicConfig(level=logging.INFO, format="  %(message)s")
for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

from serverless.common.storage import input_key, s3_client
from serverless.splitter.handler import split_handler


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pdf", help="path to a PDF file")
    parser.add_argument(
        "--callback",
        action="store_true",
        help="send signed callbacks to dev_receiver on localhost:8099",
    )
    args = parser.parse_args()
    if args.callback:
        os.environ["WEBHOOK_URL"] = "http://localhost:8099/hooks/pages-ready"
        os.environ.setdefault("HMAC_SECRET", "local-test-secret")

    s3 = s3_client()
    ensure_bucket(s3, BUCKET)
    job_id = uuid.uuid4().hex[:12]
    s3.upload_file(args.pdf, BUCKET, input_key(job_id))
    print(f"Uploaded {args.pdf} to {input_key(job_id)}")

    # Same shape as a real S3 event, trimmed to the fields the splitter reads
    record = {"s3": {"bucket": {"name": BUCKET}, "object": {"key": input_key(job_id)}}}
    started = time.monotonic()
    print("Running the splitter...")
    print(json.dumps(split_handler({"Records": [record]}), indent=2))

    pages = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"jobs/{job_id}/pages/")
    print(f"{pages.get('KeyCount', 0)} page images in jobs/{job_id}/pages/")
    print(f"Total time {time.monotonic() - started:.1f} s")
    print(f"Browse them at http://localhost:9001 > {BUCKET} > jobs > {job_id} > pages")


if __name__ == "__main__":
    main()
