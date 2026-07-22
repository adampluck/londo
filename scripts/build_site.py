"""Build a deployable site: web/ (plus the site's overlay) and static
per-event and per-listing pages with OG/meta tags, JSON-LD, and a sitemap —
so Google (and WhatsApp link unfurls) can see what the client-side app
renders.

Pages are written as directory indexes (e/<name-slug>/index.html) so URLs
drop the .html extension on GitHub Pages: /e/<event-name>/ and /t/<topic>/.

Stdlib only, so CI needs no installs:
    python3 scripts/build_site.py [--site londo|psyconnect] [outdir]
Reads Supabase credentials from the site's config.js (public anon key).
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

ROOT = Path(__file__).resolve().parent.parent

# Each site is web/ plus an optional overlay directory copied on top; its
# config.js may carry a SITE block (strict JSON between the SITE-JSON
# markers) that filters which events the site shows — the same block the
# SPA reads, so the two can't diverge.
SITES = {
    "londo": {
        "base_url": "https://adampluck.github.io/londo",
        "name": "londo",
        "tagline": "the other london, in person",
        "overlay": None,
        "config": ROOT / "web" / "config.js",
        "outdir": ROOT / "build",
    },
    "psyconnect": {
        "base_url": "https://psyconnect.london",
        "name": "psyconnect",
        "tagline": "consciousness, connection, ceremony & psychedelics — in person in london",
        "overlay": ROOT / "sites" / "psyconnect",
        "config": ROOT / "sites" / "psyconnect" / "config.js",
        "outdir": ROOT / "build-psyconnect",
        "utm": True,
    },
}

# iOS launch screens. The PNGs are pre-generated (scripts/gen_splash.py,
# needs Pillow) and committed under each site's splash/ dir; here we only
# emit the matching <link> tags — pure string work, so the CI build stays
# stdlib-only. Keep (pt_w, pt_h, dpr) in sync with gen_splash.py DEVICES.
STARTUP_DEVICES = [
    (375, 667, 2),
    (414, 736, 3),
    (375, 812, 3),
    (414, 896, 2),
    (414, 896, 3),
    (390, 844, 3),
    (428, 926, 3),
    (393, 852, 3),
    (430, 932, 3),
    (402, 874, 3),
    (440, 956, 3),
]
STARTUP_MARKER = "<!-- APPLE-STARTUP-IMAGES:"


def inject_startup_images(outdir: Path) -> None:
    """Replace the APPLE-STARTUP-IMAGES marker comment in index.html with
    per-device apple-touch-startup-image links, one per committed splash PNG."""
    index = outdir / "index.html"
    text = index.read_text()
    start = text.find(STARTUP_MARKER)
    if start == -1:
        return  # no marker (e.g. a site without the splash treatment)
    end = text.find("-->", start)
    if end == -1:
        return
    end += len("-->")
    links = []
    for pt_w, pt_h, dpr in STARTUP_DEVICES:
        px = f"{pt_w * dpr}x{pt_h * dpr}"
        media = (
            f"(device-width: {pt_w}px) and (device-height: {pt_h}px) "
            f"and (-webkit-device-pixel-ratio: {dpr}) "
            f"and (orientation: portrait)"
        )
        links.append(
            f'<link rel="apple-touch-startup-image" '
            f'media="{media}" href="splash/splash-{px}.png">'
        )
    index.write_text(text[:start] + "\n  ".join(links) + text[end:])


# set from SITES by main(); the script builds one site per invocation
BASE_URL = SITES["londo"]["base_url"]
SITE = SITES["londo"]
SITE_JSON: dict = {}

CATEGORIES = {
    "move": ("move", "Ecstatic dance, movement & embodiment events in London"),
    "connect": ("connect", "Authentic relating, circles & community events in London"),
    "expand": ("expand", "Breathwork, meditation & consciousness events in London"),
    "think": ("think", "AI, philosophy & ideas events in London"),
    "make": ("make", "Workshops, crafts & creative events in London"),
}

# Warm intro copy for /c/<category>/ pages (1–2 short paragraphs).
CATEGORY_INTROS = {
    "move": (
        "Bodies first. These are the nights and mornings when London moves — "
        "ecstatic dance floors, 5Rhythms waves, yoga that feels like play, "
        "contact improv and everything in between.",
        "No performance required. Show up as you are, follow what feels good, "
        "and leave a little more awake than you arrived.",
    ),
    "connect": (
        "For the people who miss real conversation. Circles, authentic relating, "
        "shared tables and soft socials where the point is each other — "
        "not networking, not small talk that goes nowhere.",
        "Come curious. Leave with a face you recognise next time.",
    ),
    "expand": (
        "Quiet rooms, deep breath, altered edges. Breathwork, meditation, "
        "sound baths, ceremony and the soft practices that open something "
        "wider than the usual week.",
        "In person, in London — chosen for presence, not spectacle.",
    ),
    "think": (
        "Salons, talks and long-form evenings for people who like their "
        "ideas with other humans in the room. Philosophy, AI, science, "
        "civic chat — without the webinar energy.",
        "Bring a question. Stay for the conversation after.",
    ),
    "make": (
        "Hands busy, mind quieter. Workshops, craft, song and making "
        "things together — the kind of evening where you leave with "
        "something you built, not just a ticket stub.",
        "No portfolio needed. Just show up ready to try.",
    ),
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

# Warm intro copy for /t/<topic>/ pages.
TOPIC_INTROS = {
    "psychedelics": (
        "Talks, integration circles, community nights and careful "
        "conversations about plant medicine and psychedelic culture in London — "
        "education and connection, in person.",
        "A gentle way in if you're curious, and a place to land if you've "
        "already been out there.",
    ),
    "consciousness": (
        "Explorations of mind, awareness and the odd miracle of being awake. "
        "From contemplative evenings to lively salons — always with other "
        "people in the room.",
    ),
    "connection & intimacy": (
        "Spaces for relating with a bit more honesty. Circles, workshops and "
        "gatherings about friendship, intimacy and the courage to be seen.",
        "Come as you are. Leave a little less alone in the city.",
    ),
    "tech & ai": (
        "Builders, thinkers and the quietly obsessed — in-person nights about "
        "AI, tools and the future, without another Zoom grid.",
    ),
    "startups & work": (
        "Founders, side projects and the people building things in London. "
        "Meetups and evenings that feel human, not like a pitch deck.",
    ),
    "arts & creativity": (
        "Making, looking, listening. Creative gatherings for anyone who "
        "wants art in their week, not only on a gallery wall.",
    ),
    "music & sound": (
        "Sound baths, live rooms, shared listening and the evenings where "
        "music is the medicine. Ears open, phones down if you can.",
    ),
    "nature & outdoors": (
        "Parks, walks and outdoor rituals — London still has green edges "
        "if you know where to look. Come for the sky and the company.",
    ),
    "healing & wellbeing": (
        "Gentle practices for nervous systems that live in a loud city. "
        "Bodywork, breath, rest and care — in person, at a human pace.",
    ),
    "spirituality & ritual": (
        "Ceremony, ritual and the sacred ordinary. Cacao, prayer, seasonal "
        "gatherings and rooms held with intention.",
        "You don't need a fixed belief — only a little openness.",
    ),
    "society & politics": (
        "Civic conversation without the shouty timeline. Evenings about "
        "how we live together, face to face.",
    ),
    "science & ideas": (
        "Curiosity as a social sport. Talks and salons where science and "
        "big ideas get a pint and a good audience.",
    ),
}

# Nested static pages live at e/<id>/index.html → css/assets two levels up.
NESTED_PREFIX = "../.."


def read_config() -> tuple[str, str]:
    text = SITE["config"].read_text()
    url = re.search(r'SUPABASE_URL:\s*"([^"]+)"', text).group(1)
    key = re.search(r'"(eyJ[^"]+)"', text).group(1)
    return url, key


def read_site_block() -> dict:
    m = re.search(
        r"/\*SITE-JSON\*/(.*?)/\*END-SITE-JSON\*/",
        SITE["config"].read_text(),
        re.DOTALL,
    )
    return json.loads(m.group(1)) if m else {}


def site_match(event: dict) -> bool:
    """Mirror of siteMatch() in web/app.js — keep the two in step."""
    org = (event.get("organizer_name") or "").lower()
    featured = SITE_JSON.get("featured") or {}
    if any(org == o.lower() for o in featured.get("organizers") or []):
        return True
    flt = SITE_JSON.get("filter")
    if not flt:
        return True
    hay = " ".join(
        p
        for p in (
            event.get("title") or "",
            event.get("organizer_name") or "",
            event.get("hook") or "",
            event.get("description") or "",
            " ".join(event.get("tags") or []),
        )
        if p
    ).lower()
    if any(term in hay for term in flt.get("exclude") or []):
        return False

    topics = event.get("topics") or []
    techish = ("tech & ai", "startups & work")
    strong_scene = (
        "psychedelics",
        "consciousness",
        "spirituality & ritual",
        "connection & intimacy",
    )
    # "healing & wellbeing" + tech is how health hackathons leak in
    if (
        any(t in techish for t in topics)
        and not any(t in strong_scene for t in topics)
        and event.get("category") != "expand"
    ):
        return False

    if event.get("category") in (flt.get("categories") or []):
        return True
    return any(t in (flt.get("topics") or []) for t in topics)


def goatcounter_snippet() -> str:
    text = SITE["config"].read_text()
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
            "hidden": "is.false",
        }
    )
    req = urllib.request.Request(
        f"{supabase_url}/rest/v1/events?{query}",
        headers={"apikey": anon_key, "Authorization": f"Bearer {anon_key}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


# Filled once per build by assign_event_slugs() — unique, title-based paths.
_EVENT_SLUGS: dict[tuple[str, str], str] = {}


def slugify_title(title: str) -> str:
    """URL-safe slug from an event name: 'PsyConnect: Park…' → 'psyconnect-park'."""
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    if len(text) > 72:
        text = text[:72].rstrip("-")
    return text or "event"


def legacy_event_id(event: dict) -> str:
    """Previous /e/<source>-<id>/ path — kept as a redirect target only."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", event["source_id"])[:48]
    return f"{event['source']}-{sid}"


