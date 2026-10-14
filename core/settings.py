import os

MAX_OVERFLOW = 10
POOL_SIZE = 5
DEBUG_ECHO = False
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://localhost:8001")
SCHEDULER_INTERVAL = 5  # seconds
BATCH_SIZE = 10  # number of replicas to fetch from the database at a time
JOB_NAMESPACE = "idlecloud-jobs"
S3_URL = os.environ.get("S3_URL", "http://localhost:9000/csc3104-local")
S3_PUBLIC_ENDPOINT_URL = os.environ.get(
    "S3_PUBLIC_ENDPOINT_URL", "http://localhost:19000"
)
S3_BUCKET = os.environ.get("S3_BUCKET", "csc3104-local")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
