"""Rate limits counted in PostgreSQL, so every app instance shares one count and a restart forgets nothing.

`hit` records one event and says whether the caller is still within its budget; it checks and records in one
transaction under an advisory lock on (bucket, key), so two concurrent requests cannot both take the last slot.
Keys are hashed (an IP is never stored in the clear) with a salt from LENDBOX_HASH_SALT.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from lendbox.domain.errors import LendboxError

SALT = os.environ.get("LENDBOX_HASH_SALT", "lendbox-dev-salt").encode()


class RateLimited(LendboxError):
    """Too many actions of this kind in the window. `bucket` names the kind."""

    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        super().__init__(bucket)


@dataclass(frozen=True)
class Limit:
    bucket: str
    max_events: int
    window: timedelta


# Budgets from docs/threat-model.md section 5. Per IP for anonymous actions, per user once signed in.
SIGNUP_PER_IP = Limit("signup_ip", 5, timedelta(hours=1))
REQUESTS_PER_USER = Limit("request_user", 20, timedelta(hours=1))
MESSAGES_PER_USER = Limit("message_user", 30, timedelta(hours=1))
REPORTS_PER_USER = Limit("report_user", 10, timedelta(hours=1))
POSTS_PER_USER = Limit("looking_for_user", 5, timedelta(days=1))
DEMO_LOGIN_PER_IP = Limit("demo_ip", 30, timedelta(hours=1))


def key_hash(value: str | int) -> bytes:
    return hashlib.sha256(SALT + str(value).encode()).digest()


def hit(conn: psycopg.Connection, limit: Limit, key: str | int, *, now: datetime) -> None:
    """Record one event for `key`; raise RateLimited (recording nothing) if the budget is already spent."""
    h = key_hash(key)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s || encode(%s, 'hex'), 0))", (limit.bucket, h))
        conn.execute("DELETE FROM rate_events WHERE bucket = %s AND at < %s", (limit.bucket, now - limit.window))
        used = conn.execute(
            "SELECT count(*) AS n FROM rate_events WHERE bucket = %s AND key = %s AND at >= %s",
            (limit.bucket, h, now - limit.window),
        ).fetchone()["n"]
        if used >= limit.max_events:
            raise RateLimited(limit.bucket)
        conn.execute("INSERT INTO rate_events (bucket, key, at) VALUES (%s, %s, %s)", (limit.bucket, h, now))
