"""Tell Bing (and the other IndexNow engines) which psyconnect pages are
new or changed since the last deploy. ChatGPT's search reads Bing's
index, so this is how a new listing reaches both within the hour rather
than whenever Bing next crawls.

    python3 scripts/indexnow.py snapshot live-sitemap.xml   # before deploy
    python3 scripts/indexnow.py submit live-sitemap.xml build-psyconnect/sitemap.xml

`submit` sends the URLs that are new in the fresh sitemap, or whose
<lastmod> moved — never the whole sitemap on every run, which IndexNow
treats as spam. The key is public by design: it's served as
/<key>.txt from the site root (sites/psyconnect/<key>.txt) to prove
the site asked. Stdlib only, for CI.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

HOST = "psyconnect.london"
LIVE = f"https://{HOST}/sitemap.xml"
ENDPOINT = "https://api.indexnow.org/indexnow"
KEY_DIR = Path(__file__).resolve().parent.parent / "sites/psyconnect"
_ENTRY = re.compile(r"<loc>([^<]+)</loc>(?:<lastmod>([^<]+)</lastmod>)?")


def entries(xml: str) -> dict[str, str]:
    return {loc: mod or "" for loc, mod in _ENTRY.findall(xml)}


def key() -> str:
    found = [p.stem for p in KEY_DIR.glob("*.txt") if re.fullmatch(r"[0-9a-f]{32}", p.stem)]
    if len(found) != 1:
        sys.exit(f"expected one IndexNow key file in {KEY_DIR}, found {found}")
    return found[0]


def snapshot(out: str) -> None:
    try:
        with urllib.request.urlopen(LIVE, timeout=30) as r:
            Path(out).write_bytes(r.read())
    except Exception as e:  # first deploy, or the site's down: submit nothing
        print(f"no live sitemap ({e}); leaving an empty snapshot")
        Path(out).write_text("")


def submit(old_path: str, new_path: str) -> None:
    old = entries(Path(old_path).read_text())
    new = entries(Path(new_path).read_text())
    if not old:
        print("no previous sitemap to compare against — not submitting")
        return
    changed = [u for u, mod in new.items() if old.get(u) != mod]
    if not changed:
        print("nothing new or changed")
        return
    k = key()
    body = json.dumps({
        "host": HOST,
        "key": k,
        "keyLocation": f"https://{HOST}/{k}.txt",
        "urlList": changed[:10000],
    }).encode()
    req = urllib.request.Request(
        ENDPOINT, body, {"Content-Type": "application/json; charset=utf-8"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"IndexNow: {r.status} for {len(changed)} URLs")
                return
        except Exception as e:
            print(f"IndexNow attempt {attempt + 1} failed: {e}")
            time.sleep(10)
    # never fail the deploy over this
    print("IndexNow: giving up this run")


if __name__ == "__main__":
    if sys.argv[1:2] == ["snapshot"]:
        snapshot(sys.argv[2])
    elif sys.argv[1:2] == ["submit"]:
        submit(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
