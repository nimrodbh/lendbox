"""Repository: all SQL lives here. Functions take a connection and return plain values.

Approving a booking is the core write path. Two layers keep a listing from being double booked:
1. `approve_booking` locks the listing row first, so approvals on one listing run one at a time and the
   loser sees a clear state ("no longer pending") instead of a deadlock.
2. The `no_double_booking` exclusion constraint is the backstop: even a buggy or concurrent writer that
   skips the lock cannot commit two overlapping approved bookings (SQLSTATE 23P01).
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg
from psycopg import errors as pg_errors

from lendbox.domain.booking import (
    Actor,
    BookingEvent,
    BookingStatus,
    BookingUnit,
    DateRange,
    expires_at_for,
    quote,
    transition,
)
from lendbox.domain.errors import (
    BookingConflict,
    BookingNotPending,
    Forbidden,
    InvalidTransition,
    NotFound,
    TooManyOpenRequests,
)

MAX_OPEN_REQUESTS = 5


def area_subtree_ids(conn: psycopg.Connection, area_id: int) -> list[int]:
    """This area and every area inside it (ADR 0001: adjacency list + recursive CTE)."""
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM communities WHERE id = %(area_id)s AND kind = 'area'
            UNION
            SELECT child.id
            FROM communities AS child
            JOIN subtree AS parent ON child.parent_id = parent.id
            WHERE child.kind = 'area'
        )
        SELECT id FROM subtree ORDER BY id
        """,
        {"area_id": area_id},
    ).fetchall()
    return [r["id"] for r in rows]


