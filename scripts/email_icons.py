"""The weekly email's icons as PNGs, saved into the psyconnect overlay.

Mail apps won't show SVG, and an image proxy (Gmail's, Kit's) can't be
trusted to fetch one converted on the fly, so the email links plain PNGs
on psyconnect.london itself. The source is build_site.write_email_icons'
SVGs — the explore tiles' line icons — rasterised once by wsrv.nl from
the live site. Re-run after adding an icon there (deploy the SVGs first):

    python3 scripts/email_icons.py
"""
from __future__ import annotations

import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402

OUT = bs.ROOT / "sites" / "psyconnect" / "icons" / "email"
PX = 88  # 2x the email's largest icon


def main() -> None:
    bs.SITE = bs.SITES["psyconnect"]
    bs.SITE_JSON = bs.read_site_block()
    with tempfile.TemporaryDirectory() as tmp:
        bs.write_email_icons(Path(tmp), bs.SITE_JSON.get("topics"))
        names = sorted(p.stem for p in Path(tmp).glob("*.svg"))
    OUT.mkdir(parents=True, exist_ok=True)
    for name in names:
        svg = f"{bs.SITE['base_url']}/icons/email/{name}.svg"
        url = "https://wsrv.nl/?url=" + urllib.parse.quote(svg, safe="") + f"&w={PX}&h={PX}&output=png"
        # wsrv.nl turns away Python's default user agent
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 psyconnect-icons"})
        with urllib.request.urlopen(req, timeout=30) as res:
            (OUT / f"{name}.png").write_bytes(res.read())
    print(f"Wrote {len(names)} icons -> {OUT.relative_to(bs.ROOT)}")


if __name__ == "__main__":
    main()
