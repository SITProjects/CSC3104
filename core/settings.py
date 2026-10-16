import os

MAX_OVERFLOW = 10
POOL_SIZE = 5
DEBUG_ECHO = False
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://localhost:8001")
SCHEDULER_INTERVAL = 5  # seconds
BATCH_SIZE = 10  # number of replicas to fetch from the database at a time
JOB_NAMESPACE = "idlecloud-jobs"
# MinIO address that presigned URLs are signed for; it must be reachable by the
# workers. Unset on AWS, so boto3 signs for real S3.
S3_PUBLIC_ENDPOINT_URL = os.environ.get("S3_PUBLIC_ENDPOINT_URL") or None
S3_BUCKET = os.environ.get("S3_BUCKET", "csc3104-local")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
RETRY_ATTEMPTS = 3
