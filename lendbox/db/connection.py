"""Database connections. One place reads the DSN, so the app, migrations and tests agree."""

from __future__ import annotations

import os

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = "postgresql://lendbox:lendbox-dev@localhost:5433/lendbox"


def dsn() -> str:
    return os.environ.get("LENDBOX_DATABASE_URL", DEFAULT_DSN)


def connect(conninfo: str | None = None) -> psycopg.Connection:
    """A new connection with dict rows. Callers own the transaction (`with conn.transaction():`)."""
    return psycopg.connect(conninfo or dsn(), row_factory=dict_row, autocommit=True)