def assign_event_slugs(events: list[dict]) -> dict[tuple[str, str], str]:
    """Prefer bare title slug; on collision append date, then a short unique tail."""
    used: set[str] = set()
    mapping: dict[tuple[str, str], str] = {}
    for event in events:
        base = slugify_title(event.get("title") or "event")
        day = ""
        if event.get("start_at"):
            day = event["start_at"][:10]  # YYYY-MM-DD
        short = re.sub(r"[^A-Za-z0-9]", "", event.get("source_id") or "")[-8:]
        candidates = [base]
        if day:
            candidates.append(f"{base}-{day}")
        if day and short:
            candidates.append(f"{base}-{day}-{short.lower()}")
        candidates.append(f"{base}-{legacy_event_id(event).lower()}")

        chosen = None
        for c in candidates:
            if c and c not in used:
                chosen = c
                break
        if chosen is None:
            n = 2
            while f"{base}-{n}" in used:
                n += 1
            chosen = f"{base}-{n}"
        used.add(chosen)
        mapping[(event["source"], event["source_id"])] = chosen
    return mapping


def event_slug(event: dict) -> str:
    key = (event["source"], event["source_id"])
    if key in _EVENT_SLUGS:
        return _EVENT_SLUGS[key]
    return slugify_title(event.get("title") or "event")


