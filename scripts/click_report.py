"""What psyconnect sent each host, month by month, from GoatCounter.

    .venv/bin/python scripts/click_report.py [--since 2026-07-01] [--host om-being]

Per host: ticket clicks (cards, picks and event-page buttons that leave
for the host's ticket page), card opens (a card that opened our own
event page instead) and views of the host's event pages. GoatCounter
counts unique visitors per path per day, so these are people, not taps.

Reads a read-only GoatCounter API key from ~/.config/londo/goatcounter-key
(never from the repo). Event pages are matched to hosts through the
database, so only events from the last year (and ahead) are attributed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_site as b  # noqa: E402

API = "https://psyconnect.goatcounter.com/api/v0"
KEY_FILE = Path.home() / ".config/londo/goatcounter-key"


def gc_get(path: str, **query) -> dict:
    key = KEY_FILE.read_text().strip()
    req = urllib.request.Request(
        f"{API}/{path}?{urllib.parse.urlencode(query)}",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            # GoatCounter drops Python's default user agent
            "User-Agent": "curl/8.7.1",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_hits(since: str, until: str) -> list[dict]:
    """Every path's daily counts, paged the way the API pages: by
    excluding the paths already seen."""
    hits: list[dict] = []
    seen: list[str] = []
    while True:
        r = gc_get(
            "stats/hits", start=since, end=until, limit=100,
            exclude_paths=",".join(seen),
        )
        hits += r["hits"]
        seen += [str(h["path_id"]) for h in r["hits"]]
        if not r.get("more") or not r["hits"]:
            return hits
        time.sleep(1)


def monthly(hit: dict) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for s in hit["stats"]:
        out[s["day"][:7]] += sum(s["hourly"])
    return out


def event_page_hosts() -> dict[str, tuple[str, str]]:
    """/e/<slug> -> (host slug, host name), slugged exactly as the build
    slugs them."""
    b.SITE = b.SITES["psyconnect"]
    b.BASE_URL = b.SITE["base_url"]
    b.SITE_JSON = b.read_site_block()
    events = [e for e in b.fetch_events() if b.site_match(e)]
    past = [e for e in b.fetch_past_events() if b.site_match(e)]
    slugs = b.assign_event_slugs(events, past)
    by_slug = {}
    for e in events + past:
        host = b.organizer_key(e)
        if host:
            by_slug[slugs[(e["source"], e["source_id"])]] = host
    return by_slug


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--until", default=date.today().isoformat())
    ap.add_argument("--host", help="one host's slug, e.g. om-being")
    ap.add_argument("--json", action="store_true", help="raw table as JSON")
    args = ap.parse_args()

    hits = fetch_hits(args.since, args.until)
    pages = event_page_hosts()
    names: dict[str, str] = {s: n for s, n in pages.values()}

    # host -> measure -> month -> count
    table: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for h in hits:
        path = h["path"].strip("/")
        parts = path.split("/")
        if h["event"] and parts[0] in ("out", "open"):
            host = parts[-1]  # out/<host>, out/pick/<host>, open/featured/<host>
            measure = "ticket clicks" if parts[0] == "out" else "card opens"
        elif parts[0] == "e" and len(parts) > 1 and parts[1] in pages:
            host = pages[parts[1]][0]
            measure = "event page views"
        elif parts[0] == "o" and len(parts) > 1:
            host = parts[1]
            measure = "host page views"
        else:
            continue
        for month, n in monthly(h).items():
            table[host][measure][month] += n

    if args.host:
        table = {k: v for k, v in table.items() if k == args.host}
    if args.json:
        print(json.dumps(table, indent=2))
        return

    months = sorted({m for t in table.values() for c in t.values() for m in c})
    measures = ["ticket clicks", "card opens", "event page views", "host page views"]

    def total(host: str) -> int:
        return sum(sum(c.values()) for c in table[host].values())

    for host in sorted(table, key=lambda h: -sum(table[h]["ticket clicks"].values()) * 1000 - total(h)):
        print(f"\n{names.get(host, host)}  ({host})")
        print("  " + " " * 18 + "".join(f"{m:>9}" for m in months) + f"{'total':>9}")
        for m in measures:
            row = table[host].get(m)
            if not row:
                continue
            print(
                f"  {m:<18}"
                + "".join(f"{row.get(mo, 0):>9}" for mo in months)
                + f"{sum(row.values()):>9}"
            )


if __name__ == "__main__":
    main()
