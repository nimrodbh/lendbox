"""Schema guarantees from ADR 0001, each checked against the real database."""

from __future__ import annotations

import pytest
from psycopg import errors as pg_errors

from lendbox.db import repo
from lendbox.db.migrate import migrate


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []


def test_area_subtree_includes_children_but_not_interests(conn, world):
    assert repo.area_subtree_ids(conn, world.city) == [world.city, world.neighborhood]
    assert repo.area_subtree_ids(conn, world.country) == [world.country, world.city, world.neighborhood]
    assert repo.area_subtree_ids(conn, world.interest) == []


REJECTED = {
    "listing in an interest community": (
        ("INSERT INTO listings (owner_id, category_id, area_id, title, condition, price_per_unit_agorot, deposit_agorot) "
        "SELECT owner_id, category_id, {interest}, 'Saw', 'good', 0, 100 FROM listings LIMIT 1"),
        pg_errors.ForeignKeyViolation,
    ),
    "area used as an interest tag": (
        "INSERT INTO listing_interests (listing_id, community_id) VALUES ({listing}, {city})",
        pg_errors.ForeignKeyViolation,
    ),
    "area whose parent is an interest": (
        "INSERT INTO communities (slug, name, kind, area_level, parent_id) VALUES ('x', 'X', 'area', 'city', {interest})",
        pg_errors.ForeignKeyViolation,
    ),
    "city without a parent": (
        "INSERT INTO communities (slug, name, kind, area_level) VALUES ('y', 'Y', 'area', 'city')",
        pg_errors.CheckViolation,
    ),
    "looking-for post with an area as its interest": (
        "INSERT INTO looking_for_posts (author_id, area_id, interest_id, title) VALUES ({owner}, {city}, {city}, 'Tent')",
        pg_errors.ForeignKeyViolation,
    ),
    "loan without expiry": (
        ("INSERT INTO bookings (listing_id, kind, borrower_id, during, status) "
        "VALUES ({listing}, 'loan', {owner}, '[2026-12-01,2026-12-02)', 'requested')"),
        pg_errors.CheckViolation,
    ),
    "booking that never ends": (
        "INSERT INTO bookings (listing_id, kind, during, status) VALUES ({listing}, 'block', '[2026-12-01,infinity)', 'approved')",
        pg_errors.CheckViolation,
    ),
    "requested block": (
        "INSERT INTO bookings (listing_id, kind, during, status) VALUES ({listing}, 'block', '[2026-12-01,2026-12-02)', 'requested')",
        pg_errors.CheckViolation,
    ),
    "report with two targets": (
        "INSERT INTO reports (reporter_id, listing_id, target_user_id, reason) VALUES ({owner}, {listing}, {owner}, 'spam')",
        pg_errors.CheckViolation,
    ),
    "blank report reason": (
        "INSERT INTO reports (reporter_id, listing_id, reason) VALUES ({owner}, {listing}, '  ')",
        pg_errors.CheckViolation,
    ),
}


@pytest.mark.parametrize("case", REJECTED, ids=list(REJECTED))
def test_schema_rejects(conn, world, case):
    sql, error = REJECTED[case]
    ids = {"owner": world.owner, "listing": world.listing, "city": world.city, "interest": world.interest}
    with pytest.raises(error):
        conn.execute(sql.format(**ids))


def test_new_listing_defaults_to_draft(conn, world):
    row = conn.execute(
        "INSERT INTO listings (owner_id, category_id, area_id, title, condition, price_per_unit_agorot, deposit_agorot) "
        "SELECT owner_id, category_id, area_id, 'Ladder', 'good', 0, 100 FROM listings LIMIT 1 RETURNING status"
    ).fetchone()
    assert row["status"] == "draft"
