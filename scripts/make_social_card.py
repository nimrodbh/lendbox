"""Build the link preview card and the site icons from the demo photos.

    pip install -e ".[photos]"
    python scripts/make_social_card.py

Writes into lendbox/web/static/:
  og-card.jpg            1200 x 627, the picture a pasted link shows on LinkedIn, WhatsApp, Slack and others
  favicon.svg            the browser tab icon
  favicon-32.png         the same, for browsers that do not take SVG icons
  apple-touch-icon.png   180 x 180, for a phone's home screen

The card's text is set in the first font found in --fonts (Segoe UI on Windows, DejaVu on Linux), so the
card is rebuilt by hand when the wording or the photos change, not in CI.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "lendbox" / "web" / "static"

PAPER, INK, MUTED, GREEN, WHITE = "#faf8f3", "#1d2420", "#59645e", "#1f6b4f", "#ffffff"
PHOTOS = [1, 12, 22, 64]  # a drill, a guitar, a tent, a camera: four different reasons to borrow
HEADLINE = ["Borrow what", "you need.", "Lend what you", "don't use."]
TAGLINE = "A neighborhood library of things"

# The letter L of the logo on a 64 x 64 grid. The SVG icon and the drawn ones share these points.
MARK_L = [(22, 14), (31, 14), (31, 42), (46, 42), (46, 50), (22, 50)]

FONT_FILES = {
    "bold": ["segoeuib.ttf", "DejaVuSans-Bold.ttf"],
    "regular": ["segoeui.ttf", "DejaVuSans.ttf"],
}


def font(dirs: list[Path], weight: str, size: int) -> ImageFont.FreeTypeFont:
    for folder in dirs:
        for name in FONT_FILES[weight]:
            if (folder / name).exists():
                return ImageFont.truetype(str(folder / name), size)
    raise SystemExit(f"no {weight} font found in {[str(d) for d in dirs]}; pass --fonts")


def mark(size: int) -> Image.Image:
    """The logo mark, drawn four times too large and scaled down so the edges come out smooth."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=round(big * 14 / 64), fill=GREEN)
    draw.polygon([(x * big / 64, y * big / 64) for x, y in MARK_L], fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def tile(path: Path, width: int, height: int, radius: int) -> Image.Image:
    """One photo, cropped from its center to fill the tile, with rounded corners."""
    photo = Image.open(path).convert("RGB")
    scale = max(width / photo.width, height / photo.height)
    photo = photo.resize((round(photo.width * scale), round(photo.height * scale)), Image.LANCZOS)
    left, top = (photo.width - width) // 2, (photo.height - height) // 2
    photo = photo.crop((left, top, left + width, top + height))
    mask = Image.new("L", (width * 4, height * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, width * 4 - 1, height * 4 - 1], radius=radius * 4, fill=255)
    photo.putalpha(mask.resize((width, height), Image.LANCZOS))
    return photo


def card(fonts: list[Path]) -> Image.Image:
    width, height, margin, gap = 1200, 627, 64, 16
    img = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(img)

    logo = mark(60)
    img.paste(logo, (margin, margin), logo)
    draw.text((margin + 78, margin + 30), "Lendbox", font=font(fonts, "bold", 40), fill=INK, anchor="lm")

    y = 196
    headline = font(fonts, "bold", 62)
    for line in HEADLINE:
        draw.text((margin, y), line, font=headline, fill=INK)
        y += 72
    draw.text((margin, y + 22), TAGLINE, font=font(fonts, "regular", 28), fill=MUTED)

    left, top = 608, margin
    tile_w = (width - margin - left - gap) // 2
    tile_h = (height - 2 * margin - gap) // 2
    for i, number in enumerate(PHOTOS):
        photo = tile(STATIC / "listings" / f"l{number:03d}.webp", tile_w, tile_h, radius=18)
        img.paste(photo, (left + (i % 2) * (tile_w + gap), top + (i // 2) * (tile_h + gap)), photo)
    return img


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fonts", action="append", type=Path, help="a folder with TrueType fonts (repeatable)")
    args = ap.parse_args()
    fonts = args.fonts or [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/dejavu")]

    card(fonts).save(STATIC / "og-card.jpg", quality=88, optimize=True, progressive=True)
    mark(32).save(STATIC / "favicon-32.png", optimize=True)
    touch = Image.new("RGB", (180, 180), GREEN)  # phones round the corners themselves
    big = mark(180)
    touch.paste(big, (0, 0), big)
    touch.save(STATIC / "apple-touch-icon.png", optimize=True)
    points = " ".join(f"{x},{y}" for x, y in MARK_L)
    (STATIC / "favicon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="14" fill="{GREEN}"/>'
        f'<polygon points="{points}" fill="{WHITE}"/></svg>\n',
        encoding="utf-8",
        newline="\n",
    )
    for name in ("og-card.jpg", "favicon.svg", "favicon-32.png", "apple-touch-icon.png"):
        print(f"wrote {name}  {(STATIC / name).stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
