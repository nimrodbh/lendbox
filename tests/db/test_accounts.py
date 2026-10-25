from __future__ import annotations

from datetime import timedelta

import pytest

from lendbox.db import accounts
from lendbox.domain.errors import EmailTaken, InvalidCredentials, InvalidToken, TooManyAttempts

from .conftest import NOW

PW = "a long enough password"


def sign_up(conn, email="dana@example.test"):
    """Sign up and capture the confirmation link the way the mail sender would see it."""
    user_id = accounts.sign_up(conn, email=email, display_name="Dana K.", password=PW, now=NOW)
    outbox: list[str] = []
    accounts.deliver_outbox(conn, send=lambda to, subject, body: outbox.append(body), now=NOW, base_url="http://x")
    raw = outbox[-1].split("token=")[1] if outbox else ""
    return user_id, raw


def test_sign_up_creates_unverified_user_and_queues_mail_without_a_token(conn):
    user_id = accounts.sign_up(conn, email="dana@example.test", display_name="Dana K.", password=PW, now=NOW)
    user = conn.execute("SELECT email_verified_at FROM users WHERE id = %s", (user_id,)).fetchone()
    mail = conn.execute("SELECT to_email, body, sent_at FROM email_outbox").fetchall()
    assert user["email_verified_at"] is None
    assert [(m["to_email"], m["body"], m["sent_at"]) for m in mail] == [("dana@example.test", None, None)]
    assert conn.execute("SELECT count(*) AS n FROM email_tokens").fetchone()["n"] == 0


def test_delivery_mints_the_token_at_send_time_and_stores_no_link(conn):
    _, raw = sign_up(conn)
    assert raw and accounts.verify_email(conn, raw_token=raw, now=NOW)
    row = conn.execute("SELECT body, sent_at FROM email_outbox").fetchone()
    assert row["body"] is None and row["sent_at"] == NOW


def test_failed_send_leaves_the_mail_queued_and_no_token(conn):
    accounts.sign_up(conn, email="dana@example.test", display_name="Dana K.", password=PW, now=NOW)

    def broken(to, subject, body):
        raise OSError("smtp down")

    with pytest.raises(OSError):
        accounts.deliver_outbox(conn, send=broken, now=NOW, base_url="http://x")
    assert conn.execute("SELECT sent_at FROM email_outbox").fetchone()["sent_at"] is None
    assert conn.execute("SELECT count(*) AS n FROM email_tokens").fetchone()["n"] == 0


def test_email_is_unique_regardless_of_case(conn):
    sign_up(conn)
    with pytest.raises(EmailTaken):
        sign_up(conn, "DANA@example.test")
    assert conn.execute("SELECT count(*) AS n FROM email_outbox").fetchone()["n"] == 1  # no mail for the failure


def test_verification_link_works_once(conn):
    user_id, raw = sign_up(conn)
    assert accounts.verify_email(conn, raw_token=raw, now=NOW) == user_id
    with pytest.raises(InvalidToken):
        accounts.verify_email(conn, raw_token=raw, now=NOW)


def test_expired_verification_link_is_rejected(conn):
    _, raw = sign_up(conn)
    with pytest.raises(InvalidToken):
        accounts.verify_email(conn, raw_token=raw, now=NOW + accounts.VERIFY_TTL)


def test_login_with_right_and_wrong_password(conn):
    user_id, _ = sign_up(conn)
    assert accounts.authenticate(conn, email=" Dana@Example.test", password=PW, now=NOW) == user_id
    with pytest.raises(InvalidCredentials):
        accounts.authenticate(conn, email="dana@example.test", password="nope nope nope", now=NOW)
    with pytest.raises(InvalidCredentials):
        accounts.authenticate(conn, email="nobody@example.test", password=PW, now=NOW)


def test_login_is_throttled_after_five_failures_then_recovers(conn):
    sign_up(conn)
    for _ in range(accounts.MAX_FAILURES):
        with pytest.raises(InvalidCredentials):
            accounts.authenticate(conn, email="dana@example.test", password="wrong guess!", now=NOW, ip="1.1.1.1")
    with pytest.raises(TooManyAttempts):  # even the right password waits, from that machine
        accounts.authenticate(conn, email="dana@example.test", password=PW, now=NOW, ip="1.1.1.1")
    later = NOW + accounts.FAILURE_WINDOW + timedelta(seconds=1)
    assert accounts.authenticate(conn, email="dana@example.test", password=PW, now=later, ip="1.1.1.1")


def test_an_attacker_cannot_lock_the_owner_out_from_one_machine(conn):
    """Threat model, threat 11: failures from the attacker's IP do not block the real owner elsewhere."""
    user_id, _ = sign_up(conn)
    for _ in range(accounts.MAX_FAILURES):
        with pytest.raises(InvalidCredentials):
            accounts.authenticate(conn, email="dana@example.test", password="wrong guess!", now=NOW, ip="6.6.6.6")
    assert accounts.authenticate(conn, email="dana@example.test", password=PW, now=NOW, ip="2.2.2.2") == user_id


def test_one_machine_cannot_try_many_emails(conn):
    """Threat model, threat 10: the per-IP budget stops credential stuffing across different emails."""
    for i in range(accounts.MAX_FAILURES_PER_IP):
        with pytest.raises(InvalidCredentials):
            accounts.authenticate(conn, email=f"victim{i}@example.test", password="leaked pw!", now=NOW, ip="6.6.6.6")
    with pytest.raises(TooManyAttempts):
        accounts.authenticate(conn, email="fresh@example.test", password="leaked pw!", now=NOW, ip="6.6.6.6")


def test_ips_are_stored_only_as_hashes(conn):
    with pytest.raises(InvalidCredentials):
        accounts.authenticate(conn, email="x@example.test", password="nope nope", now=NOW, ip="203.0.113.7")
    row = conn.execute("SELECT encode(ip_hash, 'escape') AS h FROM login_failures").fetchone()
    assert "203.0.113.7" not in row["h"]


def test_session_lifecycle(conn):
    user_id, _ = sign_up(conn)
    raw = accounts.create_session(conn, user_id=user_id, now=NOW)
    assert accounts.session_user(conn, raw_token=raw, now=NOW)["id"] == user_id
    assert accounts.session_user(conn, raw_token=raw, now=NOW + accounts.SESSION_TTL) is None
    accounts.end_session(conn, raw_token=raw)
    assert accounts.session_user(conn, raw_token=raw, now=NOW) is None
    assert accounts.session_user(conn, raw_token="forged", now=NOW) is None


def test_only_token_hashes_are_stored(conn):
    user_id, raw = sign_up(conn)
    session = accounts.create_session(conn, user_id=user_id, now=NOW)
    stored = conn.execute(
        "SELECT encode(token_hash, 'escape') AS h FROM sessions UNION ALL "
        "SELECT encode(token_hash, 'escape') FROM email_tokens"
    ).fetchall()
    assert all(raw not in r["h"] and session not in r["h"] for r in stored)
