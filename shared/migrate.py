from pathlib import Path
from typing import LiteralString, cast

import psycopg
from psycopg import sql

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOCK_ID = 3104


def run_migrations(database_url: str) -> list[str]:
    applied = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))

        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
            )
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in done:
                    continue
                with conn.transaction():  # file + bookkeeping row commit together
                    conn.execute(
                        sql.SQL(cast(LiteralString, path.read_text(encoding="utf-8")))
                    )
                    conn.execute(
                        "INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,)
                    )
                    applied.append(path.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))

        return applied
