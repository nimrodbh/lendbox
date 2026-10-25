from __future__ import annotations

from datetime import date, timedelta

import pytest

from lendbox.db import repo
from lendbox.domain.errors import InvalidRange

from .conftest import NOW

D = date(2026, 11, 12)


def add_listing(conn, world, title, area, price=1000, status="live"):
    return conn.execute(
        """
        INSERT INTO listings (owner_id, category_id, area_id, title, condition,
                              price_per_unit_agorot, deposit_agorot, status)
        SELECT %s, category_id, %s, %s, 'good', %s, 5000, %s FROM listings LIMIT 1 RETURNING id
        """,
        (world.owner, area, title, price, status),
    ).fetchone()["id"]


def ids(rows):
    return {r["id"] for r in rows}


def test_area_filter_includes_child_areas(conn, world):
    in_city = add_listing(conn, world, "Ladder", world.city)
    # world.listing sits in Florentin, a neighborhood inside the city
    assert ids(repo.search_listings(conn, area_id=world.city)) == {world.listing, in_city}
    assert ids(repo.search_listings(conn, area_id=world.neighborhood)) == {world.listing}


def test_interest_filter(conn, world):
    other = add_listing(conn, world, "Ladder", world.city)
    conn.execute("INSERT INTO listing_interests (listing_id, community_id) VALUES (%s, %s)", (other, world.interest))
    assert ids(repo.search_listings(conn, interest_id=world.interest)) == {other}


def test_free_dates_exclude_booked_listings_but_not_touching_ones(conn, world):
    repo.approve_booking(
        conn,
        booking_id=repo.request_booking(
            conn, listing_id=world.listing, borrower_id=world.borrowers[0],
            start=D, end=D + timedelta(days=3), now=NOW,
        ),
        owner_id=world.owner,
        now=NOW,
    )
    assert ids(repo.search_listings(conn, start=D + timedelta(days=2), end=D + timedelta(days=4))) == set()
    assert ids(repo.search_listings(conn, start=D + timedelta(days=3), end=D + timedelta(days=5))) == {world.listing}


def test_pending_requests_do_not_hide_a_listing(conn, world):
    repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[0], start=D, end=D + timedelta(days=3), now=NOW
    )
    assert ids(repo.search_listings(conn, start=D, end=D + timedelta(days=1))) == {world.listing}


def test_max_price_and_free_items(conn, world):
    free = add_listing(conn, world, "Free ladder", world.city, price=0)
    assert ids(repo.search_listings(conn, max_price_agorot=1000)) == {free}  # the drill costs 1500 a day


def test_text_search_and_hidden_listings(conn, world):
    add_listing(conn, world, "Hidden drill press", world.city, status="hidden")
    assert ids(repo.search_listings(conn, q="drill")) == {world.listing}
    assert ids(repo.search_listings(conn, q="   ")) == {world.listing}


def test_reversed_or_half_range_is_rejected(conn, world):
    with pytest.raises(InvalidRange):
        repo.search_listings(conn, start=D, end=D)
    with pytest.raises(ValueError):
        repo.search_listings(conn, start=D)


def test_lender_inbox_flags_overlapping_requests(conn, world):
    a = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[0], start=D, end=D + timedelta(days=3), now=NOW
    )
    b = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[1],
        start=D + timedelta(days=2), end=D + timedelta(days=4), now=NOW,
    )
    c = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[2],
        start=D + timedelta(days=10), end=D + timedelta(days=11), now=NOW,
    )
    inbox = repo.lender_inbox(conn, owner_id=world.owner, now=NOW)
    overlaps = {r["id"]: r["overlaps_with"] for r in inbox["requests"]}
    assert overlaps == {a: [b], b: [a], c: []}
    assert repo.lender_inbox(conn, owner_id=world.borrowers[0], now=NOW)["requests"] == []