def event_url(event: dict) -> str:
    return f"{BASE_URL}/e/{event_slug(event)}/"


def topic_url(slug_: str) -> str:
    return f"{BASE_URL}/t/{slug_}/"


def category_url(key: str) -> str:
    return f"{BASE_URL}/c/{key}/"


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def with_utm(url: str) -> str:
    """Tag an outbound event link so organisers can see referral traffic
    from this site in their own analytics — only sites with utm=True."""
    if not SITE.get("utm") or not url:
        return url
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not k.startswith("utm_")]
    query += [("utm_source", "psyconnect.london"), ("utm_medium", "referral")]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


def _start_london(event: dict) -> datetime:
    start = datetime.fromisoformat(event["start_at"].replace("Z", "+00:00"))
    return start.astimezone(LONDON)


def fmt_when(event: dict) -> str:
    start = _start_london(event)
    if event.get("is_all_day"):
        return start.strftime("%A %-d %B %Y")
    return start.strftime("%A %-d %B %Y · %H:%M")


def fmt_when_short(event: dict) -> str:
    start = _start_london(event)
    if event.get("is_all_day"):
        return start.strftime("%a %-d %b")
    return start.strftime("%a %-d %b · %H:%M")


def write_index(path: Path, html_text: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text(html_text)


def write_html_redirect(old_file: Path, new_url: str) -> None:
    """Keep old .html URLs alive for crawlers that already indexed them."""
    old_file.parent.mkdir(parents=True, exist_ok=True)
    old_file.write_text(
        f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Moved</title>
  <link rel="canonical" href="{esc(new_url)}">
  <meta http-equiv="refresh" content="0;url={esc(new_url)}">
</head>
<body>
  <p><a href="{esc(new_url)}">This page has moved</a>.</p>
</body>
</html>
"""
    )


def page(
    title: str,
    description: str,
    canonical: str,
    og_image: str | None,
    body: str,
    json_ld: dict | None = None,
    css_prefix: str = NESTED_PREFIX,
) -> str:
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
  <meta property="og:site_name" content="{esc(SITE["name"])}">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(description)}">
  <meta property="og:url" content="{esc(canonical)}">
  {image}
  <link rel="icon" type="image/png" href="{css_prefix}/icons/favicon.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,300..800&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{css_prefix}/styles.css">
  {extra_css(css_prefix)}
  {ld}
  {goatcounter_snippet()}
</head>
<body class="static-page">
  <div class="sky" aria-hidden="true"><div class="blob blob-a"></div><div class="blob blob-b"></div><div class="grain"></div></div>
  <header class="static-header">
    <a class="static-brand" href="{BASE_URL}/">{site_wordmark(css_prefix)}</a>
    <p class="static-tagline">{esc(SITE["tagline"])}</p>
  </header>
  <main class="static-main">
  {body}
  </main>
  <footer class="static-footer">
    {seo_nav_html()}
    <p class="static-footer-home"><a href="{BASE_URL}/">{esc(SITE["name"])}</a> — {esc(SITE["tagline"])}</p>
  </footer>
</body>
</html>
"""


def seo_nav_html() -> str:
    """Topic links for static pages — same set the SPA footer renders."""
    site_topics = SITE_JSON.get("topics")
    keys = [k for k in TOPICS if site_topics is None or k in site_topics]
    if not keys:
        return ""
    parts = []
    for i, key in enumerate(keys):
        slug_, _ = TOPICS[key]
        if i:
            parts.append('<span class="seo-sep" aria-hidden="true">·</span>')
        parts.append(f'<a href="{topic_url(slug_)}">{esc(key)}</a>')
    return f'<nav class="seo-nav" aria-label="topics">{"".join(parts)}</nav>'


def site_wordmark(css_prefix: str) -> str:
    logo = SITE_JSON.get("logo")
    if not logo:
        return esc(SITE["name"])
    return (
        f'<img src="{css_prefix}/{esc(logo)}" alt="{esc(SITE["name"])}" '
        'class="static-logo">'
    )


def extra_css(css_prefix: str) -> str:
    return "\n  ".join(
        f'<link rel="stylesheet" href="{css_prefix}/{esc(sheet)}">'
        for sheet in SITE_JSON.get("shellExtras") or []
        if sheet.endswith(".css")
    )


def fmt_price(event: dict) -> str:
    if event.get("is_free"):
        return "Free"
    if event.get("price_min") is None:
        return ""
    low = event["price_min"]
    high = event.get("price_max")
    if high is not None and high != low:
        return f"£{low:g}–£{high:g}"
    return f"£{low:g}"


def topic_chips(event: dict) -> str:
    site_topics = SITE_JSON.get("topics")
    chips = []
    for t in event.get("topics") or []:
        if t not in TOPICS:
            continue
        if site_topics is not None and t not in site_topics:
            continue
        slug_, _ = TOPICS[t]
        chips.append(
            f'<a class="static-chip" href="{topic_url(slug_)}">{esc(t)}</a>'
        )
    cat = event.get("category")
    if cat and cat in CATEGORIES and not SITE_JSON.get("filter"):
        chips.insert(
            0,
            f'<a class="static-chip" href="{category_url(cat)}">{esc(cat)}</a>',
        )
    if not chips:
        return ""
    return f'<p class="static-chips">{"".join(chips)}</p>'


def event_page(event: dict) -> str:
    canonical = event_url(event)
    when = fmt_when(event)
    venue = event.get("venue_name") or ""
    address = event.get("address") or ""
    where = ", ".join(p for p in (venue, address) if p) or "London"
    price = fmt_price(event)
    org = event.get("organizer_name") or ""
    description = (
        event.get("hook")
        or re.sub(r"\s+", " ", event.get("description") or "")[:200]
        or f"{when} at {where}"
    )
    if len(description) > 160:
        description = description[:157].rsplit(" ", 1)[0] + "…"

    json_ld: dict = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": event["title"],
        "startDate": event["start_at"],
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "eventStatus": "https://schema.org/EventScheduled",
        "location": {
            "@type": "Place",
            "name": venue or "London",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": address or None,
                "addressLocality": "London",
                "addressCountry": "GB",
            },
        },
        "url": canonical,
        "inLanguage": "en-GB",
    }
    # strip nulls from nested address
    addr = json_ld["location"]["address"]
    json_ld["location"]["address"] = {k: v for k, v in addr.items() if v}
    if event.get("end_at"):
        json_ld["endDate"] = event["end_at"]
    if event.get("image_url"):
        json_ld["image"] = [event["image_url"]]
    if event.get("description"):
        json_ld["description"] = re.sub(r"\s+", " ", event["description"])[:500]
    elif event.get("hook"):
        json_ld["description"] = event["hook"]
    if org:
        json_ld["organizer"] = {"@type": "Organization", "name": org}
    if event.get("is_free"):
        json_ld["isAccessibleForFree"] = True
        json_ld["offers"] = {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "GBP",
            "url": event["source_url"],
            "availability": "https://schema.org/InStock",
        }
    elif event.get("price_min") is not None:
        json_ld["offers"] = {
            "@type": "Offer",
            "price": str(event["price_min"]),
            "priceCurrency": "GBP",
            "url": event["source_url"],
            "availability": "https://schema.org/InStock",
        }

    facts = []
    facts.append(
        f'<div class="static-fact"><dt>When</dt><dd>{esc(when)}</dd></div>'
    )
    facts.append(
        f'<div class="static-fact"><dt>Where</dt><dd>{esc(where)}</dd></div>'
    )
    if price:
        facts.append(
            f'<div class="static-fact"><dt>Price</dt><dd>{esc(price)}</dd></div>'
        )
    if org:
        facts.append(
            f'<div class="static-fact"><dt>Host</dt><dd>{esc(org)}</dd></div>'
        )

    hook = (
        f'<p class="static-hook">{esc(event["hook"])}</p>'
        if event.get("hook")
        else ""
    )
    img = (
        f'<figure class="static-figure"><img src="{esc(event["image_url"])}" '
        f'alt="{esc(event["title"])}" loading="lazy"></figure>'
        if event.get("image_url")
        else ""
    )
    paragraphs = [
        p.strip()
        for p in re.split(r"\n\n+", event.get("description") or "")
        if p.strip()
    ][:12]
    if paragraphs:
        desc_html = (
            '<div class="static-prose">'
            + "".join(f"<p>{esc(p)}</p>" for p in paragraphs)
            + "</div>"
        )
    else:
        desc_html = (
            '<div class="static-prose">'
            f"<p>{esc(event.get('hook') or 'An in-person gathering in London.')}</p>"
            "</div>"
        )

    area = event.get("area")
    kicker_bits = ["in person", "London"]
    if area:
        kicker_bits.append(f"{area} London")
    if event.get("category") and not SITE_JSON.get("filter"):
        kicker_bits.append(event["category"])

    body = f"""
  <nav class="static-crumbs" aria-label="breadcrumb">
    <a href="{BASE_URL}/">{esc(SITE["name"])}</a>
    <span aria-hidden="true">/</span>
    <span>event</span>
  </nav>
  <article class="static-event">
    <p class="static-kicker">{esc(" · ".join(kicker_bits))}</p>
    <h1 class="static-title">{esc(event["title"])}</h1>
    {hook}
    <dl class="static-facts">{"".join(facts)}</dl>
    {topic_chips(event)}
    {img}
    {desc_html}
    <p class="static-cta-wrap">
      <a class="static-cta" href="{esc(with_utm(event["source_url"]))}" rel="noopener">
        tickets &amp; details ↗
      </a>
    </p>
    <p class="static-back">
      <a href="{BASE_URL}/">← more in-person gatherings on {esc(SITE["name"])}</a>
    </p>
  </article>"""
    return page(
        f"{event['title']} — {SITE['name']}",
        description,
        canonical,
        event.get("image_url"),
        body,
        json_ld,
    )


