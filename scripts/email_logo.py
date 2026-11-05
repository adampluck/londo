"""The weekly email's logo: the site's plum wordmark with a cream outline.

Mail apps in dark mode (Gmail's, Outlook's) darken the email's cream
background but leave images alone, so the plain plum-on-transparent logo
all but vanishes. Outlined in the email's own cream, it looks unchanged on
the light background and still reads on a dark one. Saved into the
psyconnect overlay, so it deploys with the site; re-run after changing
logo.png (stdlib only, like the rest of scripts/):

    python3 scripts/email_logo.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "sites" / "psyconnect" / "logo.png"
OUT = ROOT / "sites" / "psyconnect" / "logo-email.png"
HALO = (0xF6, 0xEF, 0xE4)  # weekly_email.CREAM
RADIUS = 9  # px at the logo's 960 wide; about 1.7px in the email's 180


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

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    w, h, src = read_rgba(SRC)
    pad = RADIUS + 1
    W, H = w + 2 * pad, h + 2 * pad
    # the halo's alpha: each opaque pixel stamps a soft-edged disc
    disc = [
        (dx, dy, max(0.0, min(1.0, RADIUS + 0.5 - math.hypot(dx, dy))))
        for dy in range(-RADIUS - 1, RADIUS + 2)
        for dx in range(-RADIUS - 1, RADIUS + 2)
    ]
    disc = [d for d in disc if d[2] > 0]
    halo = [[0.0] * W for _ in range(H)]
    for y, row in enumerate(src):
        for x, p in enumerate(row):
            if not p[3]:
                continue
            a = p[3] / 255
            for dx, dy, k in disc:
                cell = halo[y + pad + dy]
                v = a * k
                if v > cell[x + pad + dx]:
                    cell[x + pad + dx] = v
    # the wordmark over the halo
    out = []
    for Y in range(H):
        line = []
        for X in range(W):
            ha = halo[Y][X]
            y, x = Y - pad, X - pad
            r, g, b, a = src[y][x] if 0 <= y < h and 0 <= x < w else (0, 0, 0, 0)
            a /= 255
            oa = a + ha * (1 - a)
            if not oa:
                line.append((0, 0, 0, 0))
                continue
            mix = [round((c * a + hc * ha * (1 - a)) / oa) for c, hc in zip((r, g, b), HALO)]
            line.append((*mix, round(oa * 255)))
        out.append(line)
    write_rgba(OUT, W, H, out)
    print(f"wrote {OUT.relative_to(ROOT)} ({W}x{H})")


if __name__ == "__main__":
    main()
