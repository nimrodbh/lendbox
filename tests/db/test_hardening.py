"""A-4: the fixes from docs/threat-model.md section 5, one test (or a few) per item."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from psycopg import errors as pg_errors

from lendbox.db import accounts, limits, repo, social
from lendbox.domain.booking import BookingEvent
from lendbox.domain.errors import NotFound, TooManyOpenRequests
from lendbox.domain.listing import InvalidListing, validate_listing
from lendbox.web.app import app, clock, csrf_protect

from .conftest import NOW

TODAY = NOW.date()
PW = "a long enough password"


def add_listing(conn, world, title="Ladder"):
    return conn.execute(
        """
        INSERT INTO listings (owner_id, category_id, area_id, title, condition, price_per_unit_agorot, deposit_agorot,
                              status)
        SELECT owner_id, category_id, area_id, %s, 'good', 0, 100, 'live' FROM listings LIMIT 1 RETURNING id
        """,
        (title,),
    ).fetchone()["id"]


def request(conn, borrower, listing, days_ahead=5):
    start = TODAY + timedelta(days=days_ahead)
    return repo.request_booking(
        conn, listing_id=listing, borrower_id=borrower, start=start, end=start + timedelta(days=1), now=NOW
    )


# 4. open-request caps ---------------------------------------------------------------------------------------


def test_one_open_request_per_item(conn, world):
    request(conn, world.borrowers[0], world.listing)
    with pytest.raises(TooManyOpenRequests):
        request(conn, world.borrowers[0], world.listing, days_ahead=20)  # other dates, same item


def test_at_most_five_open_requests_overall(conn, world):
    items = [add_listing(conn, world, f"Item {i}") for i in range(6)]
    for item in items[: repo.MAX_OPEN_REQUESTS]:
        request(conn, world.borrowers[0], item)
    with pytest.raises(TooManyOpenRequests):
        request(conn, world.borrowers[0], items[-1])


def test_expired_requests_do_not_count(conn, world):
    request(conn, world.borrowers[0], world.listing)
    later = NOW + timedelta(hours=49)
    start = TODAY + timedelta(days=10)
    assert repo.request_booking(conn, listing_id=world.listing, borrower_id=world.borrowers[0], start=start,
                                end=start + timedelta(days=1), now=later)


# 7. closed threads -------------------------------------------------------------------------------------------


def test_thread_closes_after_decline(conn, world):
    b = request(conn, world.borrowers[0], world.listing)
    social.post_message(conn, booking_id=b, sender_id=world.owner, body="Is it still free?", now=NOW)
    repo.decline_booking(conn, booking_id=b, owner_id=world.owner, now=NOW)
    with pytest.raises(NotFound):
        social.post_message(conn, booking_id=b, sender_id=world.borrowers[0], body="Why?!", now=NOW)


def test_thread_closes_fourteen_days_after_return(conn, world):
    start = TODAY
    b = repo.request_booking(conn, listing_id=world.listing, borrower_id=world.borrowers[0], start=start,
                             end=start + timedelta(days=1), now=NOW)
    repo.approve_booking(conn, booking_id=b, owner_id=world.owner, now=NOW)
    repo.act_on_booking(conn, booking_id=b, user_id=world.owner, event=BookingEvent.PICKUP, now=NOW)
    repo.act_on_booking(conn, booking_id=b, user_id=world.owner, event=BookingEvent.RETURN, now=NOW)
    social.post_message(conn, booking_id=b, sender_id=world.owner, body="Thanks!", now=NOW + timedelta(days=13))
    with pytest.raises(NotFound):
        social.post_message(conn, booking_id=b, sender_id=world.owner, body="Hi again", now=NOW + timedelta(days=15))


# 8. review farming -------------------------------------------------------------------------------------------


def returned(conn, world, borrower, days_ahead):
    start = TODAY + timedelta(days=days_ahead)
    b = repo.request_booking(conn, listing_id=world.listing, borrower_id=borrower, start=start,
                             end=start + timedelta(days=1), now=NOW)
    repo.approve_booking(conn, booking_id=b, owner_id=world.owner, now=NOW)
    later = NOW + timedelta(days=days_ahead)
    repo.act_on_booking(conn, booking_id=b, user_id=world.owner, event=BookingEvent.PICKUP, now=later)
    repo.act_on_booking(conn, booking_id=b, user_id=world.owner, event=BookingEvent.RETURN, now=later)
    return b


def test_repeat_reviews_from_one_author_count_once(conn, world):
    for i, rating in enumerate([5, 5, 5]):
        b = returned(conn, world, world.borrowers[0], i * 2)
        social.submit_review(conn, booking_id=b, author_id=world.borrowers[0], rating=rating, body="", now=NOW)
    b = returned(conn, world, world.borrowers[1], 10)
    social.submit_review(conn, booking_id=b, author_id=world.borrowers[1], rating=1, body="", now=NOW)
    later = NOW + timedelta(days=30)
    assert social.rating(conn, user_id=world.owner, now=later) == {"avg": 3.0, "count": 2}
    assert len(social.visible_reviews(conn, user_id=world.owner, now=later)) == 4  # all still readable


# 9. contact details in public text ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["call 054-1234567", "+972 54 123 4567", "03 612 3456", "write to me@x.co"])
def test_contact_details_are_kept_out_of_listings(text):
    form = {"title": "Drill", "description": text, "condition": "good", "price": "0", "deposit": "50", "area": "1"}
    with pytest.raises(InvalidListing) as err:
        validate_listing(form, area_ids={1}, interest_ids=set())
    assert err.value.key == "listing.error.contact"


def test_sizes_and_models_are_not_phone_numbers():
    form = {"title": "Desk 120x60", "description": "Model 2023, fits M8 bolts, 1234567 serial", "condition": "good",
            "price": "0", "deposit": "50", "area": "1"}
    assert validate_listing(form, area_ids={1}, interest_ids=set())


# 10. length bounds -------------------------------------------------------------------------------------------


def test_database_rejects_oversized_text(conn, world):
    with pytest.raises(pg_errors.CheckViolation):
        conn.execute("UPDATE listings SET description = repeat('x', 5001) WHERE id = %s", (world.listing,))


# 2. generic rate limits --------------------------------------------------------------------------------------


def test_rate_limit_budget_and_window(conn):
    lim = limits.Limit("test_bucket", 2, timedelta(minutes=10))
    limits.hit(conn, lim, "k", now=NOW)
    limits.hit(conn, lim, "k", now=NOW)
    with pytest.raises(limits.RateLimited):
        limits.hit(conn, lim, "k", now=NOW)
    limits.hit(conn, lim, "other key", now=NOW)
    limits.hit(conn, lim, "k", now=NOW + timedelta(minutes=11))


# 1. reports and the admin queue ------------------------------------------------------------------------------


def make_admin(conn, user_id):
    conn.execute("UPDATE users SET is_admin = true WHERE id = %s", (user_id,))


def test_hiding_a_listing_removes_it_and_resolves_every_report_on_it(conn, world):
    social.file_report(conn, reporter_id=world.borrowers[0], target_type="listing", target_id=world.listing,
                       reason="Fake photos", now=NOW)
    second = social.file_report(conn, reporter_id=world.borrowers[1], target_type="listing", target_id=world.listing,
                                reason="Asked for Bit upfront", now=NOW)
    make_admin(conn, world.borrowers[7])
    social.resolve_report(conn, report_id=second, admin_id=world.borrowers[7], action="hidden", now=NOW)
    assert repo.search_listings(conn) == []
    assert social.open_reports(conn) == []


def test_hiding_a_member_signs_them_out_and_hides_their_items(conn, world):
    raw = accounts.create_session(conn, user_id=world.owner, now=NOW)
    r = social.file_report(conn, reporter_id=world.borrowers[0], target_type="user", target_id=world.owner,
                           reason="Threats in messages", now=NOW)
    make_admin(conn, world.borrowers[7])
    social.resolve_report(conn, report_id=r, admin_id=world.borrowers[7], action="hidden", now=NOW)
    assert accounts.session_user(conn, raw_token=raw, now=NOW) is None
    assert repo.search_listings(conn) == []
    with pytest.raises(NotFound):
        repo.listing_detail(conn, listing_id=world.listing, today=TODAY)


def test_non_admin_cannot_resolve(conn, world):
    r = social.file_report(conn, reporter_id=world.borrowers[0], target_type="listing", target_id=world.listing,
                           reason="Spam", now=NOW)
    with pytest.raises(NotFound):
        social.resolve_report(conn, report_id=r, admin_id=world.borrowers[1], action="hidden", now=NOW)


# 6. headers, CSRF coverage, production guard; identical 404s ------------------------------------------------


@pytest.fixture
def client(dsn, monkeypatch):
    monkeypatch.setenv("LENDBOX_DATABASE_URL", dsn)
    app.dependency_overrides[clock] = lambda: NOW
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_security_headers_on_every_page(client, world):
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_every_post_route_checks_csrf():
    unguarded = [
        r.path for r in app.routes
        if "POST" in getattr(r, "methods", set())
        and not any(d.call is csrf_protect for d in r.dependant.dependencies)
    ]
    assert unguarded == []


def test_admin_area_is_invisible_to_members(client, conn, world):
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (accounts.hash_password(PW), world.owner))
    email = conn.execute("SELECT email FROM users WHERE id = %s", (world.owner,)).fetchone()["email"]
    client.get("/")
    client.post("/login", data={"email": email, "password": PW, "csrf_token": client.cookies["lb_csrf"]})
    assert client.get("/admin/reports").status_code == 404
    make_admin(conn, world.owner)
    assert client.get("/admin/reports").status_code == 200


def test_strangers_get_the_same_404_everywhere(client, conn, world):
    b = request(conn, world.borrowers[0], world.listing)
    stranger = world.borrowers[5]
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (accounts.hash_password(PW), stranger))
    email = conn.execute("SELECT email FROM users WHERE id = %s", (stranger,)).fetchone()["email"]
    client.get("/")
    client.post("/login", data={"email": email, "password": PW, "csrf_token": client.cookies["lb_csrf"]})
    token = client.cookies["lb_csrf"]
    assert client.get(f"/bookings/{b}").status_code == 404
    assert client.get(f"/bookings/{b}/review").status_code == 404
    assert client.get(f"/listings/{world.listing}/manage").status_code == 404
    for action in ("approve", "decline", "cancel", "pickup", "return"):
        r = client.post(f"/bookings/{b}/{action}", data={"csrf_token": token}, follow_redirects=False)
        assert r.headers["location"].endswith("error=error.forbidden"), action
    assert conn.execute("SELECT status FROM bookings WHERE id = %s", (b,)).fetchone()["status"] == "requested"


def test_production_refuses_insecure_cookies():
    env = {**os.environ, "LENDBOX_ENV": "production", "LENDBOX_SECURE_COOKIES": "0"}
    env.pop("LENDBOX_DEV_MAIL", None)
    cmd = [sys.executable, "-c", "import lendbox.web.app"]
    run = subprocess.run(cmd, check=False, env=env, capture_output=True, text=True)
    assert run.returncode != 0 and "LENDBOX_SECURE_COOKIES" in run.stderr


def test_hsts_only_when_served_over_https():
    code = "import lendbox.web.app as a; print(a.SECURITY_HEADERS.get('Strict-Transport-Security'))"
    secure = {**os.environ, "LENDBOX_SECURE_COOKIES": "1"}
    plain = {k: v for k, v in os.environ.items() if k != "LENDBOX_SECURE_COOKIES"}
    out = [subprocess.run([sys.executable, "-c", code], check=True, env=e, capture_output=True, text=True).stdout
           for e in (secure, plain)]
    assert out[0].startswith("max-age=") and out[1].strip() == "None"


def test_legal_pages_show_the_configured_contact(client):
    assert "contact@lendbox.example" in client.get("/privacy").text