def listing_intro_paragraphs(key: str, kind: str, label: str, count: int) -> list[str]:
    """Warm prose for listing pages, plus a light freshness line."""
    if kind == "category":
        paras = list(CATEGORY_INTROS.get(key) or ())
    else:
        paras = list(TOPIC_INTROS.get(key) or ())
    if not paras:
        paras = [
            f"In-person {label} gatherings in London, collected on {SITE['name']} "
            f"so you can find the good rooms without scrolling forever."
        ]
    n = count
    freshness = (
        f"{n} upcoming right now — times, venues and tickets, "
        f"refreshed several times a day."
        if n != 1
        else "One upcoming right now — times, venue and tickets below."
    )
    return [*paras, freshness]


def listing_page(
    key: str,
    label: str,
    seo_title: str,
    canonical: str,
    events: list[dict],
    kind: str = "topic",
) -> str:
    paras = listing_intro_paragraphs(key, kind, label, len(events))
    lead_html = "".join(f'<p class="static-lead">{esc(p)}</p>' for p in paras)
    # meta description: first paragraph, kept short
    meta_desc = paras[0]
    if len(meta_desc) > 160:
        meta_desc = meta_desc[:157].rsplit(" ", 1)[0] + "…"

    items = []
    for e in events[:60]:
        when = fmt_when_short(e)
        place = e.get("venue_name") or e.get("address") or "London"
        hook = (
            f'<p class="static-list-hook">{esc(e["hook"])}</p>'
            if e.get("hook")
            else ""
        )
        org = (
            f'<span class="static-list-org">{esc(e["organizer_name"])}</span>'
            if e.get("organizer_name")
            else ""
        )
        items.append(
            f"""
    <li class="static-list-item">
      <a class="static-list-title" href="{event_url(e)}">{esc(e["title"])}</a>
      <p class="static-list-meta">{esc(when)} · {esc(place)}{(' · ' + org) if org else ''}</p>
      {hook}
    </li>"""
        )

    body = f"""
  <nav class="static-crumbs" aria-label="breadcrumb">
    <a href="{BASE_URL}/">{esc(SITE["name"])}</a>
    <span aria-hidden="true">/</span>
    <span>{esc(kind)}</span>
  </nav>
  <header class="static-list-head">
    <p class="static-kicker">in person · London</p>
    <h1 class="static-title">{esc(seo_title)}</h1>
    <div class="static-intro">
      {lead_html}
    </div>
  </header>
  <ol class="static-list">
    {"".join(items)}
  </ol>
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(SITE["name"])}</a>
  </p>"""
    return page(
        f"{seo_title} — {SITE['name']}",
        meta_desc,
        canonical,
        None,
        body,
    )