def request_booking(
    conn: psycopg.Connection,
    *,
    listing_id: int,
    borrower_id: int,
    start: date,
    end: date,
    message: str = "",
    now: datetime,
) -> int:
    """Create a `requested` loan for [start, end), priced by domain.booking.quote and frozen on the row.

    Overlapping requests are allowed; approval decides.
    Raises NotFound, Forbidden, InvalidRange, BelowMinimum, TooManyOpenRequests.
    """
    with conn.transaction():
        listing = conn.execute(
            """
            SELECT l.owner_id, l.status, l.price_per_unit_agorot, l.deposit_agorot, l.min_units,
                   c.prohibited, c.booking_unit, o.hidden_at IS NOT NULL AS owner_hidden
            FROM listings l JOIN categories c ON c.id = l.category_id JOIN users o ON o.id = l.owner_id
            WHERE l.id = %s
            """,
            (listing_id,),
        ).fetchone()
        if listing is None or listing["status"] != "live" or listing["prohibited"] or listing["owner_hidden"]:
            raise NotFound(f"listing {listing_id} is not available")
        if listing["owner_id"] == borrower_id:
            raise Forbidden("you cannot borrow your own listing")
        # Threat model, threat 2: one open request per item, a few overall, so nobody can flood owners' inboxes.
        open_requests = conn.execute(
            """
            SELECT count(*) AS total, count(*) FILTER (WHERE listing_id = %s) AS here
            FROM bookings WHERE borrower_id = %s AND status = 'requested' AND expires_at > %s
            """,
            (listing_id, borrower_id, now),
        ).fetchone()
        if open_requests["here"] > 0 or open_requests["total"] >= MAX_OPEN_REQUESTS:
            raise TooManyOpenRequests(f"borrower {borrower_id} has {open_requests['total']} open requests")
        price = quote(
            BookingUnit(listing["booking_unit"]),
            listing["price_per_unit_agorot"],
            listing["deposit_agorot"],
            DateRange(start, end),
            listing["min_units"],
        )
        row = conn.execute(
            """
            INSERT INTO bookings (listing_id, kind, borrower_id, during, status,
                                  rental_agorot, deposit_agorot, message, requested_at, expires_at)
            VALUES (%s, 'loan', %s, daterange(%s, %s, '[)'), 'requested', %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (listing_id, borrower_id, start, end, price.rental_agorot, price.deposit_agorot, message, now,
             expires_at_for(now)),
        ).fetchone()
    return row["id"]


def approve_booking(conn: psycopg.Connection, *, booking_id: int, owner_id: int, now: datetime) -> list[int]:
    """Approve a pending loan and decline every pending loan that overlaps it, in one transaction.

    Returns the ids declined automatically. Raises NotFound, Forbidden, BookingNotPending or BookingConflict.
    """
    try:
        with conn.transaction():
            target = conn.execute(
                "SELECT listing_id FROM bookings WHERE id = %s AND kind = 'loan'", (booking_id,)
            ).fetchone()
            if target is None:
                raise NotFound(f"booking {booking_id}")
            listing = conn.execute(
                "SELECT owner_id FROM listings WHERE id = %s FOR UPDATE", (target["listing_id"],)
            ).fetchone()
            if listing["owner_id"] != owner_id:
                raise Forbidden("only the owner can approve")
            # Re-read under the listing lock: a concurrent approval may have declined this booking.
            booking = conn.execute(
                "SELECT status, expires_at, during FROM bookings WHERE id = %s", (booking_id,)
            ).fetchone()
            try:
                transition(
                    BookingStatus(booking["status"]),
                    BookingEvent.APPROVE,
                    Actor.OWNER,
                    now=now,
                    expires_at=booking["expires_at"],
                    starts_on=booking["during"].lower,
                )
            except InvalidTransition as exc:
                raise BookingNotPending(f"booking {booking_id} is {booking['status']} or past its expiry") from exc
            conn.execute(
                "UPDATE bookings SET status = 'approved', decided_at = %s WHERE id = %s", (now, booking_id)
            )
            declined = conn.execute(
                """
                UPDATE bookings SET status = 'declined', decided_at = %s
                WHERE listing_id = %s AND id <> %s AND status = 'requested' AND during && %s
                RETURNING id
                """,
                (now, target["listing_id"], booking_id, booking["during"]),
            ).fetchall()
    except pg_errors.ExclusionViolation as exc:
        raise BookingConflict(f"booking {booking_id} overlaps an approved booking") from exc
    return sorted(r["id"] for r in declined)


def expire_due_bookings(conn: psycopg.Connection, *, now: datetime) -> list[int]:
    """Close every request whose 48 hours ran out; return their ids."""
    rows = conn.execute(
        """
        UPDATE bookings SET status = 'expired', decided_at = %s
        WHERE status = 'requested' AND expires_at <= %s
        RETURNING id
        """,
        (now, now),
    ).fetchall()
    return sorted(r["id"] for r in rows)


def decline_booking(conn: psycopg.Connection, *, booking_id: int, owner_id: int, now: datetime) -> None:
    """Decline a pending loan. Raises NotFound, Forbidden or BookingNotPending."""
    with conn.transaction():
        row = conn.execute(
            """
            SELECT b.status, b.expires_at, b.during, l.owner_id
            FROM bookings b JOIN listings l ON l.id = b.listing_id
            WHERE b.id = %s AND b.kind = 'loan'
            FOR UPDATE OF b
            """,
            (booking_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"booking {booking_id}")
        if row["owner_id"] != owner_id:
            raise Forbidden("only the owner can decline")
        try:
            transition(
                BookingStatus(row["status"]),
                BookingEvent.DECLINE,
                Actor.OWNER,
                now=now,
                expires_at=row["expires_at"],
                starts_on=row["during"].lower,
            )
        except InvalidTransition as exc:
            raise BookingNotPending(f"booking {booking_id} is {row['status']}") from exc
        conn.execute("UPDATE bookings SET status = 'declined', decided_at = %s WHERE id = %s", (now, booking_id))


_EVENT_STAMP = {BookingEvent.PICKUP: "picked_up_at", BookingEvent.RETURN: "returned_at"}


def act_on_booking(
    conn: psycopg.Connection, *, booking_id: int, user_id: int, event: BookingEvent, now: datetime
) -> BookingStatus:
    """Cancel, pick up or return a loan. The caller's role (owner or borrower) is derived, never trusted.

    The domain state machine decides what is allowed; this function only locks, asks, and writes.
    Raises NotFound (also for users who are not a party, so ids cannot be probed) or BookingNotPending.
    """
    if event not in (BookingEvent.CANCEL, BookingEvent.PICKUP, BookingEvent.RETURN):
        raise ValueError(f"use approve_booking / decline_booking for {event}")
    with conn.transaction():
        row = conn.execute(
            """
            SELECT b.status, b.expires_at, b.during, b.borrower_id, l.owner_id
            FROM bookings b JOIN listings l ON l.id = b.listing_id
            WHERE b.id = %s AND b.kind = 'loan'
            FOR UPDATE OF b
            """,
            (booking_id,),
        ).fetchone()
        if row is None or user_id not in (row["owner_id"], row["borrower_id"]):
            raise NotFound(f"booking {booking_id}")
        actor = Actor.OWNER if user_id == row["owner_id"] else Actor.BORROWER
        try:
            new = transition(
                BookingStatus(row["status"]), event, actor,
                now=now, expires_at=row["expires_at"], starts_on=row["during"].lower,
            )
        except InvalidTransition as exc:
            raise BookingNotPending(f"cannot {event} booking {booking_id} as {actor} while {row['status']}") from exc
        stamp = _EVENT_STAMP.get(event, "decided_at")
        conn.execute(f"UPDATE bookings SET status = %s, {stamp} = %s WHERE id = %s", (new, now, booking_id))
    return new


def borrower_bookings(conn: psycopg.Connection, *, user_id: int, now: datetime) -> list[dict]:
    """Everything a borrower asked for, newest first, with whether a review is still due from them."""
    rows = conn.execute(
        """
        SELECT b.id, b.listing_id, b.status, lower(b.during) AS start, upper(b.during) AS end, b.rental_agorot,
               b.deposit_agorot, b.expires_at, l.title, u.display_name AS owner_name,
               (b.status = 'returned' AND NOT EXISTS (
                   SELECT 1 FROM reviews r WHERE r.booking_id = b.id AND r.author_id = %(u)s)) AS review_due
        FROM bookings b JOIN listings l ON l.id = b.listing_id JOIN users u ON u.id = l.owner_id
        WHERE b.borrower_id = %(u)s AND b.kind = 'loan'
        ORDER BY b.requested_at DESC
        """,
        {"u": user_id},
    ).fetchall()
    for r in rows:
        r["range"] = DateRange(r["start"], r["end"])
        if r["status"] == "requested" and r["expires_at"] <= now:
            r["status"] = "expired"  # the expiry job has not run yet; show the truth
    return rows


def communities(conn: psycopg.Connection) -> list[dict]:
    """All communities with live-listing counts; areas in tree order with their depth (parents first)."""
    return conn.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id, ARRAY[name] AS path, 0 AS depth FROM communities WHERE kind = 'area' AND parent_id IS NULL
            UNION ALL
            SELECT c.id, t.path || c.name, t.depth + 1
            FROM communities c JOIN tree t ON c.parent_id = t.id WHERE c.kind = 'area'
        )
        SELECT c.id, c.slug, c.name, c.kind, c.area_level, c.parent_id, coalesce(t.depth, 0) AS depth,
               (SELECT count(DISTINCT l.id) FROM listings l
                LEFT JOIN listing_interests li ON li.listing_id = l.id AND li.community_id = c.id
                WHERE l.status = 'live' AND (l.area_id = c.id OR li.community_id IS NOT NULL)) AS live_count
        FROM communities c LEFT JOIN tree t ON t.id = c.id
        ORDER BY c.kind, t.path, c.name
        """
    ).fetchall()


