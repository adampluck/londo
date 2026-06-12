"""Build the deployable site: web/ plus static per-event and per-category
pages with OG/meta tags, JSON-LD, and a sitemap — so Google (and WhatsApp
link unfurls) can see what the client-side app renders.

Stdlib only, so CI needs no installs:  python3 scripts/build_site.py [outdir]
Reads Supabase credentials from web/config.js (public anon key).
"""
from __future__ import annotations

import html
import json
import re
import shutil
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://adampluck.github.io/londo"

CATEGORIES = {
    "move": ("move", "Ecstatic dance, movement & embodiment events in London"),
    "connect": ("connect", "Authentic relating, circles & community events in London"),
    "expand": ("expand", "Breathwork, meditation & consciousness events in London"),
    "think": ("think", "AI, philosophy & ideas events in London"),
    "make": ("make", "Workshops, crafts & creative events in London"),
}

# Subject topics (londo/enrich.py TOPIC_VOCAB): key -> (slug, SEO title)
TOPICS = {
    "psychedelics": ("psychedelics", "Psychedelics events in London"),
    "consciousness": ("consciousness", "Consciousness events in London"),
    "connection & intimacy": ("connection", "Human connection & intimacy events in London"),
    "tech & ai": ("tech-ai", "Tech & AI events in London"),
    "startups & work": ("startups", "Startup & founders events in London"),
    "arts & creativity": ("arts", "Arts & creativity events in London"),
    "music & sound": ("music", "Music & sound events in London"),
    "nature & outdoors": ("nature", "Nature & outdoors events in London"),
    "healing & wellbeing": ("healing", "Healing & wellbeing events in London"),
    "spirituality & ritual": ("spirituality", "Spirituality & ritual events in London"),
    "society & politics": ("society", "Society & politics events in London"),
    "science & ideas": ("ideas", "Science & ideas events in London"),
}


def read_config() -> tuple[str, str]:
    text = (ROOT / "web" / "config.js").read_text()
    url = re.search(r'SUPABASE_URL:\s*"([^"]+)"', text).group(1)
    key = re.search(r'"(eyJ[^"]+)"', text).group(1)
    return url, key


def goatcounter_snippet() -> str:
    text = (ROOT / "web" / "config.js").read_text()
    m = re.search(r'GOATCOUNTER:\s*"([^"]+)"', text)
    if not m:
        return ""
    return (
        f'<script data-goatcounter="{esc(m.group(1))}" async '
        'src="https://gc.zgo.at/count.js"></script>'
    )


