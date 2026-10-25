"""Validate the portable demo world before a loader assigns database ids."""
from __future__ import annotations

import json
import re
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

_SEED = Path(__file__).resolve().parents[1] / "seed"
_INTERESTS = {"musicians", "students", "builders-diy", "office-study", "outdoor-camping", "parents-kids"}
_TREE = {
    "israel": ("country", None),
    "tel-aviv-yafo": ("city", "israel"),
    "haifa": ("city", "israel"),
    "beer-sheva": ("city", "israel"),
    "florentin": ("neighborhood", "tel-aviv-yafo"),
    "tel-aviv-university": ("campus", "tel-aviv-yafo"),
}
_CITIES = {"tel-aviv-yafo", "haifa", "beer-sheva"}
_STATUSES = {"requested", "approved", "active", "returned", "declined", "expired", "cancelled"}
_PROHIBITED = {"vehicles", "weapons", "medication", "child-car-seats"}
Row = dict[str, Any]


@pytest.fixture(scope="module")
def demo() -> Row:
    """Load the delivered data using a path independent of the working directory."""
    return json.loads((_SEED / "demo.json").read_text(encoding="utf-8"))


def _index(rows: list[Row], field: str = "key") -> dict[str, Row]:
    return {row[field]: row for row in rows}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in [key, *_strings(item)]]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def test_top_level_keys_and_version(demo: Row) -> None:
    assert list(demo) == [
        "version", "users", "communities", "memberships", "categories", "listings", "bookings", "reviews", "looking_for",
    ]
    assert type(demo["version"]) is int
    assert demo["version"] == 1


def test_keys_slugs_are_unique_and_all_references_resolve(demo: Row) -> None:
    indexes = {}
    for collection, field in [
        ("users", "key"), ("communities", "slug"), ("categories", "slug"), ("listings", "key"), ("bookings", "key"),
    ]:
        assert all(isinstance(row[field], str) and row[field] for row in demo[collection])
        indexes[collection] = _index(demo[collection], field)
        assert len(indexes[collection]) == len(demo[collection])
    users, communities = indexes["users"], indexes["communities"]
    categories, listings, bookings = indexes["categories"], indexes["listings"], indexes["bookings"]
    for community in communities.values():
        assert community["parent"] is None or community["parent"] in communities
    membership_pairs = [(row["user"], row["community"]) for row in demo["memberships"]]
    assert len(membership_pairs) == len(set(membership_pairs))
    for user, community in membership_pairs:
        assert user in users
        assert community in communities
    for listing in listings.values():
        assert listing["owner"] in users
        assert listing["category"] in categories
        assert listing["area"] in communities
        assert all(slug in communities for slug in listing["interests"])
    for booking in bookings.values():
        assert booking["listing"] in listings
        assert booking["kind"] in {"loan", "block"}
        if booking["kind"] == "loan":
            assert booking["borrower"] in users
        else:
            assert not {"borrower", "message", "requested_offset_hours"} & booking.keys()
            assert booking["status"] in {"approved", "cancelled"}
    for review in demo["reviews"]:
        assert review["booking"] in bookings
        assert review["author"] in users
        assert review["subject"] in users
    for post in demo["looking_for"]:
        assert post["author"] in users
        assert post["area"] in communities
        assert post["interest"] is None or post["interest"] in communities
        assert post["category"] is None or post["category"] in categories


def test_community_tree_has_exact_levels_parents_and_interest_roots(demo: Row) -> None:
    communities = _index(demo["communities"], "slug")
    assert set(communities) == set(_TREE) | _INTERESTS
    for slug, (level, parent) in _TREE.items():
        assert (communities[slug]["kind"], communities[slug]["area_level"], communities[slug]["parent"]) == (
            "area", level, parent,
        )
    for slug in _INTERESTS:
        assert (communities[slug]["kind"], communities[slug]["area_level"], communities[slug]["parent"]) == (
            "interest", None, None,
        )
    names = {
        "israel": "Israel", "tel-aviv-yafo": "Tel Aviv-Yafo", "haifa": "Haifa", "beer-sheva": "Be'er Sheva",
        "florentin": "Florentin", "tel-aviv-university": "Tel Aviv University", "musicians": "Musicians",
        "students": "Students", "builders-diy": "Builders and DIY", "office-study": "Office and study",
        "outdoor-camping": "Outdoor and camping", "parents-kids": "Parents and kids",
    }
    for slug, expected in names.items():
        assert communities[slug]["name"] == expected
    for slug in _CITIES | _INTERESTS:
        rules = communities[slug]["rules"]
        assert isinstance(rules, str) and rules.strip()
        assert 1 <= len(re.findall(r"[^.!?]+[.!?](?:\s|$)", rules)) <= 3


