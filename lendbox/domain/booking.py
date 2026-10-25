"""Half-open ranges allow adjacent bookings without overlap.

The state machine takes its clock and dates as inputs so rules remain deterministic
and independent of storage, network access, and transaction handling.
"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from lendbox.domain.errors import BelowMinimum, InvalidRange, InvalidTransition, NotWholeMonths


class BookingStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    DECLINED = "declined"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ACTIVE = "active"
    RETURNED = "returned"


class BookingEvent(StrEnum):
    APPROVE = "approve"
    DECLINE = "decline"
    EXPIRE = "expire"
    CANCEL = "cancel"
    PICKUP = "pickup"
    RETURN = "return"


class Actor(StrEnum):
    OWNER = "owner"
    BORROWER = "borrower"
    SYSTEM = "system"


class BookingUnit(StrEnum):
    DAY = "day"
    NIGHT = "night"
    MONTH = "month"


REQUEST_TTL = timedelta(hours=48)
LOCAL_TZ = ZoneInfo("Asia/Jerusalem")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class DateRange:
    """A nonempty interval whose end is exclusive."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if not self.start < self.end:
            raise InvalidRange(f"start {self.start} must be before end {self.end}")

    @classmethod
    def from_inclusive(cls, first_day: date, last_day: date) -> DateRange:
        """Convert inclusive display dates to a storage interval."""
        return cls(first_day, last_day + timedelta(days=1))

    @property
    def last_day(self) -> date:
        """Return the final included date."""
        return self.end - timedelta(days=1)

    @property
    def days(self) -> int:
        """Return the number of included dates."""
        return (self.end - self.start).days

    def overlaps(self, other: DateRange) -> bool:
        """Check whether the ranges share any included date."""
        return self.start < other.end and other.start < self.end

    def contains(self, day: date) -> bool:
        """Check membership, excluding the end date."""
        return self.start <= day < self.end


@dataclass(frozen=True)
class Quote:
    """Integer amounts due for the requested duration."""

    unit: BookingUnit
    units: int
    rental_agorot: int
    deposit_agorot: int
    due_at_pickup_agorot: int


def _require_aware(value: datetime) -> None:
    if value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")


def expires_at_for(requested_at: datetime) -> datetime:
    """Return the request deadline, requiring an aware timestamp."""
    _require_aware(requested_at)
    return requested_at + REQUEST_TTL


def transition(
    status: BookingStatus, event: BookingEvent, actor: Actor, *,
    now: datetime, expires_at: datetime | None, starts_on: date,
) -> BookingStatus:
    """Validate an event and return its resulting status."""
    _require_aware(now)
    today = now.astimezone(LOCAL_TZ).date()
    match status, event, actor:
        case BookingStatus.REQUESTED, BookingEvent.APPROVE, Actor.OWNER:
            if expires_at is not None:
                _require_aware(expires_at)
                if now < expires_at:
                    return BookingStatus.APPROVED
        case BookingStatus.REQUESTED, BookingEvent.DECLINE, Actor.OWNER:
            return BookingStatus.DECLINED
        case BookingStatus.REQUESTED, BookingEvent.EXPIRE, Actor.SYSTEM:
            if expires_at is not None:
                _require_aware(expires_at)
                if now >= expires_at:
                    return BookingStatus.EXPIRED
        case BookingStatus.REQUESTED, BookingEvent.CANCEL, Actor.BORROWER:
            return BookingStatus.CANCELLED
        case BookingStatus.APPROVED, BookingEvent.CANCEL, Actor.OWNER:
            # Any time before pickup: a borrower who never shows up must not hold the dates (threat model, 3).
            return BookingStatus.CANCELLED
        case BookingStatus.APPROVED, BookingEvent.CANCEL, Actor.BORROWER:
            if today < starts_on:
                return BookingStatus.CANCELLED
        case BookingStatus.APPROVED, BookingEvent.PICKUP, Actor.OWNER:
            if today >= starts_on:
                return BookingStatus.ACTIVE
        case BookingStatus.ACTIVE, BookingEvent.RETURN, Actor.OWNER:
            return BookingStatus.RETURNED
    raise InvalidTransition(status, event, actor)


def _add_months(start: date, months: int) -> date:
    year, month_zero = divmod(start.year * 12 + start.month - 1 + months, 12)
    month = month_zero + 1
    return date(year, month, min(start.day, monthrange(year, month)[1]))


def quote(
    unit: BookingUnit, price_per_unit_agorot: int, deposit_agorot: int,
    rng: DateRange, min_units: int = 1,
) -> Quote:
    """Price days, nights, or whole months from the original start date."""
    if price_per_unit_agorot < 0 or deposit_agorot <= 0 or min_units < 1:
        raise ValueError("price must be >= 0, deposit > 0 and min_units >= 1")
    if unit == BookingUnit.MONTH:
        units = (rng.end.year - rng.start.year) * 12 + rng.end.month - rng.start.month
        if units < 1 or _add_months(rng.start, units) != rng.end:
            raise NotWholeMonths(f"{rng.start} to {rng.end} is not a whole number of months")
    elif unit in (BookingUnit.DAY, BookingUnit.NIGHT):
        units = rng.days
    else:
        raise ValueError(f"unknown booking unit {unit!r}")
    if units < min_units:
        raise BelowMinimum(units, min_units)
    rental_agorot = units * price_per_unit_agorot
    return Quote(unit, units, rental_agorot, deposit_agorot, rental_agorot + deposit_agorot)


def conflicts(requested: DateRange, taken: Iterable[DateRange]) -> list[DateRange]:
    """Return overlapping ranges in date order, retaining duplicates."""
    return sorted((rng for rng in taken if requested.overlaps(rng)), key=lambda rng: (rng.start, rng.end))


def to_auto_decline(approved: DateRange, pending: Mapping[int, DateRange]) -> set[int]:
    """Select pending booking ids that overlap the approved range."""
    return {booking_id for booking_id, rng in pending.items() if approved.overlaps(rng)}


def format_range(rng: DateRange) -> str:
    """Render inclusive dates using fixed English month abbreviations."""
    first, last = rng.start, rng.last_day
    start_text = f"{_MONTHS[first.month - 1]} {first.day}"
    end_text = f"{_MONTHS[last.month - 1]} {last.day}"
    if first == last:
        return start_text
    if first.year != last.year:
        return f"{start_text}, {first.year} to {end_text}, {last.year}"
    if first.month == last.month:
        return f"{start_text} to {last.day}"
    return f"{start_text} to {end_text}"
