"""Domain errors. Every failure the domain or repository reports is a LendboxError subclass,
so the web layer can map them to user-facing messages in one place."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lendbox.domain.booking import Actor, BookingEvent, BookingStatus


class LendboxError(Exception):
    """Base for all Lendbox domain errors."""


class NotFound(LendboxError):
    """The row does not exist, or the caller may not see it."""


class Forbidden(LendboxError):
    """The caller is not allowed to perform this action."""


class BookingConflict(LendboxError):
    """The dates overlap an approved or active booking (PostgreSQL SQLSTATE 23P01)."""


class BookingNotPending(LendboxError):
    """The booking is no longer waiting for an answer: approved, declined, expired or cancelled meanwhile."""


class InvalidRange(LendboxError):
    """The date range is empty or reversed."""


class InvalidTransition(LendboxError):
    """The requested state change is not allowed."""

    def __init__(self, status: BookingStatus, event: BookingEvent, actor: Actor) -> None:
        self.status = status
        self.event = event
        self.actor = actor
        super().__init__(status, event, actor)


class BelowMinimum(LendboxError):
    """The requested duration is below the listing minimum."""

    def __init__(self, units: int, min_units: int) -> None:
        self.units = units
        self.min_units = min_units
        super().__init__(units, min_units)


class NotWholeMonths(LendboxError):
    """The range does not span whole calendar months."""


class EmailTaken(LendboxError):
    """An account with this email already exists."""


class InvalidCredentials(LendboxError):
    """Email or password is wrong. Deliberately does not say which."""


class TooManyAttempts(LendboxError):
    """Too many failed logins for this email; try again later."""


class InvalidToken(LendboxError):
    """A verification link is unknown, used or expired."""


class NotVerified(LendboxError):
    """The action needs a verified email."""


class TooManyOpenRequests(LendboxError):
    """The borrower already has an open request on this item, or too many open requests overall."""
