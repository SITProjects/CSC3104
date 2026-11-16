import os
import socket

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
# A copy fails after this many failures that weren't owner evictions (a lost
# node, a launch error, a node without a working GPU). Evictions don't count.
RETRY_ATTEMPTS = 3
# Backstop for job types with no max_age_hours: a copy evicted this many times
# fails anyway, so it can't be rerun for ever
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "50"))
HOUSEKEEPING_INTERVAL = 60  # seconds
FILE_RETENTION_HOURS = int(os.environ.get("FILE_RETENTION_HOURS", "24"))
PLACEMENT_TIMEOUT = int(os.environ.get("PLACEMENT_TIMEOUT_S", "300"))  # seconds
# imagePullPolicy for task Pods. Unset: Always for a tag such as :main, so a node
# never keeps running an old build, and IfNotPresent for a pinned @sha256 digest.
# The local compose stack sets IfNotPresent to use the images it loads itself.
TASK_IMAGE_PULL_POLICY = os.environ.get("TASK_IMAGE_PULL_POLICY") or None
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
# The enrollment helper's socket on the hub (infra/enroll), mounted into the Pod
ENROLL_SOCKET = os.environ.get("ENROLL_SOCKET", "/run/idlecloud/enroll.sock")
# Where a worker's node agent sends heartbeats. It runs on the PC, not in a Pod,
# so it can't resolve cluster DNS: by default the coordinator Service's
# ClusterIP, which Kubernetes passes to this Pod as COORDINATOR_SERVICE_HOST.
NODE_AGENT_COORDINATOR_URL = os.environ.get("NODE_AGENT_COORDINATOR_URL") or (
    f"http://{os.environ['COORDINATOR_SERVICE_HOST']}:"
    f"{os.environ.get('COORDINATOR_SERVICE_PORT', '8001')}"
    if os.environ.get("COORDINATOR_SERVICE_HOST")
    else None
)
# User logins: how long a bearer token from /auth/login lasts
SESSION_TTL_SECONDS = 24 * 3600
