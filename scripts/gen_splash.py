"""Generate iOS `apple-touch-startup-image` PNGs (the launch screen shown
before any JS runs, killing the blank-white cold-launch flash).

Run MANUALLY when the logo or brand colour changes — NOT part of the CI
build (build_site.py is stdlib-only; this needs Pillow):

    python3 scripts/gen_splash.py

Writes one PNG per device resolution into each site's `splash/` dir:
  - londo     -> web/splash/            (dark bg, app icon)
  - psyconnect-> sites/psyconnect/splash/ (cream bg, wordmark logo)

The overlay build (web/ then sites/psyconnect/ on top) means identical
filenames let psyconnect's cream splashes cleanly override londo's dark
ones in build-psyconnect/. Keep DEVICES in sync with build_site.py, which
emits the matching <link media=...> tags.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent

# (css_pt_width, css_pt_height, device_pixel_ratio) for current iPhones,
# portrait. Pixel size = pt * dpr; filename is keyed by pixel size so the
# media-query tag in build_site.py can point at it.
DEVICES = [
    (375, 667, 2),  # SE 2/3, 8, 7, 6s          -> 750x1334
    (414, 736, 3),  # 8 Plus                     -> 1242x2208
    (375, 812, 3),  # X, XS, 11 Pro, 12/13 mini  -> 1125x2436
    (414, 896, 2),  # XR, 11                      -> 828x1792
    (414, 896, 3),  # XS Max, 11 Pro Max         -> 1242x2688
    (390, 844, 3),  # 12/13/14, 12/13 Pro        -> 1170x2532
    (428, 926, 3),  # 12/13 Pro Max, 14 Plus     -> 1284x2778
    (393, 852, 3),  # 14 Pro, 15, 15 Pro, 16     -> 1179x2556
    (430, 932, 3),  # 14 Pro Max, 15 Plus, 16+   -> 1290x2796
    (402, 874, 3),  # 16 Pro                     -> 1206x2622
    (440, 956, 3),  # 16 Pro Max                 -> 1320x2868
]

# logo source, background colour, output dir. Logo pasted centred; sized to
# a fraction of the shorter (width) edge so it reads on every aspect ratio.
SPLASH_SITES = [
    {
        "logo": ROOT / "sites" / "psyconnect" / "logo.png",
        "bg": (246, 239, 228),  # #f6efe4
        "out": ROOT / "sites" / "psyconnect" / "splash",
        "logo_frac": 0.52,
    },
    {
        "logo": ROOT / "web" / "icons" / "icon-512.png",
        "bg": (20, 16, 31),  # #14101f
        "out": ROOT / "web" / "splash",
        "logo_frac": 0.34,
    },
]


def splash_name(pt_w: int, pt_h: int, dpr: int) -> str:
    return f"splash-{pt_w * dpr}x{pt_h * dpr}.png"


def build_site_splashes(cfg: dict) -> None:
    logo_src = Image.open(cfg["logo"]).convert("RGBA")
    cfg["out"].mkdir(parents=True, exist_ok=True)
    for pt_w, pt_h, dpr in DEVICES:
        px_w, px_h = pt_w * dpr, pt_h * dpr
        canvas = Image.new("RGB", (px_w, px_h), cfg["bg"])
        # scale logo to a fraction of the canvas width, preserve aspect
        target_w = int(px_w * cfg["logo_frac"])
        scale = target_w / logo_src.width
        target_h = int(logo_src.height * scale)
        logo = logo_src.resize((target_w, target_h), Image.LANCZOS)
        x = (px_w - target_w) // 2
        y = (px_h - target_h) // 2
        canvas.paste(logo, (x, y), logo)  # logo alpha as mask
        canvas.save(cfg["out"] / splash_name(pt_w, pt_h, dpr), optimize=True)
    print(f"Wrote {len(DEVICES)} splashes -> {cfg['out']}")


def main() -> None:
    for cfg in SPLASH_SITES:
        build_site_splashes(cfg)


if __name__ == "__main__":
    main()
