"""Choosing the photos for the home page. A pure rule: no database needed."""

from __future__ import annotations

from lendbox.web import app as web


def listing(i: int, category: str, photo: bool = True) -> dict:
    return {"id": i, "category_slug": category, "photo": f"listings/l{i:03d}.webp" if photo else None}


def test_home_photos_show_one_listing_per_category_newest_first():
    found = [listing(9, "tools"), listing(8, "tools"), listing(7, "music"), listing(6, "camping", photo=False),
             listing(5, "camping"), listing(4, "games"), listing(3, "kitchen")]
    assert [item["id"] for item in web.pick_featured(found)] == [9, 7, 5, 4]


def test_home_photos_can_be_fewer_than_four():
    assert web.pick_featured([listing(2, "tools"), listing(1, "tools")]) == [listing(2, "tools")]
    assert web.pick_featured([]) == []
