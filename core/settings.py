import os
import socket

MAX_OVERFLOW = 10
POOL_SIZE = 5
DEBUG_ECHO = False
COORDINATOR_URL = os.environ.get("COORDINATOR_URL", "http://localhost:8001")
SCHEDULER_INTERVAL = 5
BATCH_SIZE = 10  # number of replicas to fetch from the database at a time
JOB_NAMESPACE = "idlecloud-jobs"
# MinIO address that presigned URLs are signed for; it must be reachable by the
# workers. Unset on AWS, so boto3 signs for real S3.
S3_PUBLIC_ENDPOINT_URL = os.environ.get("S3_PUBLIC_ENDPOINT_URL") or None
S3_BUCKET = os.environ.get("S3_BUCKET", "csc3104-local")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
RETRY_ATTEMPTS = 3
HOUSEKEEPING_INTERVAL = 60  # seconds
# Node heartbeats: a projected service-account token for this audience, checked
# with a TokenReview. Only these service accounts may send heartbeats, and only
# for the node their Pod runs on, or the Node their token is bound to (lab PC
# node agents, see agent/node-agent/make-kubeconfig.sh).
HEARTBEAT_AUDIENCE = "idlecloud-coordinator"
HEARTBEAT_SERVICE_ACCOUNTS = frozenset(
    os.environ.get(
        "HEARTBEAT_SERVICE_ACCOUNTS",
        "system:serviceaccount:idlecloud-system:spot-watcher,"
        "system:serviceaccount:idlecloud-system:node-agent-heartbeat",
    ).split(",")
)
POD_NAME = os.environ.get("POD_NAME") or socket.gethostname()
LEASE_NAME = "coordinator-leader"
LEASE_NAMESPACE = os.environ.get("LEASE_NAMESPACE", "idlecloud-system")
LEASE_SECONDS = 15
RENEW_INTERVAL = 5  # seconds
RENEW_DEADLINE = 10  # seconds
# Jobs one user may have unfinished (PENDING or RUNNING) at once
MAX_ACTIVE_JOBS_PER_USER = int(os.environ.get("MAX_ACTIVE_JOBS_PER_USER", "3"))
# User logins: how long a bearer token from /auth/login lasts
SESSION_TTL_SECONDS = 24 * 3600
