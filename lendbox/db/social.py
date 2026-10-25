"""The people side: booking threads, blind reviews, community membership, Looking for posts.

Every read and write checks that the caller is a party (owner or borrower). A non-party gets NotFound,
not Forbidden, so booking ids cannot be probed to learn which ones exist.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import psycopg

from lendbox.domain.booking import DateRange
from lendbox.domain.errors import LendboxError, NotFound

BLIND_WINDOW = timedelta(days=14)


class ReviewNotAllowed(LendboxError):
    """Not returned yet, not a party, or already reviewed."""


def booking_thread(conn: psycopg.Connection, *, booking_id: int, user_id: int) -> dict:
    """A loan with its messages, for one of its two parties. Pickup detail only once the owner approved."""
    b = conn.execute(
        """
        SELECT b.id, b.listing_id, b.status, lower(b.during) AS start, upper(b.during) AS end, b.rental_agorot,
               b.deposit_agorot, b.message, b.borrower_id, l.owner_id, l.title, l.pickup_detail, l.area_id,
               ou.display_name AS owner_name, bu.display_name AS borrower_name, a.name AS area_name
        FROM bookings b
        JOIN listings l ON l.id = b.listing_id
        JOIN users ou ON ou.id = l.owner_id
        JOIN users bu ON bu.id = b.borrower_id
        JOIN communities a ON a.id = l.area_id
        WHERE b.id = %s AND b.kind = 'loan'
        """,
        (booking_id,),
    ).fetchone()
    if b is None or user_id not in (b["owner_id"], b["borrower_id"]):
        raise NotFound(f"booking {booking_id}")
    if b["status"] not in ("approved", "active", "returned"):
        b["pickup_detail"] = None
    b["range"] = DateRange(b["start"], b["end"])
    b["role"] = "owner" if user_id == b["owner_id"] else "borrower"
    b["messages"] = conn.execute(
        """
        SELECT m.body, m.created_at, m.sender_id, u.display_name AS sender_name
        FROM booking_messages m JOIN users u ON u.id = m.sender_id
        WHERE m.booking_id = %s ORDER BY m.created_at, m.id
        """,
        (booking_id,),
    ).fetchall()
    b["my_review"] = conn.execute(
        "SELECT rating FROM reviews WHERE booking_id = %s AND author_id = %s", (booking_id, user_id)
    ).fetchone()
    return b


def post_message(conn: psycopg.Connection, *, booking_id: int, sender_id: int, body: str, now: datetime) -> None:
    """Only the two parties, and only while the loan is live or within 14 days of its return (threat model, 6).
    A closed thread raises NotFound like a stranger's would."""
    body = body.strip()[:2000]
    if not body:
        return
    row = conn.execute(
        """
        INSERT INTO booking_messages (booking_id, sender_id, body, created_at)
        SELECT b.id, %(s)s, %(body)s, %(now)s FROM bookings b JOIN listings l ON l.id = b.listing_id
        WHERE b.id = %(b)s AND b.kind = 'loan' AND %(s)s IN (b.borrower_id, l.owner_id)
          AND (b.status IN ('requested', 'approved', 'active')
               OR (b.status = 'returned' AND b.returned_at > %(now)s - %(window)s))
        RETURNING id
        """,
        {"b": booking_id, "s": sender_id, "body": body, "now": now, "window": BLIND_WINDOW},
    ).fetchone()
    if row is None:
        raise NotFound(f"booking {booking_id}")


def submit_review(
    conn: psycopg.Connection, *, booking_id: int, author_id: int, rating: int, body: str, now: datetime
) -> None:
    """One review per party per returned loan. The subject is derived: the other party, never the form."""
    if not 1 <= rating <= 5:
        raise ReviewNotAllowed("rating must be 1 to 5")
    row = conn.execute(
        """
        INSERT INTO reviews (booking_id, author_id, subject_id, rating, body, created_at)
        SELECT b.id, %(a)s, CASE WHEN %(a)s = b.borrower_id THEN l.owner_id ELSE b.borrower_id END,
               %(rating)s, %(body)s, %(now)s
        FROM bookings b JOIN listings l ON l.id = b.listing_id
        WHERE b.id = %(b)s AND b.kind = 'loan' AND b.status = 'returned' AND %(a)s IN (b.borrower_id, l.owner_id)
        ON CONFLICT (booking_id, author_id) DO NOTHING
        RETURNING id
        """,
        {"b": booking_id, "a": author_id, "rating": rating, "body": body.strip()[:1000], "now": now},
    ).fetchone()
    if row is None:
        raise ReviewNotAllowed(f"booking {booking_id}")


