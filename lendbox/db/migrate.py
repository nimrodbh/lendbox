"""Apply numbered SQL migrations in order, each once, each in its own transaction.

Plain SQL files instead of an ORM tool: the schema's guarantees (the exclusion constraint, the composite
foreign keys) are the point of the project, so they are written and reviewed as SQL.
Usage: python -m lendbox.db.migrate
"""

from __future__ import annotations

from importlib import resources

import psycopg

from .connection import connect

LOCK_ID = 7_310_001  # advisory lock: two app containers starting together never migrate twice


def migration_files() -> list[tuple[str, str]]:
    folder = resources.files("lendbox.db") / "migrations"
    names = sorted(p.name for p in folder.iterdir() if p.name.endswith(".sql"))
    return [(name, (folder / name).read_text(encoding="utf-8")) for name in names]


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations; return the names applied."""
    applied: list[str] = []
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for name, sql in migration_files():
            if name in done:
                continue
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
            applied.append(name)
    return applied


def main() -> None:
    with connect() as conn:
        applied = migrate(conn)
    print("applied:", ", ".join(applied) if applied else "nothing (up to date)")


if __name__ == "__main__":
    main()
