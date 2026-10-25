"""Accounts: sign-up, email verification, login with throttling, sessions. All SQL for auth lives here."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

import psycopg
from psycopg import errors as pg_errors

from lendbox.domain.auth import DUMMY_HASH, hash_password, new_token, token_hash, validate_signup, verify_password
from lendbox.domain.errors import EmailTaken, InvalidCredentials, InvalidToken, TooManyAttempts

from .limits import key_hash

SESSION_TTL = timedelta(days=14)
VERIFY_TTL = timedelta(hours=48)
FAILURE_WINDOW = timedelta(minutes=15)
MAX_FAILURES = 5  # per (email, IP)
MAX_FAILURES_PER_IP = 20
MAX_FAILURES_PER_EMAIL = 50


def sign_up(conn: psycopg.Connection, *, email: str, display_name: str, password: str, now: datetime) -> int:
    """Create an unverified user and queue its confirmation email. Returns the user id.

    The outbox row says what to send, not the link: the token is minted by `deliver_outbox` at send time and
    only its hash is stored (threat model, threat 20).
    """
    email, display_name = validate_signup(email, display_name, password)
    password_hash = hash_password(password)
    try:
        with conn.transaction():
            user_id = conn.execute(
                "INSERT INTO users (email, display_name, password_hash, created_at) VALUES (%s, %s, %s, %s) "
                "RETURNING id",
                (email, display_name, password_hash, now),
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO email_outbox (to_email, kind, user_id, created_at) VALUES (%s, 'verify_email', %s, %s)",
                (email, user_id, now),
            )
    except pg_errors.UniqueViolation as exc:
        raise EmailTaken(email) from exc
    return user_id


def issue_verification(conn: psycopg.Connection, *, user_id: int, now: datetime) -> str:
    """A fresh single-use confirmation token for this user. Returns the raw value; stores only its hash."""
    raw, hashed = new_token()
    conn.execute(
        "INSERT INTO email_tokens (token_hash, user_id, purpose, created_at, expires_at) "
        "VALUES (%s, %s, 'verify_email', %s, %s)",
        (hashed, user_id, now, now + VERIFY_TTL),
    )
    return raw


Send = Callable[[str, str, str], None]  # (to, subject, body)


def deliver_outbox(conn: psycopg.Connection, *, send: Send, now: datetime, base_url: str, limit: int = 50) -> int:
    """Send pending mail. Each confirmation gets its token here, goes straight to `send`, and is never stored.

    A row is marked sent in the same transaction that minted its token; if `send` raises, both roll back and
    the row is retried next time. Returns how many were sent.
    """
    sent = 0
    rows = conn.execute(
        """
        SELECT o.id, o.to_email, o.kind, o.user_id, u.display_name
        FROM email_outbox o JOIN users u ON u.id = o.user_id
        WHERE o.sent_at IS NULL ORDER BY o.created_at LIMIT %s
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        with conn.transaction():
            raw = issue_verification(conn, user_id=row["user_id"], now=now)
            send(
                row["to_email"],
                "Confirm your Lendbox email",
                f"Hi {row['display_name']}, confirm your email to start borrowing and lending: "
                f"{base_url}/verify?token={raw}",
            )
            conn.execute("UPDATE email_outbox SET sent_at = %s WHERE id = %s", (now, row["id"]))
        sent += 1
    return sent


def verify_email(conn: psycopg.Connection, *, raw_token: str, now: datetime) -> int:
    """Mark the token used and the user verified, once. Returns the user id."""
    with conn.transaction():
        row = conn.execute(
            """
            UPDATE email_tokens SET used_at = %(now)s
            WHERE token_hash = %(h)s AND purpose = 'verify_email' AND used_at IS NULL AND expires_at > %(now)s
            RETURNING user_id
            """,
            {"h": token_hash(raw_token), "now": now},
        ).fetchone()
        if row is None:
            raise InvalidToken()
        conn.execute(
            "UPDATE users SET email_verified_at = coalesce(email_verified_at, %s) WHERE id = %s", (now, row["user_id"])
        )
    return row["user_id"]


def authenticate(conn: psycopg.Connection, *, email: str, password: str, now: datetime, ip: str = "") -> int:
    """Return the user id, or raise TooManyAttempts / InvalidCredentials. Failures are recorded and throttled.

    Three budgets per 15 minutes (threat model, threats 10 and 11):
    - per client IP, checked before any scrypt work: one machine cannot try thousands of emails;
    - per (email, IP): the strict limit a guesser at one account hits first;
    - per email from anywhere, set high: locking someone out takes many machines, not one script.
    """
    email = email.strip().lower()
    ip_hash = key_hash(ip)
    since = now - FAILURE_WINDOW
    with conn.transaction():
        conn.execute("DELETE FROM login_failures WHERE failed_at < %s", (since,))
        counts = conn.execute(
            """
            SELECT count(*) FILTER (WHERE ip_hash = %(ip)s) AS by_ip,
                   count(*) FILTER (WHERE ip_hash = %(ip)s AND email = %(email)s) AS by_pair,
                   count(*) FILTER (WHERE email = %(email)s) AS by_email
            FROM login_failures WHERE failed_at >= %(since)s
            """,
            {"ip": ip_hash, "email": email, "since": since},
        ).fetchone()
        if (
            counts["by_ip"] >= MAX_FAILURES_PER_IP
            or counts["by_pair"] >= MAX_FAILURES
            or counts["by_email"] >= MAX_FAILURES_PER_EMAIL
        ):
            raise TooManyAttempts()
        user = conn.execute(
            "SELECT id, password_hash FROM users WHERE email = %s AND hidden_at IS NULL", (email,)
        ).fetchone()
        ok = verify_password(password, user["password_hash"] if user else DUMMY_HASH)
        if user and ok:
            conn.execute("DELETE FROM login_failures WHERE email = %s AND ip_hash = %s", (email, ip_hash))
            return user["id"]
        conn.execute(
            "INSERT INTO login_failures (email, ip_hash, failed_at) VALUES (%s, %s, %s)", (email, ip_hash, now)
        )
    raise InvalidCredentials()


def create_session(conn: psycopg.Connection, *, user_id: int, now: datetime) -> str:
    raw, hashed = new_token()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
        (hashed, user_id, now, now + SESSION_TTL),
    )
    return raw


def session_user(conn: psycopg.Connection, *, raw_token: str, now: datetime) -> dict | None:
    """The signed-in user for a session cookie, or None if unknown, expired or hidden."""
    return conn.execute(
        """
        SELECT u.id, u.email, u.display_name, u.is_admin, u.email_verified_at IS NOT NULL AS verified
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.expires_at > %s AND u.hidden_at IS NULL
        """,
        (token_hash(raw_token), now),
    ).fetchone()


def end_session(conn: psycopg.Connection, *, raw_token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash(raw_token),))
