"""End to end through HTTP: sign up, verify, log in, find an item, request dates, the owner approves."""

from __future__ import annotations

import html
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from lendbox.db import accounts
from lendbox.web.app import app, clock

from .conftest import NOW

PW = "a long enough password"
START = date(2026, 11, 12)


@pytest.fixture
def client(dsn, monkeypatch):
    monkeypatch.setenv("LENDBOX_DATABASE_URL", dsn)
    app.dependency_overrides[clock] = lambda: NOW
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def csrf(client) -> str:
    if "lb_csrf" not in client.cookies:
        client.get("/")
    return client.cookies["lb_csrf"]


def sign_up_and_verify(client, conn, email):
    r = client.post(
        "/signup", data={"email": email, "display_name": "Ron T.", "password": PW, "csrf_token": csrf(client)}
    )
    assert r.status_code == 200 and "Check your email" in r.text
    sent: list[str] = []
    accounts.deliver_outbox(conn, send=lambda to, subject, body: sent.append(body), now=NOW, base_url="http://x")
    token = re.search(r"token=([\w-]+)", sent[-1]).group(1)
    assert client.get(f"/verify?token={token}", follow_redirects=False).headers["location"] == "/login?msg=verified"


def log_in(client, email, password=PW, next_url="/browse"):
    return client.post(
        "/login",
        data={"email": email, "password": password, "next": next_url, "csrf_token": csrf(client)},
        follow_redirects=False,
    )


def test_full_borrowing_flow(client, conn, world):
    sign_up_and_verify(client, conn, "ron@example.test")
    assert log_in(client, "ron@example.test").headers["location"] == "/browse"

    page = client.get("/browse", params={"q": "drill"})
    assert "Cordless drill" in page.text

    item = client.get(f"/items/{world.listing}")
    assert "The exact spot is shared after approval" in item.text
    r = client.post(
        f"/items/{world.listing}/requests",
        data={"start": START.isoformat(), "last_day": (START + timedelta(days=2)).isoformat(),
              "message": "For two shelves", "csrf_token": csrf(client)},
        follow_redirects=False,
    )
    assert r.headers["location"] == f"/items/{world.listing}?msg=request_sent"

    # the owner logs in (in a separate browser) and approves
    conn.execute(
        "UPDATE users SET password_hash = %s, email_verified_at = now() WHERE id = %s",
        (accounts.hash_password(PW), world.owner),
    )
    owner_email = conn.execute("SELECT email FROM users WHERE id = %s", (world.owner,)).fetchone()["email"]
    with TestClient(app) as owner:
        log_in(owner, owner_email)
        inbox = owner.get("/lending")
        assert "Ron T." in inbox.text and "Nov 12 to 14" in inbox.text
        booking_id = conn.execute("SELECT id FROM bookings WHERE status = 'requested'").fetchone()["id"]
        r = owner.post(f"/bookings/{booking_id}/approve", data={"csrf_token": csrf(owner)}, follow_redirects=False)
        assert r.headers["location"] == "/lending?msg=approved"

    assert "Nov 12 to 14" in client.get(f"/items/{world.listing}").text  # now listed as booked
    assert "shared after approval" not in client.get(f"/items/{world.listing}").text  # pickup now visible


def test_post_without_csrf_token_is_rejected(client, conn, world):
    r = client.post("/signup", data={"email": "x@example.test", "display_name": "X", "password": PW})
    assert r.status_code == 403
    assert conn.execute("SELECT count(*) AS n FROM users WHERE email = 'x@example.test'").fetchone()["n"] == 0


def test_unverified_user_cannot_request(client, conn, world):
    client.post(
        "/signup", data={"email": "new@example.test", "display_name": "New", "password": PW, "csrf_token": csrf(client)}
    )
    log_in(client, "new@example.test")
    r = client.post(
        f"/items/{world.listing}/requests",
        data={"start": START.isoformat(), "last_day": START.isoformat(), "csrf_token": csrf(client)},
        follow_redirects=False,
    )
    assert r.headers["location"].endswith("error=error.not_verified")
    assert conn.execute("SELECT count(*) AS n FROM bookings").fetchone()["n"] == 0


def test_lending_requires_login(client):
    r = client.get("/lending", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "javascript:alert(1)"])
def test_login_never_redirects_off_site(client, conn, world, target):
    sign_up_and_verify(client, conn, "ron@example.test")
    assert log_in(client, "ron@example.test", next_url=target).headers["location"] == "/browse"


def test_wrong_password_shows_generic_error(client, conn, world):
    sign_up_and_verify(client, conn, "ron@example.test")
    r = log_in(client, "ron@example.test", password="not my password")
    assert r.status_code == 401 and "don't match" in html.unescape(r.text)


def test_flash_shows_only_catalog_messages(client):
    assert "evil" not in client.get("/", params={"msg": "<script>evil</script>"}).text


def test_pages_render(client, world):
    for path in ["/", "/browse", "/signup", "/login", f"/items/{world.listing}", "/terms", "/privacy", "/guidelines"]:
        assert client.get(path).status_code == 200, path
    assert client.get("/items/999999").status_code == 404


def verified_owner_client(conn, world):
    conn.execute(
        "UPDATE users SET password_hash = %s, email_verified_at = now() WHERE id = %s",
        (accounts.hash_password(PW), world.owner),
    )
    email = conn.execute("SELECT email FROM users WHERE id = %s", (world.owner,)).fetchone()["email"]
    c = TestClient(app)
    log_in(c, email)
    return c


def test_list_an_item_through_the_form_then_publish(client, conn, world):
    owner = verified_owner_client(conn, world)
    cat = conn.execute("SELECT id FROM categories LIMIT 1").fetchone()["id"]
    assert "Cordless" not in owner.get(f"/listings/new?category={cat}").text  # empty form
    bad = owner.post("/listings/new", data={
        "csrf_token": csrf(owner), "category": cat, "title": "Ladder", "condition": "good", "price": "0",
        "deposit": "", "area": world.city, "min_units": "1",
    })
    assert bad.status_code == 422 and "Add a deposit" in bad.text
    ok = owner.post("/listings/new", data={
        "csrf_token": csrf(owner), "category": cat, "title": "Ladder", "condition": "good", "price": "0",
        "deposit": "100", "area": world.city, "min_units": "1", "interests": [str(world.interest)],
    }, follow_redirects=False)
    manage = ok.headers["location"]
    listing_id = int(manage.split("/")[2])
    r = owner.post(f"/listings/{listing_id}/publish", data={"csrf_token": csrf(owner)}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=published")
    assert "Ladder" in client.get("/c/builders").text


def test_thread_messages_route_is_not_shadowed(client, conn, world):
    from lendbox.db import repo

    owner = verified_owner_client(conn, world)
    b = repo.request_booking(conn, listing_id=world.listing, borrower_id=world.borrowers[0],
                             start=START, end=START + timedelta(days=1), now=NOW)
    r = owner.post(f"/bookings/{b}/messages", data={"csrf_token": csrf(owner), "body": "Hi there"},
                   follow_redirects=False)
    assert r.status_code == 303 and "Hi there" in owner.get(f"/bookings/{b}").text


def test_new_pages_render_for_a_signed_in_user(client, conn, world):
    owner = verified_owner_client(conn, world)
    for path in ["/borrowing", "/lending?tab=out", "/lending?tab=items", "/listings/new", "/looking-for",
                 "/c/builders", "/c/tel-aviv-yafo", f"/listings/{world.listing}/manage"]:
        assert owner.get(path).status_code == 200, path
    assert owner.get("/c/nope").status_code == 404
