from __future__ import annotations

import json
from pathlib import Path

import pytest

from lendbox.domain.listing import (
    InvalidListing,
    attribute_fields,
    coerce_attributes,
    validate_attributes,
    validate_listing,
)

SCHEMAS = json.loads((Path(__file__).resolve().parents[1] / "seed" / "category_schemas.json").read_text("utf-8"))
TOOLS = SCHEMAS["power-tools"]["schema"]
SUBLETS = SCHEMAS["sublets"]["schema"]
GOOD = {"title": "Cordless drill", "condition": "good", "price": "15", "deposit": "200", "area": "3", "min_units": "1"}


def check(**changes):
    return validate_listing({**GOOD, **changes}, area_ids={3}, interest_ids={7, 8, 9})


def test_valid_listing_is_cleaned_and_priced_in_agorot():
    out = check(title="  Cordless   drill ", price="15.5", deposit="1,200", interests="7,8")
    assert (out["title"], out["price_per_unit_agorot"], out["deposit_agorot"], out["interests"]) == (
        "Cordless drill", 1550, 120000, [7, 8],
    )


def test_empty_price_means_free():
    assert check(price="")["price_per_unit_agorot"] == 0


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"title": "ab"}, "title"),
        ({"condition": "broken"}, "condition"),
        ({"price": "-3"}, "price"),
        ({"price": "1.234"}, "price"),
        ({"deposit": ""}, "deposit"),
        ({"deposit": "0"}, "deposit"),
        ({"area": "4"}, "area"),
        ({"area": "x"}, "area"),
        ({"min_units": "40"}, "min_units"),
        ({"interests": "7,8,9"}, "interests"),
        ({"interests": "5"}, "interests"),
    ],
)
def test_invalid_field_is_named(changes, field):
    with pytest.raises(InvalidListing) as err:
        check(**changes)
    assert err.value.field == field


def test_schema_drives_the_form_fields():
    fields = {f.name: f for f in attribute_fields(TOOLS)}
    assert fields["power_source"].kind == "select" and fields["power_source"].required
    assert fields["battery_included"].kind == "checkbox"
    assert fields["weight_kg"].kind == "number" and fields["weight_kg"].step == "any"
    assert {f.name: f.kind for f in attribute_fields(SUBLETS)}["check_in_from"] == "date"


def test_form_strings_become_schema_types_and_validate():
    attrs = coerce_attributes(
        TOOLS, {"attr_power_source": "battery", "attr_voltage_v": "18", "attr_battery_included": "on", "attr_brand": ""}
    )
    assert attrs == {"power_source": "battery", "voltage_v": 18, "battery_included": True}
    validate_attributes(TOOLS, attrs)


def test_half_rooms_survive_coercion():
    attrs = coerce_attributes(SUBLETS, {"attr_rooms": "2.5", "attr_floor": "3"})
    assert attrs == {"rooms": 2.5, "floor": 3}


def test_missing_required_attribute_names_the_field():
    with pytest.raises(InvalidListing) as err:
        validate_attributes(TOOLS, {})
    assert err.value.field == "attr_power_source"


def test_bad_enum_and_non_numbers_are_rejected():
    with pytest.raises(InvalidListing):
        validate_attributes(TOOLS, {"power_source": "nuclear"})
    with pytest.raises(InvalidListing):
        coerce_attributes(TOOLS, {"attr_weight_kg": "heavy"})
