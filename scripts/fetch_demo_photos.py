"""Give every demo listing a real photo from Pixabay (free to use; credited in docs/photo-credits.md).

For each listing in seed/demo.json it searches Pixabay for the item (the title before "with", "and", "for"
or "in", without sizes and counts; seed/photo_queries.json overrides that per listing), takes the best
landscape result not already used by another listing, and saves it into the app as
lendbox/web/static/listings/<key>.webp: cropped to 4:3, 800 px wide, re-encoded (which drops EXIF, camera and
location data). Photos are served from the site itself: the Content-Security-Policy only allows same-origin
images, and Pixabay's API terms ask for exactly this ("download them to your server first", no hotlinking).

Usage (key in .env as PIXABAY_API_KEY=..., never committed; shown on pixabay.com/api/docs/ when logged in):
    python scripts/fetch_demo_photos.py              # all listings without a photo yet
    python scripts/fetch_demo_photos.py l013 l072    # redo these
Needs: pip install -e ".[photos]"
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "seed"
OUT = ROOT / "lendbox" / "web" / "static" / "listings"
CREDITS = ROOT / "docs" / "photo-credits.md"
WIDTH, RATIO = 800, 4 / 3
USER_AGENT = "lendbox-demo-photo-fetcher/1.0 (portfolio project)"


def api_key() -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():  # tolerate a BOM from Windows editors
        if line.startswith("PIXABAY_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("PIXABAY_API_KEY is missing from .env")


def query_for(title: str) -> str:
    """'32-key melodica with spare mouthpiece' -> 'melodica'; 'Cordless drill with two batteries' -> 'cordless drill'."""
    head = re.split(r"\s+(?:with|and|for|in)\s+|,", title, maxsplit=1, flags=re.IGNORECASE)[0]
    words = [w for w in head.split() if not re.search(r"\d", w)]
    return " ".join(words).lower() or title.lower()


def search(key: str, query: str) -> list[dict]:
    """Photos only, landscape, safe search; normalized to the fields the rest of the script uses."""
    url = "https://pixabay.com/api/?" + urllib.parse.urlencode(
        {"key": key, "q": query[:100], "image_type": "photo", "orientation": "horizontal",
         "safesearch": "true", "per_page": 40}
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        hits = json.load(r)["hits"]
    return [
        {"id": h["id"], "image": h["largeImageURL"], "tags": h.get("tags", ""), "photographer": h["user"],
         "photographer_url": f"https://pixabay.com/users/{h['user']}-{h['user_id']}/", "url": h["pageURL"]}
        for h in hits
    ]


def best_match(query: str, results: list[dict]) -> dict | None:
    """The first result (search order is relevance) whose tags contain every word of the query, or None.

    Search alone is loose: 'compact jigsaw' returns a car painted with puzzle pieces, and requiring only the
    last word still lets 'embroidery hoop' match a basketball hoop. Requiring every word keeps the photo on
    the item; a miss is reported so a better query goes into seed/photo_queries.json, never a wrong photo.
    """
    words = [w.strip("-").lower().rstrip("s") for w in query.split()]
    for r in results:
        tags = r["tags"].lower()
        if all(w in tags for w in words):
            return r
    return None


def save_webp(image_url: str, dest: Path) -> None:
    req = urllib.request.Request(image_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r:
        img = Image.open(io.BytesIO(r.read())).convert("RGB")
    w, h = img.size
    if w / h > RATIO:  # too wide: trim the sides
        new_w = int(h * RATIO)
        img = img.crop(((w - new_w) // 2, 0, (w + new_w) // 2, h))
    else:  # too tall: trim top and bottom
        new_h = int(w / RATIO)
        img = img.crop((0, (h - new_h) // 2, w, (h + new_h) // 2))
    img = img.resize((WIDTH, int(WIDTH / RATIO)), Image.LANCZOS)
    img.save(dest, "WEBP", quality=78, method=6)  # a fresh encode carries no EXIF


def write_credits(photos: dict) -> None:
    rows = "\n".join(
        f"| `{k}` | {v['title']} | [{v['photographer']}]({v['photographer_url']}) | [Pixabay]({v['source']}) |"
        for k, v in sorted(photos.items())
    )
    CREDITS.write_text(
        "# Photo credits\n\nDemo listing photos are from [Pixabay](https://pixabay.com) under the "
        "[Pixabay Content License](https://pixabay.com/service/license-summary/) (free to use, attribution not "
        "required, given here with thanks).\n\n| Listing | Item | Photographer | Source |\n|---|---|---|---|\n" + rows + "\n",
        encoding="utf-8",
    )


def main(only: list[str]) -> None:
    key = api_key()
    world = json.loads((SEED / "demo.json").read_text(encoding="utf-8"))
    overrides = json.loads((SEED / "photo_queries.json").read_text(encoding="utf-8"))
    photos_file = SEED / "photos.json"
    photos = json.loads(photos_file.read_text(encoding="utf-8")) if photos_file.exists() else {}
    OUT.mkdir(parents=True, exist_ok=True)
    for k in only:
        photos.pop(k, None)
    used = {v["source_id"] for v in photos.values()}
    todo = [li for li in world["listings"] if li["key"] not in photos and (not only or li["key"] in only)]
    for i, li in enumerate(todo, 1):
        q = overrides.get(li["key"]) or query_for(li["title"])
        p = best_match(q, [r for r in search(key, q) if r["id"] not in used])
        if p is None:
            print(f"{i:3}/{len(todo)} {li['key']} NO MATCH for {q!r}: add an override in seed/photo_queries.json")
            time.sleep(1.0)
            continue
        save_webp(p["image"], OUT / f"{li['key']}.webp")
        used.add(p["id"])
        photos[li["key"]] = {
            "file": f"listings/{li['key']}.webp", "title": li["title"], "query": q, "source_id": p["id"],
            "tags": p["tags"], "photographer": p["photographer"],
            "photographer_url": p["photographer_url"], "source": p["url"],
        }
        photos_file.write_text(json.dumps(photos, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{i:3}/{len(todo)} {li['key']} {q!r} -> {p['photographer']}")
        time.sleep(1.0)  # gentle: well inside Pixabay's 100 requests a minute
    write_credits(photos)
    print(f"done: {len(photos)} of {len(world['listings'])} listings have a photo")


if __name__ == "__main__":
    main(sys.argv[1:])
