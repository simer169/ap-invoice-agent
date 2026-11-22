"""Apply new SQL migrations from db/migrations/ and record them in agent.schema_migrations."""

import sys
from pathlib import Path

import psycopg

from ap_agent.config import get_database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"
_LOCK_KEY = 726_001  # arbitrary app-level advisory lock id

_BOOTSTRAP = """
CREATE SCHEMA IF NOT EXISTS agent;
CREATE TABLE IF NOT EXISTS agent.schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def migrate(conn: psycopg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Run migrations not yet recorded; each file commits with its own record. Returns names run."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute(_BOOTSTRAP)
    for path in sorted(directory.glob("*.sql")):
        with conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
            done = conn.execute(
                "SELECT 1 FROM agent.schema_migrations WHERE filename = %s", (path.name,)
            ).fetchone()
            if done:
                continue
            conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
            conn.execute(
                "INSERT INTO agent.schema_migrations (filename) VALUES (%s) "
                "ON CONFLICT (filename) DO NOTHING",
                (path.name,),
            )
            applied.append(path.name)
    return applied


def main() -> int:
    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            applied = migrate(conn)
    except psycopg.Error as exc:
        print(f"FAIL migration: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1
    if applied:
        for name in applied:
            print(f"applied {name}")
    else:
        print("nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
