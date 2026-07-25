"""Generate psyconnect's app icons: the site's own background pattern,
darkened to plum, with the cross-stitch P knocked out in white.

Run MANUALLY when the mark or the background changes — NOT part of the CI
build (build_site.py is stdlib-only; this needs Pillow):

    python3 scripts/gen_icons.py

Sources:
  sites/psyconnect/bg.jpg           the page background pattern
  sites/psyconnect/icons/mark-p.png white-on-black master of the stitched P

Writes favicon.png, apple-touch-icon.png, icon-192/512.png, maskable-512.png
and icon.svg into sites/psyconnect/icons/. The mark master is kept separate
so re-running is idempotent — the script never reads its own output.
"""
from __future__ import annotations

import base64
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "sites" / "psyconnect"
ICONS = SITE / "icons"

# duotone ends for the ground. The pattern is pale and low contrast, so a
# plum multiply just flattened it into a solid block; stretching its
# luminance and mapping it between two plums keeps the swirls legible while
# staying dark enough to carry a white mark.
DARK = "#1e1626"
LIGHT = "#6e4059"

# How much of the canvas width the P spans, per icon. Everything sits well
# inside its tile: home-screen icons are rounded and shown next to others,
# so a mark that runs edge to edge reads as cramped.
FULL_BLEED = 0.46
# platforms crop this one to a circle/squircle, leaving ~80% of the canvas
# visible — scaled to match, the mark ends up the same weight as above
MASKABLE = 0.38
FAVICON = 0.5  # renders at 16px, so the mark keeps a little extra room

# Window of the 1024px background tile each icon crops. Smaller = more
# zoomed, so the swirls read as swirls rather than texture. The Apple touch
# icon is the largest any of these is ever drawn — a phone home screen — so
# it takes the tightest crop.
CROP = 620
CROP_APPLE = 130

# the mark: warm off-white rather than pure white, so it sits in the same
# family as the site's cream ground instead of glaring out of the plum
BONE = "#f4ece0"


def mark() -> Image.Image:
    """White-on-black mask of the stitched P, cropped to its bounding box."""
    src = Image.open(ICONS / "mark-p.png").convert("L")
    return src.crop(src.getbbox())


def ground(size: int, box: int = CROP) -> Image.Image:
    bg = Image.open(SITE / "bg.jpg").convert("RGB")
    # cropping a window rather than shrinking the whole tile keeps individual
    # strokes readable; the full tile turns to mush below ~192px
    left = (bg.width - box) // 2
    top = (bg.height - box) // 2
    tile = bg.crop((left, top, left + box, top + box)).resize(
        (size, size), Image.LANCZOS
    )
    lum = ImageOps.autocontrast(tile.convert("L"), cutoff=1)
    # small icons get the texture blurred almost flat — at 16px it is noise
    lum = lum.filter(ImageFilter.GaussianBlur(size * (0.05 if size <= 64 else 0.0023)))
    return ImageOps.colorize(lum, black=DARK, white=LIGHT)


def compose(
    size: int, scale: float, box: int = CROP, ink: str = BONE
) -> Image.Image:
    canvas = ground(size, box)
    m = mark()
    width = int(size * scale)
    height = int(m.height * width / m.width)
    stitched = m.resize((width, height), Image.LANCZOS)
    x = (size - width) // 2
    y = (size - height) // 2

    # a soft dark halo under the mark so it holds up over the lighter swirls
    halo = Image.new("L", (size, size), 0)
    halo.paste(stitched, (x, y))
    halo = halo.filter(ImageFilter.GaussianBlur(size * 0.012))
    canvas = Image.composite(
        ImageEnhance.Brightness(canvas).enhance(0.55), canvas, halo
    )

    stamp = Image.new("L", (size, size), 0)
    stamp.paste(stitched, (x, y))
    return Image.composite(Image.new("RGB", (size, size), ink), canvas, stamp)


def save(image: Image.Image, name: str) -> None:
    """Palette-quantised PNG. The ground is a two-colour ramp plus the
    off-white mark, so 96 colours is indistinguishable from truecolour and roughly a
    fifth of the bytes — worth it for files every visitor fetches."""
    image.quantize(colors=96, method=Image.MEDIANCUT, dither=Image.FLOYDSTEINBERG).save(
        ICONS / name, optimize=True
    )


def main() -> None:
    save(compose(512, FULL_BLEED), "icon-512.png")
    save(compose(192, FULL_BLEED), "icon-192.png")
    save(compose(180, FULL_BLEED, box=CROP_APPLE), "apple-touch-icon.png")
    save(compose(512, MASKABLE), "maskable-512.png")
    save(compose(48, FAVICON), "favicon.png")

    # icon.svg is a bitmap wrapped in an <svg>, as it was before — but built
    # from the 192 so the data URI stays small; it is only ever drawn at
    # favicon and tab sizes
    b64 = base64.b64encode((ICONS / "icon-192.png").read_bytes()).decode()
    (ICONS / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
        f'<image width="192" height="192" href="data:image/png;base64,{b64}"/>'
        "</svg>"
    )
    print(f"wrote icons -> {ICONS}")


if __name__ == "__main__":
    main()
