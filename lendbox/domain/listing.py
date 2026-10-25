"""Listing validation, and form fields generated from a category's JSON Schema. Pure: no I/O.

The listing form is one generic form: a category's `attribute_schema` (ADR 0001) says which extra fields it
has, so a new category needs a schema row, not a new template or route. Form values arrive as strings;
`coerce_attributes` turns them into the schema's types before `jsonschema` validates them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from jsonschema import Draft202012Validator, FormatChecker

from lendbox.domain.errors import LendboxError

CONDITIONS = ("new", "like_new", "good", "fair")
# Israeli phone numbers (05x mobile, 0x landline, +972) with any spacing or dashes, and email addresses.
_PHONE = re.compile(r"(?:\+972|(?<!\d)0)[\s.-]?[2-9](?:[\s.-]?\d){6,8}(?!\d)")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", re.IGNORECASE)


def has_contact_details(text: str) -> bool:
    """True if public text carries a phone number or an email (threat model, threat 17)."""
    return bool(_PHONE.search(text) or _EMAIL.search(text))
MAX_INTERESTS = 2


class InvalidListing(LendboxError):
    """A listing field failed validation. `field` names it; `key` is the i18n message key."""

    def __init__(self, field: str, key: str) -> None:
        self.field = field
        self.key = key
        super().__init__(field, key)


@dataclass(frozen=True)
class AttributeField:
    """One form control derived from a JSON Schema property."""

    name: str
    label: str
    kind: str  # "select", "checkbox", "number", "text", "date"
    required: bool
    options: tuple[str, ...] = ()
    step: str = "1"


def attribute_fields(schema: dict) -> list[AttributeField]:
    required = set(schema.get("required", []))
    fields = []
    for name, prop in schema.get("properties", {}).items():
        label = prop.get("title") or name.replace("_", " ").capitalize()
        if "enum" in prop:
            fields.append(AttributeField(name, label, "select", name in required, tuple(str(v) for v in prop["enum"])))
        elif prop.get("type") == "boolean":
            fields.append(AttributeField(name, label, "checkbox", False))
        elif prop.get("type") in ("integer", "number"):
            step = "1" if prop["type"] == "integer" else str(prop.get("multipleOf", "any"))
            fields.append(AttributeField(name, label, "number", name in required, step=step))
        elif prop.get("type") == "string" and prop.get("format") == "date":
            fields.append(AttributeField(name, label, "date", name in required))
        elif prop.get("type") == "string":
            fields.append(AttributeField(name, label, "text", name in required))
        # arrays and nested objects are not offered on the form in v1
    return fields


def coerce_attributes(schema: dict, form: dict[str, str]) -> dict:
    """Form strings to typed values, keyed by schema property. Empty optional fields are left out."""
    out: dict = {}
    for name, prop in schema.get("properties", {}).items():
        raw = form.get(f"attr_{name}")
        kind = prop.get("type")
        if kind == "boolean":
            if raw is not None:
                out[name] = raw in ("on", "true", "1")
            continue
        if raw is None or raw.strip() == "":
            continue
        raw = raw.strip()
        try:
            if kind == "integer":
                out[name] = int(raw)
            elif kind == "number":
                value = float(raw)
                out[name] = int(value) if value.is_integer() else value
            else:
                out[name] = raw
        except ValueError:
            raise InvalidListing(f"attr_{name}", "listing.error.attribute") from None
        if "enum" in prop and kind in ("integer", "number"):
            out[name] = type(prop["enum"][0])(out[name])
    return out


def validate_attributes(schema: dict, attributes: dict) -> None:
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(attributes), key=str)
    if errors:
        err = errors[0]
        missing = err.validator == "required"
        field = err.message.split("'")[1] if missing else (err.path[0] if err.path else "attributes")
        raise InvalidListing(f"attr_{field}", "listing.error.attribute")


def _agorot(raw: str, field: str) -> int:
    """Shekels as typed by a person ('15', '15.5', '1,200') to integer agorot."""
    cleaned = raw.replace(",", "").replace("₪", "").strip()
    try:
        shekels, _, frac = cleaned.partition(".")
        if not shekels.isdigit() or (frac and (not frac.isdigit() or len(frac) > 2)):
            raise ValueError
        return int(shekels) * 100 + int(frac.ljust(2, "0") or 0)
    except ValueError:
        raise InvalidListing(field, f"listing.error.{field}") from None


def validate_listing(form: dict[str, str], *, area_ids: set[int], interest_ids: set[int]) -> dict:
    """Validate the core fields. Returns clean values with money in agorot. Raises InvalidListing on the first bad field."""
    title = " ".join(form.get("title", "").split())
    if not 3 <= len(title) <= 80:
        raise InvalidListing("title", "listing.error.title")
    condition = form.get("condition", "")
    if condition not in CONDITIONS:
        raise InvalidListing("condition", "listing.error.condition")
    price = _agorot(form.get("price", "0") or "0", "price")
    deposit = _agorot(form.get("deposit", ""), "deposit") if form.get("deposit", "").strip() else 0
    if deposit <= 0:
        raise InvalidListing("deposit", "listing.error.deposit")
    try:
        area_id = int(form.get("area", ""))
        min_units = int(form.get("min_units", "1") or "1")
    except ValueError:
        raise InvalidListing("area", "listing.error.area") from None
    if area_id not in area_ids:
        raise InvalidListing("area", "listing.error.area")
    if not 1 <= min_units <= 30:
        raise InvalidListing("min_units", "listing.error.min_units")
    interests = sorted({int(v) for v in form.get("interests", "").split(",") if v.strip().isdigit()})
    if len(interests) > MAX_INTERESTS or not set(interests) <= interest_ids:
        raise InvalidListing("interests", "listing.error.interests")
    description = form.get("description", "").strip()[:2000]
    if has_contact_details(title) or has_contact_details(description):
        raise InvalidListing("description", "listing.error.contact")
    return {
        "title": title,
        "description": description,
        "condition": condition,
        "price_per_unit_agorot": price,
        "deposit_agorot": deposit,
        "min_units": min_units,
        "area_id": area_id,
        "interests": interests,
        "pickup_detail": form.get("pickup_detail", "").strip()[:200],
    }
