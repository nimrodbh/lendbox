"""Listings an owner creates and manages: draft, publish, date blocks. All SQL for listing writes lives here.

A listing is saved as a draft first and goes live only through `publish_listing`, which validates its
attributes against the category's JSON Schema (ADR 0001). Live at once: there is no admin queue.
"""

from __future__ import annotations

from datetime import date, datetime

import psycopg
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from lendbox.domain.booking import DateRange
from lendbox.domain.errors import BookingConflict, Forbidden, NotFound
from lendbox.domain.listing import InvalidListing, validate_attributes


def category_for_form(conn: psycopg.Connection, category_id: int) -> dict:
    row = conn.execute(
        "SELECT id, slug, name, booking_unit, prohibited, attribute_schema FROM categories WHERE id = %s",
        (category_id,),
    ).fetchone()
    if row is None or row["prohibited"]:
        raise InvalidListing("category", "listing.error.category")
    return row


def create_listing(
    conn: psycopg.Connection, *, owner_id: int, category_id: int, fields: dict, attributes: dict, now: datetime
) -> int:
    """Save a draft. `fields` comes from domain.listing.validate_listing; the category must be allowed."""
    category_for_form(conn, category_id)
    with conn.transaction():
        listing_id = conn.execute(
            """
            INSERT INTO listings (owner_id, category_id, area_id, title, description, condition, price_per_unit_agorot,
                deposit_agorot, min_units, attributes, pickup_detail, status, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'draft', %s, %s)
            RETURNING id
            """,
            (owner_id, category_id, fields["area_id"], fields["title"], fields["description"], fields["condition"],
             fields["price_per_unit_agorot"], fields["deposit_agorot"], fields["min_units"], Jsonb(attributes),
             fields["pickup_detail"], now, now),
        ).fetchone()["id"]
        for interest_id in fields["interests"]:
            conn.execute(
                "INSERT INTO listing_interests (listing_id, community_id) VALUES (%s, %s)", (listing_id, interest_id)
            )
    return listing_id


def owner_listing(conn: psycopg.Connection, *, listing_id: int, owner_id: int) -> dict:
    """Any status, for its owner only (others get NotFound, so drafts stay private)."""
    row = conn.execute(
        """
        SELECT l.*, c.name AS category_name, c.attribute_schema, a.name AS area_name
        FROM listings l JOIN categories c ON c.id = l.category_id JOIN communities a ON a.id = l.area_id
        WHERE l.id = %s AND l.owner_id = %s
        """,
        (listing_id, owner_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"listing {listing_id}")
    return row


def publish_listing(conn: psycopg.Connection, *, listing_id: int, owner_id: int, now: datetime) -> None:
    """Draft to live, after the attributes pass the category schema. Raises NotFound or InvalidListing."""
    with conn.transaction():
        row = conn.execute(
            """
            SELECT l.status, l.attributes, c.attribute_schema, c.prohibited
            FROM listings l JOIN categories c ON c.id = l.category_id
            WHERE l.id = %s AND l.owner_id = %s
            FOR UPDATE OF l
            """,
            (listing_id, owner_id),
        ).fetchone()
        if row is None or row["status"] not in ("draft", "live"):
            raise NotFound(f"listing {listing_id}")
        if row["prohibited"]:
            raise InvalidListing("category", "listing.error.category")
        validate_attributes(row["attribute_schema"], row["attributes"])
        conn.execute("UPDATE listings SET status = 'live', updated_at = %s WHERE id = %s", (now, listing_id))


def add_block(conn: psycopg.Connection, *, listing_id: int, owner_id: int, start: date, end: date) -> int:
    """Block [start, end) on the owner's calendar. Fails with BookingConflict if a loan holds those dates."""
    DateRange(start, end)
    owner = conn.execute("SELECT owner_id FROM listings WHERE id = %s", (listing_id,)).fetchone()
    if owner is None:
        raise NotFound(f"listing {listing_id}")
    if owner["owner_id"] != owner_id:
        raise Forbidden("only the owner can block dates")
    try:
        return conn.execute(
            "INSERT INTO bookings (listing_id, kind, during, status) VALUES (%s, 'block', daterange(%s, %s), 'approved') "
            "RETURNING id",
            (listing_id, start, end),
        ).fetchone()["id"]
    except pg_errors.ExclusionViolation as exc:
        raise BookingConflict("those dates are already booked") from exc


def remove_block(conn: psycopg.Connection, *, booking_id: int, owner_id: int) -> None:
    """Cancel a block: the row stays as history, the dates free up."""
    row = conn.execute(
        """
        UPDATE bookings b SET status = 'cancelled'
        FROM listings l
        WHERE b.id = %s AND b.kind = 'block' AND b.status = 'approved' AND l.id = b.listing_id AND l.owner_id = %s
        RETURNING b.id
        """,
        (booking_id, owner_id),
    ).fetchone()
    if row is None:
        raise NotFound(f"block {booking_id}")


def owner_blocks(conn: psycopg.Connection, *, listing_id: int, today: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, lower(during) AS start, upper(during) AS end FROM bookings
        WHERE listing_id = %s AND kind = 'block' AND status = 'approved' AND upper(during) > %s
        ORDER BY lower(during)
        """,
        (listing_id, today),
    ).fetchall()
    for r in rows:
        r["range"] = DateRange(r["start"], r["end"])
    return rows