def build(outdir: Path) -> None:
    global _EVENT_SLUGS
    events = [e for e in fetch_events() if site_match(e)]
    print(f"Building {SITE['name']} with {len(events)} events")

    if outdir.exists():
        shutil.rmtree(outdir)
    shutil.copytree(ROOT / "web", outdir)
    if SITE["overlay"]:
        shutil.copytree(SITE["overlay"], outdir, dirs_exist_ok=True)
    inject_startup_images(outdir)

    _EVENT_SLUGS = assign_event_slugs(events)
    urls = [f"{BASE_URL}/"]

    for event in events:
        slug = event_slug(event)
        canonical = event_url(event)
        write_index(outdir / "e" / slug, event_page(event))
        # legacy source-id paths keep working for old sitemaps / shares
        legacy = legacy_event_id(event)
        if legacy != slug:
            write_html_redirect(outdir / "e" / f"{legacy}.html", canonical)
            write_index(
                outdir / "e" / legacy,
                (
                    f'<!doctype html><html lang="en"><head>'
                    f'<meta charset="utf-8">'
                    f'<link rel="canonical" href="{esc(canonical)}">'
                    f'<meta http-equiv="refresh" content="0;url={esc(canonical)}">'
                    f"</head><body>"
                    f'<p><a href="{esc(canonical)}">This page has moved</a>.</p>'
                    f"</body></html>"
                ),
            )
        write_html_redirect(outdir / "e" / f"{slug}.html", canonical)
        urls.append(canonical)

    # category pages only make sense when the site spans all categories;
    # on a filtered site one of them would just mirror the homepage
    if not SITE_JSON.get("filter"):
        for key, (label, seo_title) in CATEGORIES.items():
            cat_events = [e for e in events if e.get("category") == key]
            if not cat_events:
                continue
            canonical = category_url(key)
            write_index(
                outdir / "c" / key,
                listing_page(
                    key, label, seo_title, canonical, cat_events, kind="category"
                ),
            )
            write_html_redirect(outdir / "c" / f"{key}.html", canonical)
            urls.append(canonical)

    site_topics = SITE_JSON.get("topics")
    for key, (slug_, seo_title) in TOPICS.items():
        if site_topics is not None and key not in site_topics:
            continue
        topic_events = [e for e in events if key in (e.get("topics") or [])]
        if not topic_events:
            continue
        canonical = topic_url(slug_)
        write_index(
            outdir / "t" / slug_,
            listing_page(
                key, key, seo_title, canonical, topic_events, kind="topic"
            ),
        )
        write_html_redirect(outdir / "t" / f"{slug_}.html", canonical)
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


def main() -> None:
    global BASE_URL, SITE, SITE_JSON
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", choices=sorted(SITES), default="londo")
    parser.add_argument("outdir", nargs="?", type=Path)
    args = parser.parse_args()

    SITE = SITES[args.site]
    BASE_URL = SITE["base_url"]
    SITE_JSON = read_site_block()
    build(args.outdir or SITE["outdir"])


if __name__ == "__main__":
    main()
