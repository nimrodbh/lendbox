"""Password hashing, tokens and sign-up validation. Pure: no I/O, no clock.

Passwords use scrypt from the standard library (memory-hard, no extra dependency). The stored string carries
its own parameters, so they can be raised later and old hashes still verify. Tokens are 32 random bytes;
only their SHA-256 goes to the database.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

from lendbox.domain.errors import LendboxError

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
MIN_PASSWORD_LENGTH = 10
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidSignup(LendboxError):
    """A sign-up field failed validation. `field` names it; `key` is the i18n message key."""

    def __init__(self, field: str, key: str) -> None:
        self.field = field
        self.key = key
        super().__init__(field, key)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    expected = base64.b64decode(digest)
    actual = hashlib.scrypt(
        password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected)
    )
    return hmac.compare_digest(actual, expected)


# Verifying against this when the email is unknown keeps "no such user" as slow as "wrong password",
# so response time does not reveal which emails have accounts.
DUMMY_HASH = hash_password("not a real password", salt=b"\x00" * 16)


def new_token() -> tuple[str, bytes]:
    """A fresh token: (the raw value for the cookie or link, its hash for the database)."""
    raw = secrets.token_urlsafe(32)
    return raw, token_hash(raw)


def token_hash(raw: str) -> bytes:
    return hashlib.sha256(raw.encode()).digest()


def validate_signup(email: str, display_name: str, password: str) -> tuple[str, str]:
    """Return the normalized (email, display_name), or raise InvalidSignup for the first bad field."""
    email = email.strip().lower()
    display_name = " ".join(display_name.split())
    if not _EMAIL.match(email) or len(email) > 254:
        raise InvalidSignup("email", "signup.error.email")
    if not 1 <= len(display_name) <= 60:
        raise InvalidSignup("display_name", "signup.error.name")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise InvalidSignup("password", "signup.error.password")
    return email, display_name
