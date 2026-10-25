"""Exercise every permitted triple and the date and money boundaries."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta, tzinfo
from itertools import product

import pytest

from lendbox.domain.booking import (
    LOCAL_TZ,
    REQUEST_TTL,
    Actor,
    BookingEvent,
    BookingStatus,
    BookingUnit,
    DateRange,
    Quote,
    conflicts,
    expires_at_for,
    format_range,
    quote,
    to_auto_decline,
    transition,
)
from lendbox.domain.errors import (
    BelowMinimum,
    BookingConflict,
    BookingNotPending,
    Forbidden,
    InvalidRange,
    InvalidTransition,
    LendboxError,
    NotFound,
    NotWholeMonths,
)

_NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)
_START = date(2026, 11, 12)
_ALLOWED = {
    (BookingStatus.REQUESTED, BookingEvent.APPROVE, Actor.OWNER): BookingStatus.APPROVED,
    (BookingStatus.REQUESTED, BookingEvent.DECLINE, Actor.OWNER): BookingStatus.DECLINED,
    (BookingStatus.REQUESTED, BookingEvent.EXPIRE, Actor.SYSTEM): BookingStatus.EXPIRED,
    (BookingStatus.REQUESTED, BookingEvent.CANCEL, Actor.BORROWER): BookingStatus.CANCELLED,
    (BookingStatus.APPROVED, BookingEvent.CANCEL, Actor.OWNER): BookingStatus.CANCELLED,
    (BookingStatus.APPROVED, BookingEvent.CANCEL, Actor.BORROWER): BookingStatus.CANCELLED,
    (BookingStatus.APPROVED, BookingEvent.PICKUP, Actor.OWNER): BookingStatus.ACTIVE,
    (BookingStatus.ACTIVE, BookingEvent.RETURN, Actor.OWNER): BookingStatus.RETURNED,
}


def _range(first: int, end: int) -> DateRange:
    return DateRange(date(2026, 11, first), date(2026, 11, end))


@pytest.mark.parametrize("status,event,actor", list(product(BookingStatus, BookingEvent, Actor)))
def test_full_transition_matrix(status: BookingStatus, event: BookingEvent, actor: Actor) -> None:
    expiry = _NOW if event == BookingEvent.EXPIRE else _NOW + timedelta(hours=1)
    starts = _START + timedelta(days=1) if event == BookingEvent.CANCEL else _START
    expected = _ALLOWED.get((status, event, actor))
    if expected is None:
        with pytest.raises(InvalidTransition) as caught:
            transition(status, event, actor, now=_NOW, expires_at=expiry, starts_on=starts)
        assert (caught.value.status, caught.value.event, caught.value.actor) == (status, event, actor)
    else:
        assert transition(status, event, actor, now=_NOW, expires_at=expiry, starts_on=starts) == expected


@pytest.mark.parametrize("event,offset,expected", [
    (BookingEvent.APPROVE, -1, BookingStatus.APPROVED),
    (BookingEvent.APPROVE, 0, None),
    (BookingEvent.APPROVE, 1, None),
    (BookingEvent.EXPIRE, -1, None),
    (BookingEvent.EXPIRE, 0, BookingStatus.EXPIRED),
    (BookingEvent.EXPIRE, 1, BookingStatus.EXPIRED),
])
def test_expiry_guard_boundaries(event: BookingEvent, offset: int, expected: BookingStatus | None) -> None:
    actor = Actor.SYSTEM if event == BookingEvent.EXPIRE else Actor.OWNER
    now = _NOW + timedelta(microseconds=offset)
    if expected is None:
        with pytest.raises(InvalidTransition):
            transition(BookingStatus.REQUESTED, event, actor, now=now, expires_at=_NOW, starts_on=_START)
    else:
        assert transition(
            BookingStatus.REQUESTED, event, actor, now=now, expires_at=_NOW, starts_on=_START,
        ) == expected


@pytest.mark.parametrize("event,actor", [(BookingEvent.APPROVE, Actor.OWNER), (BookingEvent.EXPIRE, Actor.SYSTEM)])
def test_missing_expiry_rejects_guarded_transition(event: BookingEvent, actor: Actor) -> None:
    with pytest.raises(InvalidTransition):
        transition(BookingStatus.REQUESTED, event, actor, now=_NOW, expires_at=None, starts_on=_START)


@pytest.mark.parametrize("hour,event,actor,expected", [
    (22, BookingEvent.PICKUP, Actor.OWNER, BookingStatus.ACTIVE),
    (22, BookingEvent.CANCEL, Actor.OWNER, BookingStatus.CANCELLED),  # owner may cancel a no-show (threat 3)
    (22, BookingEvent.CANCEL, Actor.BORROWER, None),
    (21, BookingEvent.PICKUP, Actor.OWNER, None),
    (21, BookingEvent.CANCEL, Actor.OWNER, BookingStatus.CANCELLED),
    (21, BookingEvent.CANCEL, Actor.BORROWER, BookingStatus.CANCELLED),
])
def test_guards_use_jerusalem_date(
    hour: int, event: BookingEvent, actor: Actor, expected: BookingStatus | None,
) -> None:
    now = datetime(2026, 11, 11, hour, 30, tzinfo=UTC)
    if expected is None:
        with pytest.raises(InvalidTransition):
            transition(BookingStatus.APPROVED, event, actor, now=now, expires_at=None, starts_on=_START)
    else:
        assert transition(
            BookingStatus.APPROVED, event, actor, now=now, expires_at=None, starts_on=_START,
        ) == expected


class _NaiveZone(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


@pytest.mark.parametrize("zone", [None, _NaiveZone()])
def test_naive_now_is_rejected(zone: tzinfo | None) -> None:
    with pytest.raises(ValueError):
        transition(
            BookingStatus.REQUESTED, BookingEvent.DECLINE, Actor.OWNER,
            now=_NOW.replace(tzinfo=zone), expires_at=None, starts_on=_START,
        )


@pytest.mark.parametrize("zone", [None, _NaiveZone()])
def test_naive_request_timestamp_is_rejected(zone: tzinfo | None) -> None:
    with pytest.raises(ValueError):
        expires_at_for(_NOW.replace(tzinfo=zone))


@pytest.mark.parametrize("event,actor", [(BookingEvent.APPROVE, Actor.OWNER), (BookingEvent.EXPIRE, Actor.SYSTEM)])
def test_naive_expiry_is_rejected_when_compared(event: BookingEvent, actor: Actor) -> None:
    with pytest.raises(ValueError):
        transition(
            BookingStatus.REQUESTED, event, actor,
            now=_NOW, expires_at=_NOW.replace(tzinfo=None), starts_on=_START,
        )


@pytest.mark.parametrize("now", [_NOW, _NOW.astimezone(LOCAL_TZ)])
def test_expiry_adds_exact_specified_timedelta(now: datetime) -> None:
    assert expires_at_for(now) == now + timedelta(hours=48)
    assert REQUEST_TTL == timedelta(hours=48)


@pytest.mark.parametrize("first,end", [(5, 5), (6, 5)])
def test_empty_or_reversed_range_is_rejected(first: int, end: int) -> None:
    with pytest.raises(InvalidRange):
        _range(first, end)


def test_inclusive_single_day_has_one_day() -> None:
    rng = DateRange.from_inclusive(_START, _START)
    assert rng == _range(12, 13)
    assert rng.days == 1
    assert rng.last_day == _START


def test_reversed_inclusive_range_is_rejected() -> None:
    with pytest.raises(InvalidRange):
        DateRange.from_inclusive(date(2026, 11, 13), _START)


@pytest.mark.parametrize("first,end,expected", [(1, 5, False), (5, 9, False), (2, 5, True), (1, 9, True)])
def test_overlap_is_symmetric_and_excludes_touching(first: int, end: int, expected: bool) -> None:
    rng = _range(4, 5)
    other = _range(first, end)
    # [1,5) overlaps [4,5), while [5,9) only touches it.
    if (first, end) == (1, 5):
        rng = _range(5, 9)
    assert rng.overlaps(other) is expected
    assert other.overlaps(rng) is expected


@pytest.mark.parametrize("day,expected", [(11, False), (12, True), (13, True), (14, False), (15, False)])
def test_contains_respects_half_open_boundaries(day: int, expected: bool) -> None:
    assert _range(12, 14).contains(date(2026, 11, day)) is expected


@pytest.mark.parametrize("unit", [BookingUnit.DAY, BookingUnit.NIGHT])
@pytest.mark.parametrize("price", [0, 123])
def test_daily_quotes_count_days_and_sum_deposit(unit: BookingUnit, price: int) -> None:
    assert quote(unit, price, 500, _range(12, 15)) == Quote(unit, 3, 3 * price, 500, 3 * price + 500)


@pytest.mark.parametrize("start,end,units", [
    (date(2026, 1, 31), date(2026, 2, 28), 1),
    (date(2028, 1, 31), date(2028, 2, 29), 1),
    (date(2026, 1, 31), date(2026, 3, 31), 2),
    (date(2026, 12, 31), date(2027, 2, 28), 2),
    (date(2028, 2, 29), date(2029, 2, 28), 12),
    (date(2026, 1, 15), date(2026, 2, 15), 1),
])
def test_month_quotes_clamp_from_original_start(start: date, end: date, units: int) -> None:
    assert quote(BookingUnit.MONTH, 100, 50, DateRange(start, end)) == Quote(
        BookingUnit.MONTH, units, units * 100, 50, units * 100 + 50,
    )


@pytest.mark.parametrize("start,end", [
    (date(2026, 1, 15), date(2026, 2, 14)),
    (date(2026, 1, 15), date(2026, 1, 16)),
    (date(2026, 1, 31), date(2026, 3, 28)),
])
def test_partial_months_are_rejected(start: date, end: date) -> None:
    with pytest.raises(NotWholeMonths):
        quote(BookingUnit.MONTH, 100, 50, DateRange(start, end))


@pytest.mark.parametrize("unit", list(BookingUnit))
def test_below_minimum_preserves_duration(unit: BookingUnit) -> None:
    rng = DateRange(date(2026, 1, 15), date(2026, 2, 15)) if unit == BookingUnit.MONTH else _range(1, 2)
    with pytest.raises(BelowMinimum) as caught:
        quote(unit, 1, 1, rng, 2)
    assert (caught.value.units, caught.value.min_units) == (1, 2)


def test_exact_minimum_is_accepted() -> None:
    assert quote(BookingUnit.DAY, 1, 1, _range(1, 3), 2).units == 2


@pytest.mark.parametrize("price,deposit,minimum", [(-1, 1, 1), (0, 0, 1), (0, -1, 1), (0, 1, 0), (0, 1, -1)])
def test_invalid_quote_inputs_raise_value_error(price: int, deposit: int, minimum: int) -> None:
    with pytest.raises(ValueError):
        quote(BookingUnit.DAY, price, deposit, _range(1, 2), minimum)


def test_unknown_booking_unit_is_rejected() -> None:
    with pytest.raises(ValueError):
        quote("hour", 1, 1, _range(1, 2))  # type: ignore[arg-type]


def test_conflicts_sort_by_start_then_end_and_keep_duplicates() -> None:
    taken = [_range(5, 8), _range(2, 7), _range(2, 6), _range(2, 6), _range(9, 10), _range(1, 3)]
    assert conflicts(_range(3, 9), iter(taken)) == [_range(2, 6), _range(2, 6), _range(2, 7), _range(5, 8)]
    assert taken[0] == _range(5, 8)


def test_empty_conflicts_return_empty_list() -> None:
    assert conflicts(_range(3, 9), []) == []


def test_auto_decline_selects_overlaps_and_containment_but_excludes_touching() -> None:
    pending = {1: _range(1, 4), 2: _range(4, 6), 3: _range(2, 8), 4: _range(6, 8), 5: _range(3, 5)}
    assert to_auto_decline(_range(3, 6), pending) == {1, 2, 3, 5}
    assert len(pending) == 5


def test_empty_auto_decline_returns_empty_set() -> None:
    assert to_auto_decline(_range(3, 6), {}) == set()


@pytest.mark.parametrize("first,last,expected", [
    (date(2026, 11, 12), date(2026, 11, 12), "Nov 12"),
    (date(2026, 11, 12), date(2026, 11, 14), "Nov 12 to 14"),
    (date(2026, 11, 30), date(2026, 12, 2), "Nov 30 to Dec 2"),
    (date(2026, 12, 30), date(2027, 1, 2), "Dec 30, 2026 to Jan 2, 2027"),
    (date(2026, 1, 1), date(2026, 1, 2), "Jan 1 to 2"),
    (date(2026, 1, 1), date(2027, 1, 1), "Jan 1, 2026 to Jan 1, 2027"),
])
def test_format_range_uses_inclusive_days_without_dashes(first: date, last: date, expected: str) -> None:
    result = format_range(DateRange.from_inclusive(first, last))
    assert result == expected
    assert all(dash not in result for dash in ("-", "\u2013", "\u2014"))


@pytest.mark.parametrize("month,name", list(enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1,
)))
def test_fixed_month_abbreviations(month: int, name: str) -> None:
    day = date(2026, month, 1)
    assert format_range(DateRange.from_inclusive(day, day)) == f"{name} 1"


@pytest.mark.parametrize("error", [
    NotFound, Forbidden, BookingConflict, BookingNotPending, InvalidRange,
    InvalidTransition, BelowMinimum, NotWholeMonths,
])
def test_domain_errors_inherit_base(error: type[LendboxError]) -> None:
    assert issubclass(error, LendboxError)


@pytest.mark.parametrize("value,field", [(_range(1, 2), "start"), (Quote(BookingUnit.DAY, 1, 1, 1, 2), "units")])
def test_value_objects_are_frozen(value: DateRange | Quote, field: str) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(value, field, None)


@pytest.mark.parametrize("enum_type", [BookingStatus, BookingEvent, Actor, BookingUnit])
def test_enum_values_are_lowercase_member_names(
    enum_type: type[BookingStatus | BookingEvent | Actor | BookingUnit],
) -> None:
    assert all(member.value == member.name.lower() and isinstance(member, str) for member in enum_type)



