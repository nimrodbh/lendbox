"""Public demo mode: one-click accounts, no sign-up, a daily reset."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from lendbox.db import demo
from lendbox.web import app as web

from .conftest import NOW


@pytest.fixture
def fresh(conn):
    conn.execute("DELETE FROM demo_state")
    assert demo.ensure_fresh(conn, now=NOW, password="demo pw for tests") == NOW
    return conn


def test_world_loads_once_a_day(fresh):
    assert demo.ensure_fresh(fresh, now=NOW + timedelta(hours=23), password="x") == NOW  # still fresh
    fresh.execute("UPDATE listings SET title = 'Vandalised' WHERE id = 1")
    later = NOW + timedelta(hours=25)
    assert demo.ensure_fresh(fresh, now=later, password="x") == later
    assert fresh.execute("SELECT title FROM listings WHERE id = 1").fetchone()["title"] != "Vandalised"


def test_startup_check_never_resets_a_stale_world(fresh):
    """A visitor who wakes the server must not land in the middle of a reset."""
    fresh.execute("UPDATE listings SET title = 'Kept' WHERE id = 1")
    assert demo.ensure_fresh(fresh, now=NOW + timedelta(days=3), missing_only=True, password="x") == NOW
    assert fresh.execute("SELECT title FROM listings WHERE id = 1").fetchone()["title"] == "Kept"


def test_ping_needs_no_database(monkeypatch):
    monkeypatch.setenv("LENDBOX_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    with TestClient(web.app) as c:
        assert c.get("/ping").json() == {"ok": True}


START = NOW
QUIET = web.QUIET_BEFORE_RESET


@pytest.mark.parametrize(
    ("idle", "now", "next_reset", "expected"),
    [
        (timedelta(minutes=1), START, START, (True, False)),             # visitors around: routine, no reset
        (timedelta(minutes=30), START, START, (False, True)),            # quiet and due: reset, nothing else
        (timedelta(minutes=30), START, START + timedelta(hours=1), (False, False)),  # quiet, not due: sleep
        (timedelta(hours=5), START, None, (False, True)),                # just started: check (missing only)
        (timedelta(minutes=10), START, START, (True, False)),            # due, but a visitor was here recently
        (QUIET - timedelta(seconds=1), START, START, (False, False)),     # due, routine over, not quiet enough
    ],
)
def test_housekeeping_plan(idle, now, next_reset, expected):
    assert web.plan_housekeeping(idle=idle, now=now, next_reset=next_reset, demo=True) == expected


def test_housekeeping_plan_outside_demo():
    assert web.plan_housekeeping(idle=timedelta(hours=1), now=START, next_reset=None, demo=False) == (False, False)


def test_demo_accounts_have_something_to_show(fresh):
    lender = demo.demo_user(fresh, role="lender", now=NOW)
    pending = fresh.execute(
        "SELECT count(*) AS n FROM bookings b JOIN listings l ON l.id = b.listing_id "
        "WHERE l.owner_id = %s AND b.status = 'requested'",
        (lender,),
    ).fetchone()["n"]
    borrower = demo.demo_user(fresh, role="borrower", now=NOW)
    admin = fresh.execute("SELECT is_admin FROM users WHERE id = ANY(%s)", ([lender, borrower],)).fetchall()
    assert pending > 0 and lender != borrower and not any(r["is_admin"] for r in admin)


@pytest.fixture
def client(dsn, fresh, monkeypatch):
    monkeypatch.setenv("LENDBOX_DATABASE_URL", dsn)
    monkeypatch.setattr(web, "DEMO", True)
    web.app.dependency_overrides[web.clock] = lambda: NOW
    with TestClient(web.app) as c:
        yield c
    web.app.dependency_overrides.clear()


def test_one_click_lender_lands_in_their_inbox(client):
    client.get("/")
    r = client.post("/demo/lender", data={"csrf_token": client.cookies["lb_csrf"]}, follow_redirects=False)
    assert r.headers["location"] == "/lending"
    assert "Requests (" in client.get("/lending").text


def test_signup_is_off_and_the_banner_shows(client):
    page = client.get("/signup").text
    assert "turned off in the demo" in page and "This is a demo" in page
    client.get("/")
    r = client.post("/signup", data={"email": "real@person.test", "display_name": "Real", "password": "a long password",
                                     "csrf_token": client.cookies["lb_csrf"]})
    assert r.status_code == 403


def test_demo_login_does_not_exist_outside_demo_mode(client, monkeypatch):
    monkeypatch.setattr(web, "DEMO", False)
    client.get("/")
    r = client.post("/demo/lender", data={"csrf_token": client.cookies["lb_csrf"]}, follow_redirects=False)
    assert r.status_code == 404


def test_unknown_demo_role_is_404(client):
    client.get("/")
    assert client.post("/demo/admin", data={"csrf_token": client.cookies["lb_csrf"]}).status_code == 404
