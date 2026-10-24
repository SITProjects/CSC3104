from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Web app settings, read from environment variables (e.g. COORDINATOR_URL)."""

    coordinator_url: str = "http://localhost:8001"
    coordinator_timeout: float = 10.0  # seconds
    session_cookie: str = "session"  # holds the bearer token
    cookie_secure: bool = False  # set COOKIE_SECURE=true when served over HTTPS


settings = Settings()