def test_listing_areas_and_interest_tags_have_correct_kinds(demo: Row) -> None:
    communities = _index(demo["communities"], "slug")
    for listing in demo["listings"]:
        assert listing["area"] != "israel"
        assert communities[listing["area"]]["kind"] == "area"
        assert len(listing["interests"]) <= 2
        assert len(listing["interests"]) == len(set(listing["interests"]))
        assert all(communities[slug]["kind"] == "interest" for slug in listing["interests"])


def test_category_attributes_validate_or_are_empty(demo: Row) -> None:
    schemas = json.loads((_SEED / "category_schemas.json").read_text(encoding="utf-8"))
    validators = {}
    for slug in ("power-tools", "musical-instruments"):
        schema = schemas[slug]["schema"]
        Draft202012Validator.check_schema(schema)
        validators[slug] = Draft202012Validator(schema, format_checker=FormatChecker())
    for listing in demo["listings"]:
        if listing["category"] in validators:
            validators[listing["category"]].validate(listing["attributes"])
        else:
            assert listing["attributes"] == {}


def test_no_listing_uses_a_prohibited_category(demo: Row) -> None:
    categories = _index(demo["categories"], "slug")
    assert all(type(category["prohibited"]) is bool for category in categories.values())
    assert _PROHIBITED <= set(categories)
    assert all(categories[slug]["prohibited"] for slug in _PROHIBITED)
    assert "sublets" not in categories
    assert all(category["booking_unit"] == "day" for category in categories.values())
    assert all(not categories[listing["category"]]["prohibited"] for listing in demo["listings"])


def test_money_is_integer_nonnegative_with_positive_deposits_and_minimums(demo: Row) -> None:
    for listing in demo["listings"]:
        for field in ("price_per_unit_agorot", "deposit_agorot", "late_fee_agorot", "min_units"):
            assert type(listing[field]) is int, (listing["key"], field)
        assert listing["price_per_unit_agorot"] >= 0
        assert listing["deposit_agorot"] > 0
        assert listing["late_fee_agorot"] >= 0
        assert listing["min_units"] >= 1


def test_loans_meet_minimum_duration_and_have_different_borrower_on_live_listing(demo: Row) -> None:
    listings = _index(demo["listings"])
    for booking in demo["bookings"]:
        assert type(booking["days"]) is int and booking["days"] >= 1
        if booking["kind"] == "loan":
            listing = listings[booking["listing"]]
            assert booking["days"] >= listing["min_units"]
            assert booking["borrower"] != listing["owner"]
            assert listing["status"] == "live"
            assert isinstance(booking["message"], str) and booking["message"].strip()


def test_approved_and_active_bookings_including_blocks_never_overlap(demo: Row) -> None:
    occupying = [row for row in demo["bookings"] if row["status"] in {"approved", "active"}]
    for first, second in combinations(occupying, 2):
        if first["listing"] != second["listing"]:
            continue
        first_start, second_start = first["start_offset_days"], second["start_offset_days"]
        assert not (
            first_start < second_start + second["days"] and second_start < first_start + first["days"]
        ), (first["key"], second["key"])


def test_booking_status_agrees_with_offset_clock(demo: Row) -> None:
    for booking in demo["bookings"]:
        start, days, status = booking["start_offset_days"], booking["days"], booking["status"]
        assert type(start) is int
        assert status in _STATUSES
        if booking["kind"] == "loan":
            requested = booking["requested_offset_hours"]
            assert type(requested) is int and requested < 0
            if status == "requested":
                assert -48 < requested < 0
                assert start > 0
            elif status == "expired":
                assert requested <= -48
        if status == "approved":
            assert start > 0
        elif status == "active":
            assert start <= 0 < start + days
        elif status == "returned":
            assert start + days <= 0


def test_reviews_are_unique_by_author_and_only_between_returned_loan_parties(demo: Row) -> None:
    bookings, listings = _index(demo["bookings"]), _index(demo["listings"])
    seen = set()
    for review in demo["reviews"]:
        booking = bookings[review["booking"]]
        assert booking["kind"] == "loan" and booking["status"] == "returned"
        owner = listings[booking["listing"]]["owner"]
        assert {review["author"], review["subject"]} == {owner, booking["borrower"]}
        pair = (review["booking"], review["author"])
        assert pair not in seen
        seen.add(pair)
        assert type(review["rating"]) is int and 1 <= review["rating"] <= 5
        assert isinstance(review["body"], str) and review["body"].strip()


