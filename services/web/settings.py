from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Web app settings, read from environment variables (e.g. COORDINATOR_URL)."""

    coordinator_url: str = "http://localhost:8001"
    coordinator_timeout: float = 10.0  # seconds
    session_cookie: str = "session"  # holds the bearer token
    cookie_secure: bool = False  # set COOKIE_SECURE=true when served over HTTPS

    # S3 (MinIO locally). The names match the coordinator's, so one .env serves both.
    s3_endpoint_url: str | None = None  # unset on AWS
    s3_bucket: str = "csc3104-local"

    # Uploads and how a PDF becomes tasks
    max_upload_mb: int = 50
    max_pages: int = 500  # the Lambda splitter refuses more than this too
    pages_per_task: int = 10
    replicas_per_task: int = 3
    # Only for uploads saved before users chose CPU and memory themselves
    task_cpu: int = 1
    task_mem_mib: int = 512

    # An upload whose pages aren't ready after this long is marked failed and its
    # stored session token is deleted
    pending_ttl_minutes: int = 30
    # The Lambda's callback is a few hundred bytes; anything bigger is refused unread
    callback_max_bytes: int = 16 * 1024
    max_filename_chars: int = 200

    # Signs the Lambda's /hooks/pages-ready callback. Empty disables the hook.
    hmac_secret: str = ""
    # Render pages inside this app instead of waiting for the Lambda (local dev only)
    local_render: bool = False

    # Progress page: how often the stream asks the coordinator for news
    stream_interval: float = 2.0  # seconds
    stream_keepalive: float = 15.0  # seconds


settings = Settings()
