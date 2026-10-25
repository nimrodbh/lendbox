from __future__ import annotations

import pytest

from lendbox.db import repo, seed

from .conftest import NOW


@pytest.fixture
def loaded(conn):
    return seed.load(conn, now=NOW, password="demo password for tests")


def test_demo_world_loads_through_every_constraint(conn, loaded):
    assert loaded["user"] == 40 and loaded["listing"] == 120
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, count(*) AS n FROM bookings GROUP BY status")}
    assert set(counts) == {"requested", "approved", "active", "returned", "declined", "expired", "cancelled"}


def test_demo_dates_are_relative_to_today(conn, loaded):
    active = conn.execute("SELECT lower(during) AS s, upper(during) AS e FROM bookings WHERE status = 'active'").fetchall()
    today = NOW.date()
    assert active and all(r["s"] <= today < r["e"] for r in active)


def test_demo_search_finds_items_in_the_tel_aviv_subtree(conn, loaded):
    tlv = conn.execute("SELECT id FROM communities WHERE slug = 'tel-aviv-yafo'").fetchone()["id"]
    assert len(repo.search_listings(conn, area_id=tlv, limit=200)) >= 70


def test_loading_twice_is_refused(conn, loaded):
    with pytest.raises(RuntimeError):
        seed.load(conn, now=NOW, password="x")
