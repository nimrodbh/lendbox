"""Pickup, return, cancel, owner blocks, listings, threads, blind reviews, communities."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from psycopg.types.json import Jsonb

from lendbox.db import listings, repo, social
from lendbox.domain.booking import BookingEvent, BookingStatus
from lendbox.domain.errors import BookingConflict, BookingNotPending, NotFound
from lendbox.domain.listing import InvalidListing

from .conftest import NOW

TODAY = NOW.date()  # 2026-11-01 09:00 UTC is the same date in Jerusalem


def approved_loan(conn, world, *, start: date, days: int = 2, borrower: int | None = None) -> int:
    b = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=borrower or world.borrowers[0],
        start=start, end=start + timedelta(days=days), now=NOW,
    )
    repo.approve_booking(conn, booking_id=b, owner_id=world.owner, now=NOW)
    return b


def act(conn, b, user, event, now=NOW):
    return repo.act_on_booking(conn, booking_id=b, user_id=user, event=event, now=now)


def test_full_lifecycle_sets_timestamps(conn, world):
    b = approved_loan(conn, world, start=TODAY)
    assert act(conn, b, world.owner, BookingEvent.PICKUP) == BookingStatus.ACTIVE
    assert act(conn, b, world.owner, BookingEvent.RETURN) == BookingStatus.RETURNED
    row = conn.execute("SELECT picked_up_at, returned_at FROM bookings WHERE id = %s", (b,)).fetchone()
    assert row["picked_up_at"] == NOW and row["returned_at"] == NOW


def test_pickup_before_the_start_day_is_refused(conn, world):
    b = approved_loan(conn, world, start=TODAY + timedelta(days=3))
    with pytest.raises(BookingNotPending):
        act(conn, b, world.owner, BookingEvent.PICKUP)


def test_borrower_cannot_mark_returned(conn, world):
    b = approved_loan(conn, world, start=TODAY)
    act(conn, b, world.owner, BookingEvent.PICKUP)
    with pytest.raises(BookingNotPending):
        act(conn, b, world.borrowers[0], BookingEvent.RETURN)


def test_strangers_get_not_found(conn, world):
    b = approved_loan(conn, world, start=TODAY + timedelta(days=3))
    with pytest.raises(NotFound):
        act(conn, b, world.borrowers[5], BookingEvent.CANCEL)
    with pytest.raises(NotFound):
        social.booking_thread(conn, booking_id=b, user_id=world.borrowers[5])
    with pytest.raises(NotFound):
        social.post_message(conn, booking_id=b, sender_id=world.borrowers[5], body="hi", now=NOW)


def test_cancelling_an_approval_frees_the_dates(conn, world):
    start = TODAY + timedelta(days=3)
    b = approved_loan(conn, world, start=start)
    act(conn, b, world.borrowers[0], BookingEvent.CANCEL)
    assert approved_loan(conn, world, start=start, borrower=world.borrowers[1])  # same dates, no conflict


def test_owner_block_and_unblock(conn, world):
    start = TODAY + timedelta(days=10)
    block = listings.add_block(conn, listing_id=world.listing, owner_id=world.owner, start=start, end=start + timedelta(days=2))
    with pytest.raises(BookingConflict):
        approved_loan(conn, world, start=start)
    listings.remove_block(conn, booking_id=block, owner_id=world.owner)
    assert approved_loan(conn, world, start=start, borrower=world.borrowers[1])  # the first request is still open
    with pytest.raises(BookingConflict):  # now the loan holds the dates
        listings.add_block(conn, listing_id=world.listing, owner_id=world.owner, start=start, end=start + timedelta(days=1))


def _schema_category(conn) -> int:
    schema = {"type": "object", "required": ["power_source"], "properties": {"power_source": {"enum": ["battery"]}}}
    return conn.execute(
        "INSERT INTO categories (slug, name, booking_unit, attribute_schema) VALUES ('saws', 'Saws', 'day', %s) RETURNING id",
        (Jsonb(schema),),
    ).fetchone()["id"]


FIELDS = {"title": "Jigsaw", "description": "", "condition": "good", "price_per_unit_agorot": 1000,
          "deposit_agorot": 5000, "min_units": 1, "pickup_detail": "Lobby", "interests": []}


def test_draft_is_private_and_publish_validates_attributes(conn, world):
    cat = _schema_category(conn)
    fields = {**FIELDS, "area_id": world.neighborhood, "interests": [world.interest]}
    draft = listings.create_listing(conn, owner_id=world.owner, category_id=cat, fields=fields, attributes={}, now=NOW)
    assert draft not in {r["id"] for r in repo.search_listings(conn)}
    with pytest.raises(NotFound):
        listings.owner_listing(conn, listing_id=draft, owner_id=world.borrowers[0])
    with pytest.raises(InvalidListing):  # power_source is required by the category schema
        listings.publish_listing(conn, listing_id=draft, owner_id=world.owner, now=NOW)
    conn.execute("UPDATE listings SET attributes = '{\"power_source\": \"battery\"}' WHERE id = %s", (draft,))
    listings.publish_listing(conn, listing_id=draft, owner_id=world.owner, now=NOW)
    assert draft in {r["id"] for r in repo.search_listings(conn, interest_id=world.interest)}


def test_prohibited_category_cannot_be_listed(conn, world):
    cat = conn.execute(
        "INSERT INTO categories (slug, name, booking_unit, prohibited) VALUES ('vehicles', 'Vehicles', 'day', true) RETURNING id"
    ).fetchone()["id"]
    with pytest.raises(InvalidListing):
        listings.create_listing(
            conn, owner_id=world.owner, category_id=cat, fields={**FIELDS, "area_id": world.city}, attributes={}, now=NOW
        )


def returned_loan(conn, world) -> int:
    b = approved_loan(conn, world, start=TODAY)
    act(conn, b, world.owner, BookingEvent.PICKUP)
    act(conn, b, world.owner, BookingEvent.RETURN)
    return b


def test_reviews_are_blind_until_both_sides_write(conn, world):
    b = returned_loan(conn, world)
    social.submit_review(conn, booking_id=b, author_id=world.borrowers[0], rating=5, body="Great", now=NOW)
    assert social.visible_reviews(conn, user_id=world.owner, now=NOW) == []
    assert social.rating(conn, user_id=world.owner, now=NOW) == {"avg": None, "count": 0}
    social.submit_review(conn, booking_id=b, author_id=world.owner, rating=4, body="On time", now=NOW)
    assert [r["rating"] for r in social.visible_reviews(conn, user_id=world.owner, now=NOW)] == [5]
    assert social.rating(conn, user_id=world.borrowers[0], now=NOW) == {"avg": 4.0, "count": 1}


def test_a_one_sided_review_shows_after_fourteen_days(conn, world):
    b = returned_loan(conn, world)
    social.submit_review(conn, booking_id=b, author_id=world.borrowers[0], rating=3, body="", now=NOW)
    later = NOW + social.BLIND_WINDOW
    assert len(social.visible_reviews(conn, user_id=world.owner, now=later)) == 1


def test_review_rules(conn, world):
    b = approved_loan(conn, world, start=TODAY)
    with pytest.raises(social.ReviewNotAllowed):  # not returned yet
        social.submit_review(conn, booking_id=b, author_id=world.borrowers[0], rating=5, body="", now=NOW)
    act(conn, b, world.owner, BookingEvent.PICKUP)
    act(conn, b, world.owner, BookingEvent.RETURN)
    social.submit_review(conn, booking_id=b, author_id=world.borrowers[0], rating=5, body="", now=NOW)
    for author in (world.borrowers[0], world.borrowers[3]):  # twice, and a stranger
        with pytest.raises(social.ReviewNotAllowed):
            social.submit_review(conn, booking_id=b, author_id=author, rating=5, body="", now=NOW)
    subject = conn.execute("SELECT subject_id FROM reviews WHERE booking_id = %s", (b,)).fetchone()["subject_id"]
    assert subject == world.owner  # derived, not taken from the form


def test_thread_hides_pickup_until_approved(conn, world):
    b = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[0], start=TODAY, end=TODAY + timedelta(days=1), now=NOW
    )
    conn.execute("UPDATE listings SET pickup_detail = 'Blue door' WHERE id = %s", (world.listing,))
    assert social.booking_thread(conn, booking_id=b, user_id=world.borrowers[0])["pickup_detail"] is None
    repo.approve_booking(conn, booking_id=b, owner_id=world.owner, now=NOW)
    social.post_message(conn, booking_id=b, sender_id=world.owner, body="See you at 6", now=NOW)
    thread = social.booking_thread(conn, booking_id=b, user_id=world.borrowers[0])
    assert thread["pickup_detail"] == "Blue door" and [m["body"] for m in thread["messages"]] == ["See you at 6"]


def test_membership_toggles(conn, world):
    assert social.toggle_membership(conn, community_id=world.interest, user_id=world.owner) is True
    assert social.community(conn, slug="builders", user_id=world.owner)["joined"]
    assert social.toggle_membership(conn, community_id=world.interest, user_id=world.owner) is False


def test_looking_for_rolls_up_to_the_city(conn, world):
    social.post_looking_for(
        conn, author_id=world.owner, area_id=world.neighborhood, interest_id=None, title="Need a tent",
        body="", wanted=(TODAY, TODAY + timedelta(days=2)), now=NOW,
    )
    city_ids = repo.area_subtree_ids(conn, world.city)
    assert [p["title"] for p in social.looking_for(conn, area_ids=city_ids)] == ["Need a tent"]
    with pytest.raises(ValueError):
        social.post_looking_for(conn, author_id=world.owner, area_id=world.city, interest_id=None, title="x",
                                body="", wanted=None, now=NOW)


def test_borrower_view_marks_lapsed_requests_expired(conn, world):
    b = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[0], start=TODAY + timedelta(days=5),
        end=TODAY + timedelta(days=6), now=NOW,
    )
    rows = repo.borrower_bookings(conn, user_id=world.borrowers[0], now=NOW + timedelta(hours=49))
    assert [(r["id"], r["status"]) for r in rows] == [(b, "expired")]