def test_counts_and_distributions_match_content_requirements(demo: Row) -> None:
    assert len(demo["communities"]) == 12
    assert 15 <= len(demo["categories"]) <= 18
    assert {"power-tools", "musical-instruments"} <= {row["slug"] for row in demo["categories"]}
    assert len(demo["users"]) == 40
    assert {row["key"] for row in demo["users"]} == {f"u{i:02}" for i in range(1, 41)}
    assert 34 <= sum(row["verified"] for row in demo["users"]) <= 37
    assert all(type(row["verified"]) is bool for row in demo["users"])
    assert all(re.fullmatch(r"[A-Za-z]+ [A-Z]\.", row["display_name"]) for row in demo["users"])
    listings = demo["listings"]
    assert len(listings) == 120
    assert {row["key"] for row in listings} == {f"l{i:03}" for i in range(1, 121)}
    areas = Counter(row["area"] for row in listings)
    assert sum(areas[slug] for slug in ("tel-aviv-yafo", "florentin", "tel-aviv-university")) >= 72
    assert areas["haifa"] >= 12 and areas["beer-sheva"] >= 12
    assert not areas["israel"]
    tags = Counter(slug for row in listings for slug in row["interests"])
    assert all(tags[slug] >= 12 for slug in _INTERESTS)
    assert sum(not row["interests"] for row in listings) >= 15
    assert sum(row["status"] == "live" for row in listings) >= 108
    assert all(row["status"] in {"live", "draft", "hidden"} for row in listings)
    # "About 20%" is interpreted as 18% to 22%, inclusive.
    assert 0.18 <= sum(row["price_per_unit_agorot"] == 0 for row in listings) / len(listings) <= 0.22
    assert sum(row["min_units"] == 1 for row in listings) > len(listings) / 2
    assert {2, 3} <= {row["min_units"] for row in listings}
    assert all(row["min_units"] in {1, 2, 3} for row in listings)
    owners = Counter(row["owner"] for row in listings)
    assert 25 <= len(owners) <= 32 and max(owners.values()) <= 9
    assert len({row["title"] for row in listings}) == len(listings)
    assert len({row["description"] for row in listings}) == len(listings)
    for row in listings:
        assert 1 <= len(re.findall(r"[^.!?]+[.!?](?:\s|$)", row["description"])) <= 3
        assert row["pickup_detail"].strip()
        assert not re.search(r"\d", row["pickup_detail"])
        price, title = row["price_per_unit_agorot"], row["title"].lower()
        if price and "drill" in title:
            assert 1500 <= price <= 3000
        if price and "keyboard" in title:
            assert 4000 <= price <= 9000
        if price and "projector" in title:
            assert 5000 <= price <= 12000
    assert 60 <= len(demo["bookings"]) <= 80
    loans = [row for row in demo["bookings"] if row["kind"] == "loan"]
    assert {row["status"] for row in loans} == _STATUSES
    assert sum(row["kind"] == "block" for row in demo["bookings"]) >= 4
    reviewed = Counter(row["booking"] for row in demo["reviews"])
    assert len(reviewed) >= 12
    assert 1 in reviewed.values() and 2 in reviewed.values()
    posts = demo["looking_for"]
    assert 20 <= len(posts) <= 25
    assert sum(row["status"] == "open" for row in posts) >= 18
    assert _CITIES <= {row["area"] for row in posts}
    assert len({row["interest"] for row in posts if row["interest"] is not None}) >= 4
    assert all(row["status"] in {"open", "closed"} and row["title"].strip() and row["body"].strip() for row in posts)


def test_emails_are_reserved_test_addresses_and_u01_is_only_admin(demo: Row) -> None:
    assert [row["key"] for row in demo["users"] if row["is_admin"]] == ["u01"]
    for user in demo["users"]:
        assert type(user["is_admin"]) is bool
        assert re.fullmatch(r"u\d{2}@example\.test", user["email"])
        assert user["email"] == f"{user['key']}@example.test"
    assert len({row["email"] for row in demo["users"]}) == len(demo["users"])


def test_all_strings_omit_long_dashes_phone_patterns_and_http(demo: Row) -> None:
    for text in _strings(demo):
        assert "\u2013" not in text and "\u2014" not in text
        assert not re.search(r"\d{7,}", text)
        assert "http" not in text.lower()


def test_looking_for_references_have_correct_kinds_and_optional_dates_are_paired(demo: Row) -> None:
    communities, categories = _index(demo["communities"], "slug"), _index(demo["categories"], "slug")
    for post in demo["looking_for"]:
        assert communities[post["area"]]["kind"] == "area"
        if post["interest"] is not None:
            assert communities[post["interest"]]["kind"] == "interest"
        if post["category"] is not None:
            assert not categories[post["category"]]["prohibited"]
        start, days = post["wanted_start_offset_days"], post["wanted_days"]
        assert (start is None) == (days is None)
        if start is not None:
            assert type(start) is int
            assert type(days) is int and days >= 1
