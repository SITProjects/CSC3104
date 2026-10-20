import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://admin:password123@localhost:5432/csc3104"
)
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://localhost:8001").rstrip("/")
S3_BUCKET = os.environ.get("S3_BUCKET", "csc3104-local")
# MinIO locally. Unset on AWS, so boto3 talks to real S3 with the instance role.
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None
# Shared with the Lambdas, signs /hooks/pages-ready
HMAC_SECRET = os.environ.get("HMAC_SECRET", "")
# HTTP Basic password for /admin (user "admin"). Empty turns the admin page off.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
# No Lambda locally: render pages in this process instead, the same way the
# renderer does. Also the "split in one pod" baseline for member 6's evaluation.
LOCAL_SPLIT = os.environ.get("LOCAL_SPLIT") == "1"
# The site's public address, for links in webhook payloads. Optional.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_RUNNING_JOBS = 3  # per user, counting uploads still being split
PAGES_PER_TASK = 10
REPLICAS_PER_TASK = 3
VOTES_TO_VERIFY = REPLICAS_PER_TASK // 2 + 1  # a majority of the replicas
JOB_TYPE = "ocr"

# Resource rule (member 5, step 3), from profiling sample PDFs with docker stats.
# Rendered page PNG size is the cheapest signal for "large or dense".
DENSE_PAGE_BYTES = 1_500_000  # average PNG size above this counts as dense
CPU_REQUEST = 1  # cores; the coordinator only accepts whole cores
MEM_NORMAL = 256  # MiB
MEM_DENSE = 512  # MiB

STREAM_INTERVAL = 2  # seconds between live status checks
STREAM_KEEPALIVE = 15  # seconds between SSE comments, so proxies keep the stream open
WEBHOOK_INTERVAL = 10  # seconds between checks for finished jobs to notify
WEBHOOK_TIMEOUT = 5  # seconds
WEBHOOK_MAX_ATTEMPTS = 5  # failed deliveries before giving up, WEBHOOK_INTERVAL apart
WEBHOOK_CONCURRENCY = 10  # webhooks in flight at once
