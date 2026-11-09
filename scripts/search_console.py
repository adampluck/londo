"""Google Search Console report for psyconnect.

    .venv/bin/python scripts/search_console.py sites
    .venv/bin/python scripts/search_console.py report [--days 28] [--compare]
    .venv/bin/python scripts/search_console.py queries [--days 28] [--page /p/sound-bath/]
    .venv/bin/python scripts/search_console.py pages [--days 28]
    .venv/bin/python scripts/search_console.py dump --days 90 out.csv

`report` is the overview: totals against the period before, then the
queries and pages that matter, and the near misses (average position
8–20 with real impressions) where a better page is most likely to pay.

Auth is a service account with read-only access to the property (added
in Search Console → Settings → Users and permissions, "Restricted"). Its
JSON key lives at ~/.config/londo/gsc-service-account.json, never in the
repo. The token request is a JWT signed with the openssl CLI, so this
needs nothing beyond the standard library.

Search Console data lags two to three days, so periods end three days
ago.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from functools import cache
from pathlib import Path

KEY_FILE = Path.home() / ".config/londo/gsc-service-account.json"
SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
API = "https://searchconsole.googleapis.com/webmasters/v3"
SITE_HOST = "psyconnect.london"
LAG_DAYS = 3
PAGE_ROWS = 5000

_token: str | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def access_token() -> str:
    global _token
    if _token:
        return _token
    if not KEY_FILE.exists():
        sys.exit(
            f"No service-account key at {KEY_FILE} — see the setup steps in "
            "this script's docstring."
        )
    key = json.loads(KEY_FILE.read_text())
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({
        "iss": key["client_email"],
        "scope": SCOPE,
        "aud": key["token_uri"],
        "iat": now,
        "exp": now + 3600,
    }).encode())
    signing_input = f"{header}.{claims}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as pem:
        pem.write(key["private_key"])
        pem.flush()
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem.name],
            input=signing_input, capture_output=True, check=True,
        ).stdout
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": f"{header}.{claims}.{_b64(sig)}",
    }).encode()
    with urllib.request.urlopen(key["token_uri"], body, timeout=30) as resp:
        _token = json.load(resp)["access_token"]
    return _token


def call(path: str, payload: dict | None = None) -> dict:
    url = f"{API}/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    for attempt in range(4):
        req = urllib.request.Request(url, data=data, headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                sys.exit(
                    f"{e.code} from Search Console: {e.read().decode()[:300]}\n"
                    "Is the service account's email added to the property "
                    "(Settings → Users and permissions)?"
                )
            if e.code < 500 or attempt == 3:
                raise
        except Exception:
            if attempt == 3:
                raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


@cache
def site_url() -> str:
    """The property as Search Console names it: a domain property
    (sc-domain:…) or a URL prefix (https://…/) — whichever was added."""
    entries = call("sites").get("siteEntry", [])
    ours = [s["siteUrl"] for s in entries if SITE_HOST in s["siteUrl"]]
    if not ours:
        sys.exit(
            "The service account can't see a psyconnect property. Sites it "
            f"can see: {[s['siteUrl'] for s in entries] or 'none'}"
        )
    # a domain property covers http/https/www alike — prefer it
    ours.sort(key=lambda s: not s.startswith("sc-domain:"))
    return ours[0]


def query(start: date, end: date, dims: list[str], filters: list[dict] = ()) -> list[dict]:
    site = urllib.parse.quote(site_url(), safe="")
    rows: list[dict] = []
    while True:
        payload = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dims,
            "rowLimit": PAGE_ROWS,
            "startRow": len(rows),
            "dataState": "final",
        }
        if filters:
            payload["dimensionFilterGroups"] = [{"filters": list(filters)}]
        batch = call(f"sites/{site}/searchAnalytics/query", payload).get("rows", [])
        rows += batch
        if len(batch) < PAGE_ROWS:
            return rows


def period(days: int, back: int = 0) -> tuple[date, date]:
    end = date.today() - timedelta(days=LAG_DAYS + back * days)
    return end - timedelta(days=days - 1), end


def totals(rows: list[dict]) -> dict:
    clicks = sum(r["clicks"] for r in rows)
    impr = sum(r["impressions"] for r in rows)
    pos = sum(r["position"] * r["impressions"] for r in rows) / impr if impr else 0
    return {"clicks": clicks, "impressions": impr,
            "ctr": clicks / impr if impr else 0, "position": pos}


def short(url: str) -> str:
    return urllib.parse.urlparse(url).path or "/"


def table(rows: list[dict], label, n: int, cols=("clicks", "impressions", "ctr", "position")) -> None:
    for r in rows[:n]:
        bits = []
        if "clicks" in cols:
            bits.append(f"{r['clicks']:>5.0f} clicks")
        if "impressions" in cols:
            bits.append(f"{r['impressions']:>6.0f} impr")
        if "ctr" in cols:
            bits.append(f"{100 * r['ctr']:>5.1f}%")
        if "position" in cols:
            bits.append(f"pos {r['position']:>5.1f}")
        print("  " + "  ".join(bits) + f"   {label(r)}")


