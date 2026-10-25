"""The core guarantee: a listing is never booked twice for the same day, even under concurrent approvals."""

from __future__ import annotations

import threading
from datetime import date, timedelta

import pytest
from psycopg import errors as pg_errors

from lendbox.db import repo
from lendbox.db.connection import connect
from lendbox.domain.booking import REQUEST_TTL
from lendbox.domain.errors import BelowMinimum, BookingConflict, BookingNotPending, Forbidden, NotFound

from .conftest import NOW

D = date(2026, 11, 12)


def request(conn, world, borrower: int, start: date, days: int) -> int:
    return repo.request_booking(
        conn, listing_id=world.listing, borrower_id=borrower, start=start, end=start + timedelta(days=days), now=NOW
    )


def statuses(conn, ids: list[int]) -> dict[int, str]:
    rows = conn.execute("SELECT id, status FROM bookings WHERE id = ANY(%s)", (ids,)).fetchall()
    return {r["id"]: r["status"] for r in rows}


def test_approve_declines_overlapping_requests_and_keeps_touching_ones(conn, world):
    nov12_14 = request(conn, world, world.borrowers[0], D, 3)
    nov14_15 = request(conn, world, world.borrowers[1], D + timedelta(days=2), 2)
    nov15_16 = request(conn, world, world.borrowers[2], D + timedelta(days=3), 2)  # touches, no overlap

    declined = repo.approve_booking(conn, booking_id=nov12_14, owner_id=world.owner, now=NOW)

    assert declined == [nov14_15]
    assert statuses(conn, [nov12_14, nov14_15, nov15_16]) == {
        nov12_14: "approved",
        nov14_15: "declined",
        nov15_16: "requested",
    }


@pytest.mark.parametrize("contenders", [2, 8])
def test_concurrent_approvals_of_overlapping_requests_approve_exactly_one(dsn, conn, world, contenders):
    ids = [request(conn, world, world.borrowers[i], D + timedelta(days=i % 2), 3) for i in range(contenders)]
    barrier = threading.Barrier(contenders)
    outcomes: dict[int, str] = {}

    def approve(booking_id: int) -> None:
        with connect(dsn) as own:
            barrier.wait()
            try:
                repo.approve_booking(own, booking_id=booking_id, owner_id=world.owner, now=NOW)
                outcomes[booking_id] = "approved"
            except (BookingNotPending, BookingConflict) as exc:
                outcomes[booking_id] = type(exc).__name__

    threads = [threading.Thread(target=approve, args=(i,)) for i in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert list(outcomes.values()).count("approved") == 1
    final = statuses(conn, ids)
    assert list(final.values()).count("approved") == 1
    assert set(final.values()) == {"approved", "declined"}


def test_exclusion_constraint_stops_overlap_even_without_the_listing_lock(dsn, conn, world):
    """Simulate a buggy writer that skips approve_booking: the database alone must refuse the second approval."""
    first = request(conn, world, world.borrowers[0], D, 3)
    second = request(conn, world, world.borrowers[1], D + timedelta(days=1), 3)
    second_result: list[BaseException | None] = []
    first_updated = threading.Event()

    with connect(dsn) as a, connect(dsn) as b:
        tx_a = a.transaction()
        tx_a.__enter__()
        a.execute("UPDATE bookings SET status = 'approved' WHERE id = %s", (first,))
        first_updated.set()

        def approve_second() -> None:
            try:
                with b.transaction():
                    b.execute("UPDATE bookings SET status = 'approved' WHERE id = %s", (second,))
                second_result.append(None)
            except BaseException as exc:  # noqa: BLE001  (we assert on the type below)
                second_result.append(exc)

        t = threading.Thread(target=approve_second)
        t.start()
        t.join(timeout=1)
        assert t.is_alive(), "the second writer must wait for the first transaction"
        tx_a.__exit__(None, None, None)  # commit the first approval
        t.join(timeout=10)

    assert isinstance(second_result[0], pg_errors.ExclusionViolation)
    assert statuses(conn, [first, second]) == {first: "approved", second: "requested"}


def test_owner_block_conflicts_with_approval(conn, world):
    conn.execute(
        "INSERT INTO bookings (listing_id, kind, during, status) VALUES (%s, 'block', daterange(%s, %s), 'approved')",
        (world.listing, D, D + timedelta(days=1)),
    )
    booking = request(conn, world, world.borrowers[0], D, 2)
    with pytest.raises(BookingConflict):
        repo.approve_booking(conn, booking_id=booking, owner_id=world.owner, now=NOW)
    assert statuses(conn, [booking]) == {booking: "requested"}


def test_only_the_owner_can_approve(conn, world):
    booking = request(conn, world, world.borrowers[0], D, 2)
    with pytest.raises(Forbidden):
        repo.approve_booking(conn, booking_id=booking, owner_id=world.borrowers[1], now=NOW)


def test_approve_after_expiry_is_rejected(conn, world):
    booking = request(conn, world, world.borrowers[0], D, 2)
    with pytest.raises(BookingNotPending):
        repo.approve_booking(conn, booking_id=booking, owner_id=world.owner, now=NOW + REQUEST_TTL)


def test_expire_due_bookings_closes_only_lapsed_requests(conn, world):
    old = request(conn, world, world.borrowers[0], D, 2)
    later = NOW + timedelta(hours=1)
    fresh = repo.request_booking(
        conn, listing_id=world.listing, borrower_id=world.borrowers[1], start=D, end=D + timedelta(days=2), now=later
    )
    assert repo.expire_due_bookings(conn, now=NOW + REQUEST_TTL) == [old]
    assert statuses(conn, [old, fresh]) == {old: "expired", fresh: "requested"}


def test_owner_cannot_request_own_listing(conn, world):
    with pytest.raises(Forbidden):
        request(conn, world, world.owner, D, 2)


def test_draft_listing_cannot_be_requested(conn, world):
    conn.execute("UPDATE listings SET status = 'draft' WHERE id = %s", (world.listing,))
    with pytest.raises(NotFound):
        request(conn, world, world.borrowers[0], D, 2)


def test_request_below_minimum_days_is_rejected(conn, world):
    conn.execute("UPDATE listings SET min_units = 3 WHERE id = %s", (world.listing,))
    with pytest.raises(BelowMinimum):
        request(conn, world, world.borrowers[0], D, 2)


def test_request_freezes_the_day_price(conn, world):
    booking = request(conn, world, world.borrowers[0], D, 3)
    row = conn.execute("SELECT rental_agorot, deposit_agorot FROM bookings WHERE id = %s", (booking,)).fetchone()
    assert (row["rental_agorot"], row["deposit_agorot"]) == (4500, 20000)