def categories(conn: psycopg.Connection) -> list[dict]:
    return conn.execute(
        "SELECT id, slug, name, booking_unit FROM categories WHERE NOT prohibited ORDER BY name"
    ).fetchall()


def search_listings(
    conn: psycopg.Connection,
    *,
    area_id: int | None = None,
    interest_id: int | None = None,
    category_id: int | None = None,
    start: date | None = None,
    end: date | None = None,
    max_price_agorot: int | None = None,
    q: str | None = None,
    limit: int = 60,
) -> list[dict]:
    """Live listings matching every given filter.

    - area: the area and everything inside it (a Florentin listing shows under Tel Aviv-Yafo).
    - start/end: only listings free for the whole half-open range (no approved or active booking overlaps it).
    - max_price: compares day prices only, since a day price and a month price cannot be compared.
    - q: full-text match on title and description, using the exact expression the index was built on.
    """
    if (start is None) != (end is None):
        raise ValueError("start and end go together")
    if start is not None:
        DateRange(start, end)  # raises InvalidRange for an empty or reversed range
    area_ids = area_subtree_ids(conn, area_id) if area_id is not None else None
    return conn.execute(
        """
        SELECT l.id, l.title, l.condition, l.price_per_unit_agorot, l.deposit_agorot,
               a.name AS area_name, c.name AS category_name, c.slug AS category_slug, c.booking_unit
        FROM listings l
        JOIN communities a ON a.id = l.area_id
        JOIN categories c ON c.id = l.category_id
        WHERE l.status = 'live' AND NOT c.prohibited
          AND NOT EXISTS (SELECT 1 FROM users o WHERE o.id = l.owner_id AND o.hidden_at IS NOT NULL)
          AND (%(area_ids)s::bigint[] IS NULL OR l.area_id = ANY(%(area_ids)s::bigint[]))
          AND (%(interest_id)s::bigint IS NULL OR EXISTS (
                SELECT 1 FROM listing_interests li
                WHERE li.listing_id = l.id AND li.community_id = %(interest_id)s::bigint))
          AND (%(category_id)s::bigint IS NULL OR l.category_id = %(category_id)s::bigint)
          AND (%(max_price)s::int IS NULL
               OR (c.booking_unit = 'day' AND l.price_per_unit_agorot <= %(max_price)s::int))
          AND (%(q)s::text IS NULL OR to_tsvector('simple'::regconfig, l.title || ' ' || l.description)
                                      @@ plainto_tsquery('simple'::regconfig, %(q)s::text))
          AND (%(start)s::date IS NULL OR NOT EXISTS (
                SELECT 1 FROM bookings b
                WHERE b.listing_id = l.id AND b.status IN ('approved', 'active')
                  AND b.during && daterange(%(start)s::date, %(end)s::date)))
        ORDER BY l.created_at DESC, l.id DESC
        LIMIT %(limit)s
        """,
        {
            "area_ids": area_ids,
            "interest_id": interest_id,
            "category_id": category_id,
            "max_price": max_price_agorot,
            "q": q.strip() if q and q.strip() else None,
            "start": start,
            "end": end,
            "limit": limit,
        },
    ).fetchall()


