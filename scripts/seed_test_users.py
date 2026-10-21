"""
Creates a test user and a test admin in the dev database. Not a migration, so
it never runs on a deployed coordinator.

    uv run python scripts/seed_test_users.py

Run it in the dev container: it uses DATABASE_URL and refuses any host but the
compose "postgres" service, so a port-forward to a deployed database is refused.
Re-running resets both passwords and roles.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from services.coordinator.auth import _hash_password

DEV_HOST = "postgres"
ACCOUNTS = [
    ("user@example.com", "password123", "USER"),
    ("admin@example.com", "admin12345", "ADMIN"),
]


def main() -> None:
    url = os.environ["DATABASE_URL"]
    host = urlparse(url).hostname
    if host != DEV_HOST:
        sys.exit(f"refusing to seed public test accounts on {host!r}")
    with psycopg.connect(url) as conn:
        for email, password, role in ACCOUNTS:
            conn.execute(
                """INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s)
                   ON CONFLICT (email) DO UPDATE
                   SET password_hash = EXCLUDED.password_hash, role = EXCLUDED.role""",
                (email, _hash_password(password), role),
            )
            print(f"{email} / {password} ({role})")


if __name__ == "__main__":
    main()
