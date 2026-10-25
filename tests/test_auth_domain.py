from __future__ import annotations

import pytest

from lendbox.domain.auth import InvalidSignup, hash_password, new_token, token_hash, validate_signup, verify_password


def test_password_round_trip():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$")
    assert verify_password("correct horse battery", stored)


def test_wrong_password_fails():
    assert not verify_password("wrong password!", hash_password("correct horse battery"))


def test_same_password_gets_different_salts():
    assert hash_password("correct horse battery") != hash_password("correct horse battery")


@pytest.mark.parametrize("stored", ["", "plain", "bcrypt$1$2$3$4$5", "scrypt$only$four$parts"])
def test_malformed_stored_hash_fails_closed(stored):
    assert not verify_password("anything at all", stored)


def test_token_hash_matches_new_token():
    raw, hashed = new_token()
    assert token_hash(raw) == hashed and len(raw) >= 40


def test_signup_normalizes_email_and_name():
    assert validate_signup("  Dana@Example.TEST ", "  Dana   K. ", "long enough pw") == ("dana@example.test", "Dana K.")


@pytest.mark.parametrize(
    ("email", "name", "password", "field"),
    [
        ("not-an-email", "Dana", "long enough pw", "email"),
        ("dana@example.test", "   ", "long enough pw", "display_name"),
        ("dana@example.test", "x" * 61, "long enough pw", "display_name"),
        ("dana@example.test", "Dana", "short", "password"),
    ],
)
def test_signup_rejects_bad_field(email, name, password, field):
    with pytest.raises(InvalidSignup) as err:
        validate_signup(email, name, password)
    assert err.value.field == field