_VISIBLE = """
    r.subject_id = %(u)s AND (
        EXISTS (SELECT 1 FROM reviews other WHERE other.booking_id = r.booking_id AND other.author_id = r.subject_id)
        OR b.returned_at <= %(now)s - %(window)s)
"""


def visible_reviews(conn: psycopg.Connection, *, user_id: int, now: datetime) -> list[dict]:
    """Reviews about a user that are out of the blind period: both sides wrote, or 14 days passed since return."""
    return conn.execute(
        f"""
        SELECT r.rating, r.body, r.created_at, au.display_name AS author_name
        FROM reviews r JOIN bookings b ON b.id = r.booking_id JOIN users au ON au.id = r.author_id
        WHERE {_VISIBLE}
        ORDER BY r.created_at DESC
        """,
        {"u": user_id, "now": now, "window": BLIND_WINDOW},
    ).fetchall()


def rating(conn: psycopg.Connection, *, user_id: int, now: datetime) -> dict:
    """Average of visible reviews only (the blind rule cannot leak through the average), counting each author's
    latest review once: twenty instant loans between two colluding accounts count as one (threat model, 15)."""
    row = conn.execute(
        f"""
        SELECT round(avg(rating), 1) AS avg, count(*) AS n FROM (
            SELECT DISTINCT ON (r.author_id) r.rating
            FROM reviews r JOIN bookings b ON b.id = r.booking_id
            WHERE {_VISIBLE}
            ORDER BY r.author_id, r.created_at DESC
        ) one_per_author
        """,
        {"u": user_id, "now": now, "window": BLIND_WINDOW},
    ).fetchone()
    return {"avg": float(row["avg"]) if row["avg"] is not None else None, "count": row["n"]}


def community(conn: psycopg.Connection, *, slug: str, user_id: int | None) -> dict:
    row = conn.execute(
        """
        SELECT c.id, c.slug, c.name, c.kind, c.area_level, c.rules, p.name AS parent_name,
               (SELECT count(*) FROM community_members m WHERE m.community_id = c.id) AS members,
               EXISTS (SELECT 1 FROM community_members m WHERE m.community_id = c.id AND m.user_id = %s) AS joined
        FROM communities c LEFT JOIN communities p ON p.id = c.parent_id
        WHERE c.slug = %s
        """,
        (user_id, slug),
    ).fetchone()
    if row is None:
        raise NotFound(slug)
    return row


def toggle_membership(conn: psycopg.Connection, *, community_id: int, user_id: int) -> bool:
    """Join if not a member, leave if a member. Returns True when the user is now a member."""
    left = conn.execute(
        "DELETE FROM community_members WHERE user_id = %s AND community_id = %s RETURNING user_id",
        (user_id, community_id),
    ).fetchone()
    if left:
        return False
    conn.execute(
        "INSERT INTO community_members (user_id, community_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (user_id, community_id),
    )
    return True


