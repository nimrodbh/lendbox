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
    assert demo.ensure_fresh(conn, now=NOW, password="demo pw for tests")
    return conn


def test_world_loads_once_a_day(fresh):
    assert not demo.ensure_fresh(fresh, now=NOW + timedelta(hours=23), password="x")
    fresh.execute("UPDATE listings SET title = 'Vandalised' WHERE id = 1")
    assert demo.ensure_fresh(fresh, now=NOW + timedelta(hours=25), password="x")
    assert fresh.execute("SELECT title FROM listings WHERE id = 1").fetchone()["title"] != "Vandalised"


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
