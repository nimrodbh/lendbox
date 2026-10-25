"""Public demo mode (LENDBOX_DEMO=1): visitors try the site as a ready-made member, sign-up is off, and the demo
world reloads every day, so nothing a visitor does lasts and no visitor ever types personal data.

The demo accounts are chosen from the data, not hard-coded, so they keep working after the seed changes:
the lender is the member with the most pending requests on their items; the borrower is the member with
the most loans, preferring one who still owes a review.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import psycopg

from . import seed

RESET_EVERY = timedelta(hours=24)
LOCK_ID = 7_310_002


def ensure_fresh(conn: psycopg.Connection, *, now: datetime, password: str | None = None) -> bool:
    """Load the demo world if it is missing or older than a day. Returns True if it (re)loaded.

    An advisory lock makes this safe when several workers wake up at once: one resets, the others wait and
    then see a fresh world.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        row = conn.execute("SELECT loaded_at FROM demo_state WHERE id = 1").fetchone()
        if row and now - row["loaded_at"] < RESET_EVERY:
            return False
        seed.reset(conn)
        seed.load(conn, now=now, password=password)
        conn.execute(
            "INSERT INTO demo_state (id, loaded_at) VALUES (1, %s) ON CONFLICT (id) DO UPDATE SET loaded_at = %s",
            (now, now),
        )
    return True


def demo_user(conn: psycopg.Connection, *, role: str, now: datetime) -> int:
    """The account a visitor gets for `role` ('lender' or 'borrower')."""
    if role == "lender":
        sql = """
            SELECT l.owner_id AS id
            FROM bookings b JOIN listings l ON l.id = b.listing_id JOIN users u ON u.id = l.owner_id
            WHERE b.status = 'requested' AND b.expires_at > %(now)s AND NOT u.is_admin AND u.hidden_at IS NULL
                  AND u.email_verified_at IS NOT NULL
            GROUP BY l.owner_id ORDER BY count(*) DESC, l.owner_id LIMIT 1
        """
    elif role == "borrower":
        sql = """
            SELECT b.borrower_id AS id
            FROM bookings b JOIN users u ON u.id = b.borrower_id
            WHERE b.kind = 'loan' AND NOT u.is_admin AND u.hidden_at IS NULL AND u.email_verified_at IS NOT NULL
            GROUP BY b.borrower_id
            ORDER BY bool_or(b.status = 'returned') DESC, count(*) DESC, b.borrower_id LIMIT 1
        """
    else:
        raise ValueError(role)
    row = conn.execute(sql, {"now": now}).fetchone()
    if row is None:
        raise LookupError(f"no demo {role} in the data")
    return row["id"]