def looking_for(
    conn: psycopg.Connection, *, area_ids: list[int] | None = None, interest_id: int | None = None, limit: int = 30
) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.id, p.title, p.body, lower(p.wanted_during) AS start, upper(p.wanted_during) AS end,
               a.name AS area_name, u.display_name AS author_name, p.created_at
        FROM looking_for_posts p JOIN communities a ON a.id = p.area_id JOIN users u ON u.id = p.author_id
        WHERE p.status = 'open'
          AND (%(areas)s::bigint[] IS NULL OR p.area_id = ANY(%(areas)s::bigint[]))
          AND (%(interest)s::bigint IS NULL OR p.interest_id = %(interest)s::bigint)
        ORDER BY p.created_at DESC, p.id DESC
        LIMIT %(limit)s
        """,
        {"areas": area_ids, "interest": interest_id, "limit": limit},
    ).fetchall()
    for r in rows:
        r["range"] = DateRange(r["start"], r["end"]) if r["start"] else None
    return rows


def post_looking_for(
    conn: psycopg.Connection,
    *,
    author_id: int,
    area_id: int,
    interest_id: int | None,
    title: str,
    body: str,
    wanted: tuple[date, date] | None,
    now: datetime,
) -> int:
    title = " ".join(title.split())
    if not 3 <= len(title) <= 80:
        raise ValueError("title")
    if wanted:
        DateRange(*wanted)
    return conn.execute(
        """
        INSERT INTO looking_for_posts (author_id, area_id, interest_id, title, body, wanted_during, created_at)
        VALUES (%s, %s, %s, %s, %s, CASE WHEN %s::date IS NULL THEN NULL ELSE daterange(%s::date, %s::date) END, %s)
        RETURNING id
        """,
        (author_id, area_id, interest_id, title, body.strip()[:1000],
         wanted and wanted[0], wanted and wanted[0], wanted and wanted[1], now),
    ).fetchone()["id"]


REPORT_TARGETS = {"listing": "listing_id", "user": "target_user_id", "looking_for": "looking_for_id"}


def file_report(
    conn: psycopg.Connection, *, reporter_id: int, target_type: str, target_id: int, reason: str, now: datetime
) -> int:
    """Report a listing, a member or a Looking for post. The target must exist (foreign keys say so)."""
    column = REPORT_TARGETS.get(target_type)
    reason = reason.strip()[:1000]
    if column is None or not reason:
        raise ValueError("unknown target or empty reason")
    return conn.execute(
        f"INSERT INTO reports (reporter_id, {column}, reason, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (reporter_id, target_id, reason, now),
    ).fetchone()["id"]


def open_reports(conn: psycopg.Connection) -> list[dict]:
    """The admin queue: unresolved reports, oldest first, with enough context to decide."""
    return conn.execute(
        """
        SELECT r.id, r.reason, r.created_at, ru.display_name AS reporter_name,
               CASE WHEN r.listing_id IS NOT NULL THEN 'listing'
                    WHEN r.target_user_id IS NOT NULL THEN 'user' ELSE 'looking_for' END AS target_type,
               coalesce(r.listing_id, r.target_user_id, r.looking_for_id) AS target_id,
               coalesce(l.title, tu.display_name || ' (' || tu.email || ')', p.title) AS target_label
        FROM reports r
        JOIN users ru ON ru.id = r.reporter_id
        LEFT JOIN listings l ON l.id = r.listing_id
        LEFT JOIN users tu ON tu.id = r.target_user_id
        LEFT JOIN looking_for_posts p ON p.id = r.looking_for_id
        WHERE r.resolved_at IS NULL
        ORDER BY r.created_at
        """
    ).fetchall()


def resolve_report(conn: psycopg.Connection, *, report_id: int, admin_id: int, action: str, now: datetime) -> None:
    """Hide the target (listing hidden, member hidden and signed out, post closed) or dismiss the report.

    Resolving one report resolves every open report on the same target, so admins never act twice.
    """
    if action not in ("hidden", "dismissed"):
        raise ValueError(action)
    with conn.transaction():
        admin = conn.execute("SELECT is_admin FROM users WHERE id = %s", (admin_id,)).fetchone()
        if not admin or not admin["is_admin"]:
            raise NotFound(f"report {report_id}")
        r = conn.execute(
            "SELECT listing_id, target_user_id, looking_for_id FROM reports WHERE id = %s AND resolved_at IS NULL "
            "FOR UPDATE",
            (report_id,),
        ).fetchone()
        if r is None:
            raise NotFound(f"report {report_id}")
        if action == "hidden":
            if r["listing_id"]:
                conn.execute("UPDATE listings SET status = 'hidden', updated_at = %s WHERE id = %s",
                             (now, r["listing_id"]))
            elif r["target_user_id"]:
                conn.execute("UPDATE users SET hidden_at = %s WHERE id = %s", (now, r["target_user_id"]))
                conn.execute("DELETE FROM sessions WHERE user_id = %s", (r["target_user_id"],))
            else:
                conn.execute("UPDATE looking_for_posts SET status = 'closed' WHERE id = %s", (r["looking_for_id"],))
        conn.execute(
            """
            UPDATE reports SET resolved_at = %(now)s, resolved_by = %(admin)s, resolution = %(action)s
            WHERE resolved_at IS NULL AND listing_id IS NOT DISTINCT FROM %(l)s
              AND target_user_id IS NOT DISTINCT FROM %(u)s AND looking_for_id IS NOT DISTINCT FROM %(p)s
            """,
            {"now": now, "admin": admin_id, "action": action,
             "l": r["listing_id"], "u": r["target_user_id"], "p": r["looking_for_id"]},
        )