def cmd_report(args) -> None:
    start, end = period(args.days)
    p_start, p_end = period(args.days, back=1)
    print(f"Search Console — {site_url()}")
    print(f"{start} → {end}   (previous: {p_start} → {p_end})\n")

    by_day = query(start, end, ["date"])
    prev_day = query(p_start, p_end, ["date"])
    now, was = totals(by_day), totals(prev_day)

    def delta(k: str, pct: bool = True) -> str:
        if not was[k]:
            return ""
        if k == "position":
            return f" ({now[k] - was[k]:+.1f})"
        return f" ({100 * (now[k] - was[k]) / was[k]:+.0f}%)"

    print(f"  clicks       {now['clicks']:>8.0f}{delta('clicks')}")
    print(f"  impressions  {now['impressions']:>8.0f}{delta('impressions')}")
    print(f"  CTR          {100 * now['ctr']:>7.1f}%  (was {100 * was['ctr']:.1f}%)")
    print(f"  avg position {now['position']:>8.1f}{delta('position')}  (lower is better)\n")

    weekly: dict[str, list[dict]] = {}
    for r in query(start - timedelta(days=7 * 12), end, ["date"]):
        d = date.fromisoformat(r["keys"][0])
        wk = (d - timedelta(days=d.weekday())).isoformat()
        weekly.setdefault(wk, []).append(r)
    print("By week (clicks / impressions / avg position):")
    for wk, rs in sorted(weekly.items()):
        t = totals(rs)
        bar = "█" * int(t["clicks"] / max(1, max(totals(v)["clicks"] for v in weekly.values())) * 30)
        print(f"  {wk}  {t['clicks']:>5.0f} {t['impressions']:>7.0f}  {t['position']:>5.1f}  {bar}")
    print()

    qrows = query(start, end, ["query"])
    for r in qrows:
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0
    print("Top queries by clicks:")
    table(sorted(qrows, key=lambda r: -r["clicks"]), lambda r: r["keys"][0], args.top)
    print("\nTop queries by impressions:")
    table(sorted(qrows, key=lambda r: -r["impressions"]), lambda r: r["keys"][0], args.top)

    near = [r for r in qrows if 8 <= r["position"] <= 20 and r["impressions"] >= args.min_impr]
    print(f"\nNear misses — position 8–20, ≥{args.min_impr} impressions (page 2; best to push):")
    table(sorted(near, key=lambda r: -r["impressions"]), lambda r: r["keys"][0], args.top)

    low_ctr = [r for r in qrows if r["position"] <= 6 and r["impressions"] >= args.min_impr and r["ctr"] < 0.02]
    print("\nOn page 1 but rarely clicked (title/description worth a look):")
    table(sorted(low_ctr, key=lambda r: -r["impressions"]), lambda r: r["keys"][0], args.top)

    prows = query(start, end, ["page"])
    for r in prows:
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0
    print("\nTop pages by clicks:")
    table(sorted(prows, key=lambda r: -r["clicks"]), lambda r: short(r["keys"][0]), args.top)

    fam: dict[str, list[dict]] = {}
    for r in prows:
        seg = short(r["keys"][0]).strip("/").split("/")[0]
        key = {"e": "event", "p": "practice", "o": "host", "v": "venue", "t": "topic",
               "": "home"}.get(seg, seg)
        fam.setdefault(key, []).append(r)
    print("\nBy page family:")
    for k, rs in sorted(fam.items(), key=lambda kv: -totals(kv[1])["clicks"]):
        t = totals(rs)
        print(f"  {k:<14} {len(rs):>5} pages  {t['clicks']:>6.0f} clicks  "
              f"{t['impressions']:>7.0f} impr  pos {t['position']:.1f}")


def cmd_queries(args) -> None:
    start, end = period(args.days)
    filters = []
    if args.page:
        filters.append({"dimension": "page", "operator": "contains", "expression": args.page})
    rows = query(start, end, ["query"], filters)
    for r in rows:
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0
    table(sorted(rows, key=lambda r: -r["impressions"]), lambda r: r["keys"][0], args.top)


def cmd_pages(args) -> None:
    start, end = period(args.days)
    rows = query(start, end, ["page"])
    for r in rows:
        r["ctr"] = r["clicks"] / r["impressions"] if r["impressions"] else 0
    table(sorted(rows, key=lambda r: -r["impressions"]), lambda r: short(r["keys"][0]), args.top)


def cmd_dump(args) -> None:
    start, end = period(args.days)
    rows = query(start, end, ["date", "query", "page"])
    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "query", "page", "clicks", "impressions", "position"])
        for r in rows:
            w.writerow([*r["keys"], r["clicks"], r["impressions"], round(r["position"], 2)])
    print(f"{len(rows)} rows → {args.out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sites")
    for name in ("report", "queries", "pages", "dump"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=28)
        p.add_argument("--top", type=int, default=25)
        p.add_argument("--min-impr", type=int, default=20)
        if name == "queries":
            p.add_argument("--page")
        if name == "dump":
            p.add_argument("out")
    args = ap.parse_args()
    if args.cmd == "sites":
        for s in call("sites").get("siteEntry", []):
            print(s["siteUrl"], s.get("permissionLevel"))
    else:
        {"report": cmd_report, "queries": cmd_queries,
         "pages": cmd_pages, "dump": cmd_dump}[args.cmd](args)


if __name__ == "__main__":
    main()