def fetch_events() -> list[dict]:
    supabase_url, anon_key = read_config()
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(days=3)).isoformat()
    query = urllib.parse.urlencode(
        {
            "select": "*",
            "order": "start_at.asc",
            "limit": "1000",
            "start_at": f"gte.{now.isoformat()}",
            "duplicate_of": "is.null",
            "is_online": "eq.false",
            "last_seen_at": f"gte.{stale}",
        }
    )
    req = urllib.request.Request(
        f"{supabase_url}/rest/v1/events?{query}",
        headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def slug(event: dict) -> str:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", event["source_id"])[:48]
    return f"{event['source']}-{sid}"


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def fmt_when(event: dict) -> str:
    start = datetime.fromisoformat(event["start_at"].replace("Z", "+00:00"))
    if event.get("is_all_day"):
        return start.strftime("%A %-d %B %Y")
    return start.strftime("%A %-d %B %Y, %H:%M")


def page(title: str, description: str, canonical: str, og_image: str | None,
         body: str, json_ld: dict | None = None, css_prefix: str = "..") -> str:
    # "</" must not appear inside a <script> block: a scraped description
    # containing "</script>" would otherwise break out and execute (XSS)
    ld = (
        '<script type="application/ld+json">'
        + json.dumps(json_ld).replace("</", "<\\/")
        + "</script>"
        if json_ld
        else ""
    )
    image = (
        f'<meta property="og:image" content="{esc(og_image)}">\n'
        f'  <meta name="twitter:card" content="summary_large_image">'
        if og_image
        else '<meta name="twitter:card" content="summary">'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(description)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:site_name" content="londo">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(description)}">
  <meta property="og:url" content="{esc(canonical)}">
  {image}
  <link rel="icon" type="image/png" href="{css_prefix}/icons/favicon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{css_prefix}/styles.css">
  {ld}
  {goatcounter_snippet()}
</head>
<body class="static-page">
  <div class="sky" aria-hidden="true"><div class="blob blob-a"></div><div class="blob blob-b"></div><div class="grain"></div></div>
  <header class="topbar"><h1><a href="{BASE_URL}/" style="text-decoration:none;color:inherit">londo</a></h1></header>
  <main style="max-width:720px;margin:0 auto;padding:1rem 1.5rem 4rem">
  {body}
  </main>
  <footer><p><a href="{BASE_URL}/">londo</a> — in-person london gatherings that connect and inspire</p></footer>
</body>
</html>
"""


def event_page(event: dict) -> str:
    canonical = f"{BASE_URL}/e/{slug(event)}.html"
    when = fmt_when(event)
    where = ", ".join(
        p for p in (event.get("venue_name"), event.get("address")) if p
    )
    description = (
        event.get("hook")
        or re.sub(r"\s+", " ", event.get("description") or "")[:200]
        or f"{when} at {where or 'London'}"
    )
    price = (
        "Free"
        if event.get("is_free")
        else (
            f"£{event['price_min']}"
            + (
                f"–£{event['price_max']}"
                if event.get("price_max") not in (None, event.get("price_min"))
                else ""
            )
            if event.get("price_min") is not None
            else ""
        )
    )

    json_ld = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": event["title"],
        "startDate": event["start_at"],
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name": event.get("venue_name") or "London",
            "address": event.get("address") or "London, UK",
        },
        "url": canonical,
    }
    if event.get("end_at"):
        json_ld["endDate"] = event["end_at"]
    if event.get("image_url"):
        json_ld["image"] = [event["image_url"]]
    if event.get("description"):
        json_ld["description"] = re.sub(r"\s+", " ", event["description"])[:500]
    if event.get("organizer_name"):
        json_ld["organizer"] = {
            "@type": "Organization",
            "name": event["organizer_name"],
        }
    if event.get("price_min") is not None:
        json_ld["offers"] = {
            "@type": "Offer",
            "price": str(event["price_min"]),
            "priceCurrency": "GBP",
            "url": event["source_url"],
        }

    hook = (
        f'<p style="font-family:Fraunces,Georgia,serif;font-style:italic;'
        f'font-size:1.15rem;color:var(--gold)">{esc(event.get("hook"))}</p>'
        if event.get("hook")
        else ""
    )
    img = (
        f'<img src="{esc(event["image_url"])}" alt="" '
        'style="width:100%;border-radius:18px;margin:1rem 0">'
        if event.get("image_url")
        else ""
    )
    desc_html = "".join(
        f"<p>{esc(p)}</p>"
        for p in re.split(r"\n\n+", event.get("description") or "")[:8]
        if p.strip()
    )
    meta_bits = " · ".join(
        b for b in (when, where, price, event.get("organizer_name")) if b
    )

    body = f"""
  <article>
    <h2 style="font-family:Fraunces,Georgia,serif;font-weight:480;font-size:1.9rem;margin:1.2rem 0 0.4rem">{esc(event["title"])}</h2>
    {hook}
    <p style="color:var(--ink-dim)">{esc(meta_bits)}</p>
    {img}
    {desc_html}
    <p style="margin-top:2rem"><a href="{esc(event["source_url"])}" rel="noopener"
       style="display:inline-block;padding:0.7rem 1.6rem;border-radius:999px;border:1px solid var(--peach);color:var(--peach);text-decoration:none">
       tickets &amp; details ↗</a></p>
    <p style="margin-top:1.5rem"><a href="{BASE_URL}/" style="color:var(--mauve)">← everything else on in london this week</a></p>
  </article>"""
    return page(
        f"{event['title']} — londo",
        description,
        canonical,
        event.get("image_url"),
        body,
        json_ld,
    )


def listing_page(label: str, seo_title: str, canonical: str,
                 events: list[dict]) -> str:
    items = "".join(
        f"""
    <li style="margin:1.1rem 0;list-style:none">
      <a href="{BASE_URL}/e/{slug(e)}.html" style="color:var(--ink);text-decoration:none;font-family:Fraunces,Georgia,serif;font-size:1.15rem">{esc(e["title"])}</a>
      <p style="margin:0.15rem 0 0;color:var(--ink-dim);font-size:0.9rem">{esc(fmt_when(e))} · {esc(e.get("venue_name") or e.get("address") or "London")}</p>
      {f'<p style="margin:0.15rem 0 0;font-style:italic;color:var(--gold);font-size:0.92rem">{esc(e["hook"])}</p>' if e.get("hook") else ""}
    </li>"""
        for e in events[:60]
    )
    body = f"""
  <h2 style="font-family:Fraunces,Georgia,serif;font-weight:480;font-size:1.8rem;margin:1.2rem 0 0.3rem">{esc(label)} — {esc(seo_title.lower())}</h2>
  <p style="color:var(--ink-dim)">{len(events)} upcoming · updated several times a day</p>
  <ul style="padding:0">{items}</ul>
  <p style="margin-top:2rem"><a href="{BASE_URL}/" style="color:var(--mauve)">← all of londo</a></p>"""
    return page(
        f"{seo_title} — londo",
        f"{seo_title}: {len(events)} upcoming events, updated several times a day.",
        canonical,
        None,
        body,
    )


def build(outdir: Path) -> None:
    events = fetch_events()
    print(f"Building site with {len(events)} events")

    if outdir.exists():
        shutil.rmtree(outdir)
    shutil.copytree(ROOT / "web", outdir)

    (outdir / "e").mkdir()
    urls = [f"{BASE_URL}/"]
    for event in events:
        (outdir / "e" / f"{slug(event)}.html").write_text(event_page(event))
        urls.append(f"{BASE_URL}/e/{slug(event)}.html")

    (outdir / "c").mkdir()
    for key, (label, seo_title) in CATEGORIES.items():
        cat_events = [e for e in events if e.get("category") == key]
        if not cat_events:
            continue
        canonical = f"{BASE_URL}/c/{key}.html"
        (outdir / "c" / f"{key}.html").write_text(
            listing_page(label, seo_title, canonical, cat_events)
        )
        urls.append(canonical)

    (outdir / "t").mkdir()
    for key, (slug_, seo_title) in TOPICS.items():
        topic_events = [e for e in events if key in (e.get("topics") or [])]
        if not topic_events:
            continue
        canonical = f"{BASE_URL}/t/{slug_}.html"
        (outdir / "t" / f"{slug_}.html").write_text(
            listing_page(key, seo_title, canonical, topic_events)
        )
        urls.append(canonical)

    today = datetime.now(timezone.utc).date().isoformat()
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(
            f"  <url><loc>{html.escape(u)}</loc><lastmod>{today}</lastmod></url>\n"
            for u in urls
        )
        + "</urlset>\n"
    )
    (outdir / "sitemap.xml").write_text(sitemap)
    (outdir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n"
    )
    print(f"Wrote {len(urls)} pages -> {outdir}")


if __name__ == "__main__":
    build(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build")
