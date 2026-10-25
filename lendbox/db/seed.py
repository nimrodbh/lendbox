"""Load the demo world (seed/demo.json) into an empty database.

Dates in the file are offsets from the day the loader runs, so the demo never goes stale: a booking with
start_offset_days=3 always starts in three days. Every row goes through the real constraints, so a seed
that would double book, or tag an area as an interest, fails to load instead of producing a broken demo.

Usage: python -m lendbox.db.seed [--reset]
All seeded accounts share one demo password (LENDBOX_DEMO_PASSWORD, default in README); hashed once.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

from lendbox.domain.auth import hash_password
from lendbox.domain.booking import LOCAL_TZ, REQUEST_TTL

from .connection import connect
from .migrate import migrate

SEED_DIR = Path(os.environ.get("LENDBOX_SEED_DIR", Path(__file__).resolve().parents[2] / "seed"))
DEFAULT_DEMO_PASSWORD = "lendbox-demo-2026"
TABLES = [
    "reports", "reviews", "booking_messages", "bookings", "listing_photos", "listing_interests", "listings",
    "looking_for_posts", "community_members", "categories", "communities", "sessions", "email_tokens",
    "email_outbox", "login_failures", "rate_events", "users",
]


def _local(day: date, hour: int) -> datetime:
    return datetime.combine(day, time(hour), tzinfo=LOCAL_TZ).astimezone(UTC)


def load(conn: psycopg.Connection, *, now: datetime, seed_dir: Path = SEED_DIR, password: str | None = None) -> dict:
    """Insert the demo world in one transaction. Returns row counts. Refuses a database that has users."""
    world = json.loads((seed_dir / "demo.json").read_text(encoding="utf-8"))
    schemas = json.loads((seed_dir / "category_schemas.json").read_text(encoding="utf-8"))
    today = now.astimezone(LOCAL_TZ).date()
    pw_hash = hash_password(password or os.environ.get("LENDBOX_DEMO_PASSWORD", DEFAULT_DEMO_PASSWORD))
    ids: dict[str, dict[str, int]] = {"user": {}, "community": {}, "category": {}, "listing": {}, "booking": {}}

    def insert(sql: str, params: tuple) -> int:
        return conn.execute(sql + " RETURNING id", params).fetchone()["id"]

    with conn.transaction():
        if conn.execute("SELECT EXISTS (SELECT 1 FROM users) AS e").fetchone()["e"]:
            raise RuntimeError("database already has users; run with --reset to replace them")
        for u in world["users"]:
            ids["user"][u["key"]] = insert(
                "INSERT INTO users (email, display_name, password_hash, is_admin, email_verified_at, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (u["email"], u["display_name"], pw_hash, u["is_admin"], now if u["verified"] else None,
                 now - timedelta(days=60)),
            )
        for c in world["communities"]:  # the file lists parents before children
            ids["community"][c["slug"]] = insert(
                "INSERT INTO communities (slug, name, kind, area_level, parent_id, rules) VALUES (%s, %s, %s, %s, %s, %s)",
                (c["slug"], c["name"], c["kind"], c["area_level"],
                 ids["community"][c["parent"]] if c["parent"] else None, c["rules"]),
            )
        for m in world["memberships"]:
            conn.execute(
                "INSERT INTO community_members (user_id, community_id) VALUES (%s, %s)",
                (ids["user"][m["user"]], ids["community"][m["community"]]),
            )
        for c in world["categories"]:
            schema = schemas.get(c["slug"], {}).get("schema", {})
            ids["category"][c["slug"]] = insert(
                "INSERT INTO categories (slug, name, booking_unit, prohibited, attribute_schema) "
                "VALUES (%s, %s, %s, %s, %s)",
                (c["slug"], c["name"], c["booking_unit"], c["prohibited"], Jsonb(schema)),
            )
        for i, li in enumerate(world["listings"]):
            created = now - timedelta(days=45) + timedelta(hours=6 * i)  # spread so "newest first" means something
            lid = insert(
                """
                INSERT INTO listings (owner_id, category_id, area_id, title, description, condition,
                    price_per_unit_agorot, deposit_agorot, min_units, late_fee_agorot, attributes, pickup_detail,
                    status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (ids["user"][li["owner"]], ids["category"][li["category"]], ids["community"][li["area"]],
                 li["title"], li["description"], li["condition"], li["price_per_unit_agorot"], li["deposit_agorot"],
                 li["min_units"], li["late_fee_agorot"], Jsonb(li["attributes"]), li["pickup_detail"],
                 li["status"], created, created),
            )
            ids["listing"][li["key"]] = lid
            for slug in li["interests"]:
                conn.execute(
                    "INSERT INTO listing_interests (listing_id, community_id) VALUES (%s, %s)",
                    (lid, ids["community"][slug]),
                )
        for b in world["bookings"]:
            start = today + timedelta(days=b["start_offset_days"])
            end = start + timedelta(days=b["days"])
            if b["kind"] == "block":
                ids["booking"][b["key"]] = insert(
                    "INSERT INTO bookings (listing_id, kind, during, status, requested_at) "
                    "VALUES (%s, 'block', daterange(%s, %s), %s, %s)",
                    (ids["listing"][b["listing"]], start, end, b["status"], now - timedelta(days=7)),
                )
                continue
            requested_at = now + timedelta(hours=b["requested_offset_hours"])
            expires_at = requested_at + REQUEST_TTL
            status = b["status"]
            decided_at = {
                "requested": None,
                "expired": expires_at,
            }.get(status, requested_at + timedelta(hours=3))
            picked_up = _local(start, 10) if status in ("active", "returned") else None
            returned = _local(end - timedelta(days=1), 18) if status == "returned" else None
            listing = conn.execute(
                "SELECT price_per_unit_agorot, deposit_agorot FROM listings WHERE id = %s",
                (ids["listing"][b["listing"]],),
            ).fetchone()
            ids["booking"][b["key"]] = insert(
                """
                INSERT INTO bookings (listing_id, kind, borrower_id, during, status, rental_agorot, deposit_agorot,
                    message, requested_at, expires_at, decided_at, picked_up_at, returned_at)
                VALUES (%s, 'loan', %s, daterange(%s, %s), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (ids["listing"][b["listing"]], ids["user"][b["borrower"]], start, end, status,
                 b["days"] * listing["price_per_unit_agorot"], listing["deposit_agorot"], b["message"],
                 requested_at, expires_at, decided_at, picked_up, returned),
            )
        for r in world["reviews"]:
            returned_at = conn.execute(
                "SELECT returned_at FROM bookings WHERE id = %s", (ids["booking"][r["booking"]],)
            ).fetchone()["returned_at"]
            conn.execute(
                "INSERT INTO reviews (booking_id, author_id, subject_id, rating, body, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (ids["booking"][r["booking"]], ids["user"][r["author"]], ids["user"][r["subject"]], r["rating"],
                 r["body"], min(returned_at + timedelta(hours=20), now)),
            )
        for p in world["looking_for"]:
            wanted = None
            if p["wanted_start_offset_days"] is not None:
                w_start = today + timedelta(days=p["wanted_start_offset_days"])
                wanted = (w_start, w_start + timedelta(days=p["wanted_days"]))
            conn.execute(
                """
                INSERT INTO looking_for_posts (author_id, area_id, interest_id, category_id, title, body,
                    wanted_during, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s,
                        CASE WHEN %s::date IS NULL THEN NULL ELSE daterange(%s::date, %s::date) END, %s, %s)
                """,
                (ids["user"][p["author"]], ids["community"][p["area"]],
                 ids["community"][p["interest"]] if p["interest"] else None,
                 ids["category"][p["category"]] if p["category"] else None, p["title"], p["body"],
                 wanted and wanted[0], wanted and wanted[0], wanted and wanted[1], p["status"],
                 now - timedelta(days=3)),
            )
    return {k: len(v) for k, v in ids.items()} | {"reviews": len(world["reviews"]),
                                                    "looking_for": len(world["looking_for"])}


def reset(conn: psycopg.Connection) -> None:
    conn.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="delete all data first")
    args = parser.parse_args()
    with connect() as conn:
        migrate(conn)
        if args.reset:
            reset(conn)
        print("loaded:", load(conn, now=datetime.now(UTC)))


if __name__ == "__main__":
    main()