def listing_detail(conn: psycopg.Connection, *, listing_id: int, today: date) -> dict:
    """One live listing for its page, with the owner and the ranges taken from today on.

    `pickup_detail` is private: the web layer shows it only to the owner and to a borrower with an approved loan.
    """
    listing = conn.execute(
        """
        SELECT l.id, l.title, l.description, l.condition, l.price_per_unit_agorot, l.deposit_agorot,
               l.min_units, l.late_fee_agorot, l.pickup_detail, l.owner_id, l.status,
               u.display_name AS owner_name, u.email_verified_at IS NOT NULL AS owner_verified,
               a.name AS area_name, c.name AS category_name, c.booking_unit, c.prohibited,
               u.hidden_at IS NOT NULL AS owner_hidden
        FROM listings l
        JOIN users u ON u.id = l.owner_id
        JOIN communities a ON a.id = l.area_id
        JOIN categories c ON c.id = l.category_id
        WHERE l.id = %s
        """,
        (listing_id,),
    ).fetchone()
    if listing is None or listing["status"] != "live" or listing["prohibited"] or listing["owner_hidden"]:
        raise NotFound(f"listing {listing_id}")
    taken = conn.execute(
        """
        SELECT lower(during) AS start, upper(during) AS end FROM bookings
        WHERE listing_id = %s AND status IN ('approved', 'active') AND upper(during) > %s
        ORDER BY lower(during)
        """,
        (listing_id, today),
    ).fetchall()
    listing["taken"] = [DateRange(r["start"], r["end"]) for r in taken]
    return listing


def borrower_has_approved_loan(conn: psycopg.Connection, *, listing_id: int, user_id: int) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM bookings WHERE listing_id = %s AND borrower_id = %s "
        "AND status IN ('approved', 'active')) AS ok",
        (listing_id, user_id),
    ).fetchone()["ok"]


def lender_inbox(conn: psycopg.Connection, *, owner_id: int, now: datetime) -> dict[str, list[dict]]:
    """What a lender sees: pending requests (overlaps flagged), loans approved or out now, and their items."""
    requests = conn.execute(
        """
        SELECT b.id, b.listing_id, l.title, u.display_name AS borrower_name, lower(b.during) AS start,
               upper(b.during) AS end, b.rental_agorot, b.deposit_agorot, b.message, b.expires_at,
               coalesce(array_agg(o.id ORDER BY o.id) FILTER (WHERE o.id IS NOT NULL), '{}') AS overlaps_with
        FROM bookings b
        JOIN listings l ON l.id = b.listing_id
        JOIN users u ON u.id = b.borrower_id
        LEFT JOIN bookings o ON o.listing_id = b.listing_id AND o.id <> b.id AND o.status = 'requested'
                            AND o.expires_at > %(now)s AND o.during && b.during
        WHERE l.owner_id = %(owner)s AND b.kind = 'loan' AND b.status = 'requested' AND b.expires_at > %(now)s
        GROUP BY b.id, l.title, u.display_name
        ORDER BY b.expires_at
        """,
        {"owner": owner_id, "now": now},
    ).fetchall()
    out = conn.execute(
        """
        SELECT b.id, l.title, u.display_name AS borrower_name, lower(b.during) AS start, upper(b.during) AS end,
               b.status
        FROM bookings b
        JOIN listings l ON l.id = b.listing_id
        JOIN users u ON u.id = b.borrower_id
        WHERE l.owner_id = %s AND b.kind = 'loan' AND b.status IN ('approved', 'active')
        ORDER BY lower(b.during)
        """,
        (owner_id,),
    ).fetchall()
    items = conn.execute(
        "SELECT id, title, status, price_per_unit_agorot FROM listings WHERE owner_id = %s ORDER BY created_at DESC",
        (owner_id,),
    ).fetchall()
    for r in requests + out:
        r["range"] = DateRange(r["start"], r["end"])
    return {"requests": requests, "out": out, "items": items}
