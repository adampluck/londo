"""The weekly email's logos, from the site's plum wordmark (logo.png).

Mail apps can't take the site's dark-mode trick (a CSS filter repainting
the stitches bone-white) or its SVG, so the email carries two PNGs:

- logo-email-plate.png: the wordmark on a rounded cream plate, the default.
  On the email's cream page the plate disappears; in apps that darken the
  email by themselves (the Gmail apps), which leave images alone, it reads
  as a cream header plate instead of plum lost on a dark ground.
- logo-email-dark.png: the same pixels recoloured bone, as the site's dark
  theme shows it. The Kit template's dark-mode styles swap it in where a
  mail app honours them (Apple Mail, Outlook).

Saved into the psyconnect overlay, so they deploy with the site; re-run
after changing logo.png (stdlib only, like the rest of scripts/). Give
them new names if they change: mail image proxies cache by URL.

    python3 scripts/email_logo.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sites" / "psyconnect" / "logo.png"
PLATE = ROOT / "sites" / "psyconnect" / "logo-email-plate.png"
DARK = ROOT / "sites" / "psyconnect" / "logo-email-dark.png"
CREAM = (0xF6, 0xEF, 0xE4)  # weekly_email.CREAM
BONE = (0xED, 0xED, 0xED)  # the site's brightness(0) invert(0.93)
PAD_X, PAD_Y, CORNER = 44, 34, 40  # px at the logo's 960 wide


def chunks(data: bytes):
    pos = 8
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        yield kind, data[pos + 8:pos + 8 + n]
        pos += 12 + n


def read_rgba(path: Path) -> tuple[int, int, list[list[tuple[int, int, int, int]]]]:
    """An 8-bit palette PNG (what logo.png is) as rows of RGBA tuples."""
    data = path.read_bytes()
    idat, plte, trns = b"", b"", b""
    for kind, body in chunks(data):
        if kind == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if (depth, ctype) != (8, 3):
                raise SystemExit(f"{path.name}: expected an 8-bit palette PNG")
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat += body
    raw = zlib.decompress(idat)
    rows, prev = [], bytearray(w)
    for y in range(h):
        f, line = raw[y * (w + 1)], bytearray(raw[y * (w + 1) + 1:(y + 1) * (w + 1)])
        for x in range(w):  # undo the PNG row filter (1 byte per pixel)
            a = line[x - 1] if x else 0
            b, c = prev[x], (prev[x - 1] if x else 0)
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        rows.append([
            (*plte[i * 3:i * 3 + 3], trns[i] if i < len(trns) else 255) for i in line
        ])
    return w, h, rows


def write_rgba(path: Path, w: int, h: int, px: list[list[tuple[int, ...]]]) -> None:
    raw = b"".join(b"\0" + bytes(v for p in row for v in p) for row in px)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def recolour(src: Path, out: Path, rgb: tuple[int, int, int]) -> None:
    """Every palette entry painted one colour, transparency untouched: the
    site's filter, done once, with the original's own anti-aliasing."""
    data = src.read_bytes()
    parts = [data[:8]]
    for kind, body in chunks(data):
        if kind == b"PLTE":
            body = bytes(rgb) * (len(body) // 3)
        parts.append(chunk(kind, body))
    out.write_bytes(b"".join(parts))


def corner_cover(x: int, y: int, w: int, h: int) -> float:
    """How much of pixel (x, y) the rounded plate covers."""
    cx = CORNER - x - 0.5 if x < CORNER else x + 0.5 - (w - CORNER) if x >= w - CORNER else 0
    cy = CORNER - y - 0.5 if y < CORNER else y + 0.5 - (h - CORNER) if y >= h - CORNER else 0
    if cx <= 0 or cy <= 0:
        return 1.0
    return max(0.0, min(1.0, CORNER + 0.5 - math.hypot(cx, cy)))


def plate(src: Path, out: Path) -> None:
    w, h, px = read_rgba(src)
    W, H = w + 2 * PAD_X, h + 2 * PAD_Y
    rows = []
    for Y in range(H):
        line = []
        for X in range(W):
            y, x = Y - PAD_Y, X - PAD_X
            r, g, b, a = px[y][x] if 0 <= y < h and 0 <= x < w else (0, 0, 0, 0)
            a /= 255
            mix = tuple(round(c * a + p * (1 - a)) for c, p in zip((r, g, b), CREAM))
            line.append((*mix, round(corner_cover(X, Y, W, H) * 255)))
        rows.append(line)
    write_rgba(out, W, H, rows)


def main() -> None:
    plate(SRC, PLATE)
    recolour(SRC, DARK, BONE)
    for f in (PLATE, DARK):
        print(f"wrote {f.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
