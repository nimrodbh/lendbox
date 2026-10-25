"""DB test fixtures. These tests need a real PostgreSQL 16 (the exclusion constraint is the point), so they run
only when LENDBOX_TEST_DATABASE_URL is set: by CI's Postgres service, or locally with `docker compose up db`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from lendbox.db.connection import connect
from lendbox.db.migrate import migrate

TEST_DSN = os.environ.get("LENDBOX_TEST_DATABASE_URL")
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def dsn() -> str:
    if not TEST_DSN:
        pytest.skip("LENDBOX_TEST_DATABASE_URL not set")
    with connect(TEST_DSN) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        migrate(conn)
    return TEST_DSN


@pytest.fixture
def conn(dsn):
    with connect(dsn) as c:
        tables = [
            r["tablename"]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
            ).fetchall()
        ]
        c.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
        yield c


@dataclass(frozen=True)
class World:
    owner: int
    borrowers: list[int]
    country: int
    city: int
    neighborhood: int
    interest: int
    listing: int


@pytest.fixture
def world(conn) -> World:
    """A small fictional world: one owner with a live drill in Florentin, eight would-be borrowers."""
    users = [
        conn.execute(
            "INSERT INTO users (email, display_name, password_hash) VALUES (%s, %s, 'x') RETURNING id",
            (f"user{i}@example.test", f"User {i}"),
        ).fetchone()["id"]
        for i in range(9)
    ]

    def community(slug: str, kind: str, level: str | None = None, parent: int | None = None) -> int:
        return conn.execute(
            "INSERT INTO communities (slug, name, kind, area_level, parent_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (slug, slug.title(), kind, level, parent),
        ).fetchone()["id"]

    country = community("israel", "area", "country")
    city = community("tel-aviv-yafo", "area", "city", country)
    neighborhood = community("florentin", "area", "neighborhood", city)
    interest = community("builders", "interest")
    category = conn.execute(
        "INSERT INTO categories (slug, name, booking_unit) VALUES ('power-tools', 'Power tools', 'day') RETURNING id"
    ).fetchone()["id"]
    listing = conn.execute(
        """
        INSERT INTO listings (owner_id, category_id, area_id, title, condition,
                              price_per_unit_agorot, deposit_agorot, status)
        VALUES (%s, %s, %s, 'Cordless drill', 'good', 1500, 20000, 'live') RETURNING id
        """,
        (users[0], category, neighborhood),
    ).fetchone()["id"]
    return World(users[0], users[1:], country, city, neighborhood, interest, listing)
