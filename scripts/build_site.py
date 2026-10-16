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
import statistics
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
        # londo and psyconnect list the same events, so leaving both open
        # to search would have them competing as duplicates. psyconnect is
        # the one being optimised; londo asks to be left out of the index.
        "noindex": True,
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
THEME_MARKER = "<!-- THEME-BOOT:"
THEME_BOOT_FILE = "theme-boot.js"
# set by build() from the site's theme-boot.js, if it ships one; inlined into
# every page's <head> so a saved light/dark choice paints on the first frame
THEME_BOOT: str = ""
# the shell's light-mode theme-color, mirrored onto the static pages
THEME_COLOR: str = ""


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


def load_theme_boot(outdir: Path) -> str:
    """Read the site's theme-boot.js and drop the standalone copy — it is
    only ever used inlined, and shipping it too would just be a stale twin."""
    src = outdir / THEME_BOOT_FILE
    if not src.exists():
        return ""
    code = src.read_text()
    src.unlink()
    return code


def theme_boot_tag() -> str:
    """The boot script as an inline <script>, or "" for sites without one."""
    if not THEME_BOOT:
        return ""
    # "</" cannot appear inside an inline script without closing it early
    return "<script>" + THEME_BOOT.replace("</", "<\\/") + "</script>"


def inject_theme_boot(outdir: Path) -> None:
    """Replace the THEME-BOOT marker comment in index.html with the script."""
    index = outdir / "index.html"
    text = index.read_text()
    start = text.find(THEME_MARKER)
    if start == -1:
        return
    end = text.find("-->", start)
    if end == -1:
        return
    index.write_text(text[:start] + theme_boot_tag() + text[end + len("-->") :])


def inline_shell_css(outdir: Path) -> None:
    """Inline the SPA shell's own stylesheets into its index.html, so the
    first paint doesn't wait on three more round trips (PageSpeed's
    "render-blocking requests"). Only the shell: the static pages share
    the linked, cacheable files. url()s are rewritten from the sheet's
    folder to the page's — fonts.css points at ../fonts/, which from the
    page (at londo's /londo/ subpath, too) is fonts/."""
    index = outdir / "index.html"
    text = index.read_text()

    def inline(m: re.Match) -> str:
        href = m.group(1)
        sheet = outdir / href
        if "//" in href or not sheet.is_file():
            return m.group(0)
        css = sheet.read_text()
        folder = Path(href).parent
        if str(folder) != ".":
            css = re.sub(
                r"url\((['\"]?)\.\./",
                lambda u: f"url({u.group(1)}",
                css,
            )
        return f"<style>/* {href} */\n{css}</style>"

    text = re.sub(r'<link rel="stylesheet" href="([^"]+)">', inline, text)
    index.write_text(text)


def inject_robots_meta(outdir: Path) -> None:
    """The SPA shell is copied, not generated, so it needs the tag too."""
    meta = robots_meta()
    if not meta:
        return
    index = outdir / "index.html"
    text = index.read_text()
    if 'name="robots"' in text:
        return
    index.write_text(text.replace("<head>", f"<head>\n  {meta}", 1))


def _is_ours(event: dict) -> bool:
    """Mirror of isOurs() in web/app.js — keep the two in step."""
    org = (event.get("organizer_name") or "").lower()
    featured = SITE_JSON.get("featured") or {}
    return any(org == o.lower() for o in featured.get("organizers") or [])


def _pick_curated(
    events: list[dict], now: datetime, exclude_source_url: str | None, limit: int
) -> list[dict]:
    """Mirror of pickCurated() in web/app.js — keep the two in step (see
    there for why the window widens against max(maxTotal, maxMobile), not
    `limit`). A drift doesn't break anything, it just costs the static
    spotlight: the SPA rebuilds the row when its picks key differs."""
    cfg = SITE_JSON.get("curated")
    if not cfg or limit <= 0:
        return []
    exclude_terms = [t.lower() for t in cfg.get("exclude") or []]
    today = now.astimezone(LONDON).date()

    def eligible(e: dict, days: int, allow_today: bool = False) -> bool:
        if exclude_source_url and e.get("source_url") == exclude_source_url:
            return False
        start = _parse_ts(e["start_at"])
        if not (now < start <= now + timedelta(days=days)):
            return False
        if not allow_today and start.astimezone(LONDON).date() == today:
            return False
        title = (e.get("title") or "").lower()
        return not any(term in title for term in exclude_terms)

    def within(days: int) -> list[dict]:
        return [e for e in events if _curated(e) and eligible(e, days)]

    stages = cfg.get("windowStages") or [cfg.get("windowDays") or 7]
    widen_threshold = max(cfg.get("maxTotal") or 3, cfg.get("maxMobile") or 0)
    candidates: list[dict] = []
    for days in stages:
        candidates = within(days)
        if len(candidates) >= widen_threshold:
            break

    # curated.alwaysPick: see pickCurated() — first slots, widest window
    always_terms = [t.lower() for t in cfg.get("alwaysPick") or []]

    def is_always(e: dict) -> bool:
        text = f"{e.get('title') or ''} {e.get('description') or ''}".lower()
        return any(term in text for term in always_terms)

    pinned = [
        e for e in events if always_terms and eligible(e, stages[-1], True) and is_always(e)
    ][: min(limit, cfg.get("alwaysPickMax") or limit)]
    if always_terms:
        candidates = [e for e in candidates if not is_always(e)]
    if not candidates and not pinned:
        return []

    by_org: dict[str, list[dict]] = {}
    for e in candidates:
        by_org.setdefault((e.get("organizer_name") or "").lower(), []).append(e)
    org_order = sorted(by_org, key=lambda o: _parse_ts(by_org[o][0]["start_at"]))
    picks: list[dict] = list(pinned)
    for org in org_order:
        if len(picks) >= limit:
            break
        picks.append(by_org[org][0])
    for e in candidates:
        if len(picks) >= limit:
            break
        if not any(e is p for p in picks):
            picks.append(e)
    return sorted(picks, key=lambda e: _parse_ts(e["start_at"]))


# Mirror of THUMB_WIDTHS / SPOTLIGHT_SIZES in web/app.js.
SPOTLIGHT_WIDTHS = (360, 480, 640, 800)
SPOTLIGHT_SIZES = "(max-width: 640px) 66vw, 270px"


def spotlight_key(featured: dict | None, picks: list[dict]) -> str:
    """Mirror of spotlightKey() in web/app.js: which cards the row holds,
    so the SPA can keep the static row when the live data agrees."""
    keys = [e.get("dedupe_key") or "" for e in picks]
    if featured:
        keys.insert(0, "featured:" + (featured.get("dedupe_key") or ""))
    return "|".join(keys)


def _spotlight_heading(label: str, accent: str, cls: str, col_start: int, col_end: int) -> str:
    """Mirror of spotlightHeading() in web/app.js."""
    idx = label.find(accent) if accent else -1
    if idx == -1:
        inner = esc(label)
    else:
        inner = (
            esc(label[:idx])
            + f'<span class="when">{esc(accent)}</span>'
            + esc(label[idx + len(accent):])
        )
    return (
        f'<h2 class="day-heading spotlight-heading {cls}" '
        f'style="grid-column: {col_start} / {col_end};">{inner}</h2>'
    )


def _spotlight_card(e: dict, kind: str, index: int, style: str, extra: bool) -> str:
    """Mirror of spotlightCard() in web/app.js. Always .instant: the
    settle animation starts at opacity 0, which would hold back the LCP."""
    cls = f"spotlight-card spotlight-{kind} instant" + (" spotlight-pick-extra" if extra else "")
    # every listing in the build has its own page: the card opens it, in
    # the same tab, and the ticket link is on that page
    href = f"e/{event_slug(e)}/"
    attrs = [f'class="{cls}"', f'href="{esc(href)}"']
    if style:
        attrs.append(f'style="{style}"')
    attrs.append(f'data-goatcounter-click="open/{kind}/{esc(organizer_slug(e.get("organizer_name")))}"')
    attrs.append(f'data-goatcounter-title="{esc(e.get("title") or "")}"')

    parts = []
    if e.get("image_url"):
        art = e["image_url"]
        fallback = esc(art.replace("'", "%27"))
        priority = ' fetchpriority="high"' if index < 2 else ' loading="lazy"'
        srcset = ", ".join(f"{thumb(art, w)} {w}w" for w in SPOTLIGHT_WIDTHS)
        # thumb first, then the original, then drop the figure — as setThumb()
        parts.append(
            f'<div class="spotlight-media"><img alt=""{priority} '
            f'sizes="{SPOTLIGHT_SIZES}" srcset="{esc(srcset)}" src="{esc(thumb(art, 700))}" '
            "onerror=\"this.onerror=function(){this.parentNode.remove()};"
            f"this.removeAttribute('srcset');this.src='{fallback}'\">"
            "</div>"
        )
    body = []
    if kind == "pick" and e.get("organizer_name"):
        body.append(f'<p class="spotlight-eyebrow">{esc(e["organizer_name"])}</p>')
    body.append(f"<h3>{esc(e.get('title') or '')}</h3>")
    start = _start_london(e)
    # toLocaleDateString("en-GB") spells September "Sept"
    day = start.strftime("%a %-d %b").replace(" Sep", " Sept")
    when = " · ".join(x for x in (day, fmt_time_range(e)) if x)
    venue = (
        f'<span class="spotlight-venue"> · {esc(e["venue_name"])}</span>'
        if e.get("venue_name")
        else ""
    )
    body.append(f'<p class="spotlight-when">{esc(when)}{venue}</p>')
    blurb = (e.get("hook") or e.get("description") or "").strip()
    if blurb:
        if len(blurb) > 200:
            blurb = blurb[:197].rstrip() + "…"
        body.append(f'<p class="spotlight-blurb">{esc(blurb)}</p>')
    cue = (
        "(opens the ticket page in a new tab)"
        if e.get("source_url")
        else "(opens the event page in a new tab)"
    )
    body.append(f'<span class="sr-only">{cue}</span>')
    parts.append(f'<div class="spotlight-body">{"".join(body)}</div>')
    return f'<a {" ".join(attrs)}>{"".join(parts)}</a>'


def spotlight_html(events: list[dict], now: datetime) -> str:
    """Mirror of renderSpotlight() in web/app.js, as static markup for the
    SPA shell: the row (and its images, the page's largest paint) show from
    the HTML itself instead of waiting on app.js and the events API. Empty
    when there's nothing to spotlight."""
    horizon = now + timedelta(days=HORIZON_DAYS)
    live = [e for e in events if _parse_ts(e["start_at"]) <= horizon]
    featured = None
    if SITE_JSON.get("featured"):
        featured = next(
            (e for e in live if _is_ours(e) and _parse_ts(e["start_at"]) > now), None
        )
    curated = SITE_JSON.get("curated") or {}
    max_total = curated.get("maxTotal") or 3
    desktop_limit = max_total - 1 if featured else max_total
    pick_limit = max(max_total, curated.get("maxMobile") or 0) if curated else 0
    exclude = featured.get("source_url") if featured else None
    picks = _pick_curated(live, now, exclude, pick_limit) if pick_limit else []
    if not featured and not picks:
        return ""

    grid = []
    pick_start = 2 if featured else 1
    desktop_picks = min(len(picks), desktop_limit)
    pick_end = pick_start + max(desktop_picks, 0 if featured else 1)
    feat_cfg = SITE_JSON.get("featured") or {}
    if featured:
        grid.append(_spotlight_heading(
            feat_cfg.get("label") or "Our next event",
            feat_cfg.get("accent") or "next event",
            "spotlight-heading-featured", 1, 2,
        ))
    if picks:
        grid.append(_spotlight_heading(
            curated.get("label") or "Our top picks this week",
            curated.get("accent") or "top picks",
            "spotlight-heading-picks", pick_start, pick_end,
        ))
    if featured:
        grid.append(_spotlight_card(featured, "featured", 0, "grid-column: 1;", False))
    if picks:
        desktop_set = (
            _pick_curated(live, now, exclude, desktop_limit)
            if len(picks) > desktop_limit
            else picks
        )
        cards = []
        col = pick_start
        for i, e in enumerate(picks):
            on_desktop = any(e is d for d in desktop_set)
            style = f"grid-column: {col};" if on_desktop else ""
            if on_desktop:
                col += 1
            cards.append(_spotlight_card(e, "pick", i + 1 if featured else i, style, not on_desktop))
        grid.append(f'<div class="spotlight-picks">{"".join(cards)}</div>')

    section_cls = "" if featured else ' class="no-featured"'
    return (
        f'<div id="spotlight"{section_cls} data-key="{esc(spotlight_key(featured, picks))}">'
        '<button class="spotlight-close" type="button" aria-label="hide the picks" '
        'title="hide the picks">&times;</button>'
        f'<div class="spotlight-grid">{"".join(grid)}</div></div>'
    )


def inject_spotlight(outdir: Path, events: list[dict]) -> None:
    """Swap the shell's empty #spotlight for the built row (or hide it when
    there's none). Only for sites that spotlight anything."""
    if not (SITE_JSON.get("featured") or SITE_JSON.get("curated")):
        return
    index = outdir / "index.html"
    text = index.read_text()
    row = spotlight_html(events, datetime.now(timezone.utc)) or '<div id="spotlight" hidden></div>'
    text, n = re.subn(r'<div id="spotlight"[^>]*></div>', lambda _: row, text, count=1)
    if n:
        index.write_text(text)


# set from SITES by main(); the script builds one site per invocation
BASE_URL = SITES["londo"]["base_url"]
SITE = SITES["londo"]
SITE_JSON: dict = {}
# set by build() once the site's files are copied into outdir; used as the
# og:image fallback for pages (events without their own image, listings)
# when the site ships an og-image.jpg
DEFAULT_OG_IMAGE: str | None = None

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
# Practice pages (/p/<slug>/): the level people actually search at —
# "cacao ceremonies in London", not "spirituality & ritual". Each entry
# is matched against the live listings by keyword, so a page only exists
# on a site whose events support it, and re-shapes itself every build.
#   terms:   matched on title + organizer + tags, at the start of a word
#   deep:    also matched in the description; only phrases distinctive
#            enough that a passing mention really is the practice
#   exclude: guards against a false friend ("gongfu" for "gong")
#   min_events: overrides MIN_PRACTICE_EVENTS for a practice whose scene
#            is small but steady (one or two dialogues a month)
#   tags:    False to match terms on title + organizer only — some hosts
#            tag everything they run with every practice they know
#   price_q: the FAQ question the live price answer goes under, when
#            "How much does <label> cost in London?" reads badly
# The prose is the point: it answers the query in its first sentence so
# a search engine — or a model quoting one paragraph — has something to
# lift. Keep it concrete and keep it honest about prices and safety.
PRACTICES = {
    # most listed first, all time, psyconnect's events (counted 2026-10-12):
    # the order of the homepage rail, filter row and explore tiles
    "ecstatic-dance": {
        "label": "ecstatic dance",
        "seo_title": "Ecstatic dance in London",
        "terms": ["ecstatic dance", "ecstatic rave", "conscious dance"],
        "deep": ["ecstatic dance"],
        "intro": (
            "Ecstatic dance is a freeform dance floor with no talking, no "
            "alcohol and no steps to learn. A DJ builds a wave from slow and "
            "grounded to fast and loud and back down again, and everyone moves "
            "however their body wants to for a couple of hours.",
            "London has one most nights of the week, from sunrise sessions to "
            "Friday and Saturday floors of two hundred people. Many open with "
            "cacao or a short warm-up circle and close with a lie-down.",
        ),
        "faq": [
            (
                "What is ecstatic dance?",
                "A sober, freeform dance practice: no choreography, no "
                "conversation on the floor, and no drinking. The DJ's set is "
                "arranged as a journey through tempos so the room moves together "
                "without anyone leading. It grew out of the 5Rhythms and conscious "
                "dance scenes of the 1970s and 80s.",
            ),
            (
                "Do I need to be able to dance?",
                "No. There is nothing to get right and nobody is watching — most "
                "people dance with their eyes half closed. Walking, stretching or "
                "lying down at the edge of the room all count.",
            ),
            (
                "What are the rules at an ecstatic dance?",
                "Three, almost everywhere: no talking on the dance floor, no shoes, "
                "and no alcohol or drugs. Dancing with someone else is welcome but "
                "always invited rather than assumed — a hand raised palm-out is the "
                "usual way to decline, and it isn't taken personally.",
            ),
        ],
    },
    "5rhythms": {
        "label": "5Rhythms",
        "seo_title": "5Rhythms classes in London",
        # a kind of ecstatic dance: the dance page covers it, this one
        # is the narrower door for people who know the name
        "parent": "ecstatic-dance",
        "terms": ["5rhythms", "five rhythms", "5 rhythms"],
        "deep": ["5rhythms"],
        "intro": (
            "5Rhythms is a moving meditation created by Gabrielle Roth: every "
            "class travels through flowing, staccato, chaos, lyrical and "
            "stillness, a wave that takes about two hours. A teacher holds the "
            "room and offers a focus, but there are no steps to copy.",
            "London has one of the largest 5Rhythms communities anywhere, with "
            "weekly classes across the city, monthly longer waves and regular "
            "weekend workshops. Drop in to any class marked open level.",
        ),
        "faq": [
            (
                "What are the five rhythms?",
                "Flowing (continuous, circular, grounded), staccato (defined, "
                "rhythmic, expressive), chaos (letting go of control), lyrical "
                "(light and playful) and stillness (settling, breath). Danced in "
                "sequence they make a wave, the basic form of every class.",
            ),
            (
                "Is 5Rhythms suitable for beginners?",
                "Yes — most weekly classes are open level and a good share of the "
                "room is new. There is no technique to learn and no partner "
                "needed; the teacher's instructions are invitations rather than "
                "steps.",
            ),
            (
                "How is 5Rhythms different from ecstatic dance?",
                "5Rhythms is a taught practice with a fixed map and certified "
                "teachers who guide the room through the wave. Ecstatic dance is "
                "usually DJ-led and unguided. Both are sober, barefoot and "
                "freeform.",
            ),
        ],
    },
    "sound-bath": {
        "label": "sound bath",
        "seo_title": "Sound baths & gong baths in London",
        "price_q": "How much does a sound bath cost in London?",
        # bare "gong" catches Jin Mai Gong (a qigong form), so the gong
        # terms all name the session type
        "terms": [
            "sound bath", "gong bath", "sound healing", "sound journey",
            "gong sound", "gong puja", "gongs",
        ],
        "deep": ["sound bath", "gong bath"],
        "intro": (
            "A sound bath is an hour lying on the floor while someone plays "
            "gongs, singing bowls, chimes and voice around the room. There is "
            "nothing to do: the sound is long and overlapping, and most people "
            "drift between waking and sleep.",
            "London's sound baths run in yoga studios, churches and railway "
            "arches, often in the early evening. Bring warm socks — you cool "
            "down quickly lying still — and expect to leave slightly dazed.",
        ),
        "faq": [
            (
                "What is a sound bath?",
                "A group session where you lie down and listen to sustained "
                "acoustic sound — usually gongs and crystal or Tibetan bowls — for "
                "forty-five to sixty minutes. 'Bath' refers to being surrounded by "
                "sound, not to water; nothing is asked of you beyond lying still.",
            ),
            (
                "What should I bring to a sound bath?",
                "Warm layers and socks, and a blanket if the listing doesn't say "
                "one is provided. Most venues supply mats, bolsters and eye masks. "
                "Arrive early enough to settle, as latecomers are often held at the "
                "door once the gongs start.",
            ),
            (
                "Is a gong bath the same as a sound bath?",
                "A gong bath is a sound bath led mainly by gongs, which are louder "
                "and more physical than bowls — you feel them in your chest. "
                "Sessions billed as sound healing or sound journeys use a wider mix "
                "of instruments and often voice.",
            ),
        ],
    },
    "cacao-ceremony": {
        "label": "cacao ceremony",
        "chip": "cacao",
        "seo_title": "Cacao ceremonies in London",
        "price_q": "How much does a cacao ceremony cost in London?",
        "terms": ["cacao", "cocoa ceremony"],
        "deep": ["cacao ceremony", "cacao circle"],
        "intro": (
            "A cacao ceremony is a gathering where everyone drinks a cup of "
            "thick, bitter ceremonial cacao together and then sits, moves or "
            "shares for a couple of hours. The cacao is a mild heart opener, "
            "not a psychedelic: it lifts the mood, warms the chest and makes "
            "people a little more willing to be honest with each other.",
            "In London they usually run on a weekday evening or a Sunday "
            "afternoon, often paired with ecstatic dance, sound, breathwork or "
            "a sharing circle. Expect two to three hours, a room of twenty to "
            "eighty people, and no requirement to say anything at all.",
        ),
        "faq": [
            (
                "What happens at a cacao ceremony?",
                "You arrive, sit in a circle and are handed a cup of ceremonial "
                "cacao — thick, dark and unsweetened. The facilitator usually "
                "opens with an intention or a short meditation while the cacao "
                "takes effect over twenty minutes or so, then leads whatever the "
                "evening is built around: dance, sound, breathwork, journalling "
                "or open sharing. Most end with a closing circle.",
            ),
            (
                "Is cacao a drug, and is it safe?",
                "Ceremonial cacao is food, not a drug — it's unprocessed cacao "
                "with theobromine and a little caffeine, so it feels like a strong "
                "coffee with a warmer edge. It's worth telling the facilitator "
                "beforehand if you take antidepressants (particularly MAOIs), have "
                "a heart condition, or are pregnant, as they will often serve a "
                "smaller dose.",
            ),
            (
                "Can I go to a cacao ceremony on my own?",
                "Yes — most people there came alone. Facilitators build in "
                "introductions and pairs, and nobody is made to speak or touch "
                "anyone. Arriving ten minutes early makes the first circle easier.",
            ),
        ],
    },
    "breathwork": {
        "label": "breathwork",
        "seo_title": "Breathwork classes & workshops in London",
        "terms": ["breathwork", "breathing workshop", "rebirthing", "holotropic"],
        "deep": ["breathwork session", "conscious connected breathing"],
        "intro": (
            "Breathwork is a session where a facilitator guides you through a "
            "specific breathing pattern — usually connected, mouth-led and "
            "faster than normal — while you lie down with music playing. Thirty "
            "to sixty minutes of it can bring tingling, strong emotion, "
            "tears or a deep calm, and the session closes with rest and sharing.",
            "London runs everything from lunchtime classes to weekend "
            "intensives, in styles from gentle and somatic to the intense "
            "holotropic lineage. Most are two hours and need no experience.",
        ),
        "faq": [
            (
                "What happens in a breathwork session?",
                "You lie on a mat with an eye mask while the facilitator talks you "
                "into a connected breathing rhythm — in through the mouth, no pause "
                "at the top, out with a release. After twenty to forty minutes of "
                "active breathing the music softens and you rest, then the group "
                "usually shares what came up.",
            ),
            (
                "Is breathwork safe?",
                "For most people, yes, though tingling hands and a tight jaw are "
                "common and harmless. Facilitators normally ask you to check in "
                "first if you have epilepsy, cardiovascular problems, glaucoma, "
                "severe asthma, a history of psychosis, or are pregnant — the "
                "faster styles aren't recommended in those cases.",
            ),
            (
                "Do I need any experience?",
                "No. Almost every London session is open to first-timers and the "
                "facilitator explains the pattern before anything starts. You can "
                "stop and breathe normally whenever you want.",
            ),
        ],
    },
    "somatics": {
        "label": "somatic practice",
        "chip": "somatic",
        "seo_title": "Somatic workshops & embodiment in London",
        "price_q": "How much do somatic workshops cost in London?",
        # bare "embodiment" is a house word for half the scene — it was
        # pulling in breathwork and tantra nights, so the terms name the
        # practice itself
        "terms": [
            "somatic", "feldenkrais", "body-mind centering",
            "embodiment lab", "embodiment practice", "embodied movement",
        ],
        "deep": ["somatic practice", "somatic therapy", "somatic experiencing"],
        "intro": (
            "Somatic work starts from the body rather than the story: slow "
            "movement, attention to sensation, and pauses long enough to notice "
            "what shifts. It's used for trauma, stress and plain disconnection, "
            "and looks far less dramatic than it feels.",
            "London's somatic scene spans trauma-informed workshops, "
            "Feldenkrais classes, embodiment labs and movement research. Most "
            "sessions are small, and touch — where it happens at all — is "
            "always asked for first.",
        ),
        "faq": [
            (
                "What does somatic mean?",
                "Somatic simply means 'of the body'. In practice it describes "
                "approaches that work through felt sensation and movement rather "
                "than talk alone — noticing where you brace, letting a movement "
                "finish, following what the nervous system does next.",
            ),
            (
                "Is somatic work therapy?",
                "Some of it is: somatic experiencing and similar modalities are "
                "clinical approaches delivered by trained practitioners. Group "
                "workshops and embodiment classes are educational rather than "
                "therapeutic, though they often touch the same ground.",
            ),
            (
                "Will I have to be touched?",
                "Not unless you choose to be. Most London sessions are solo or "
                "guided in pairs with explicit consent, and 'I'd rather not' is a "
                "complete answer.",
            ),
        ],
    },
    "qigong": {
        "label": "qigong",
        "seo_title": "Qigong classes in London",
        "terms": ["qigong", "chi kung", "qi gong"],
        "deep": ["qigong"],
        "intro": (
            "Qigong is a Chinese practice of slow, repeated movement "
            "coordinated with breath and attention. Sequences are short and "
            "undramatic — standing, shifting weight, opening and closing the "
            "arms — and are meant to be repeated rather than perfected.",
            "London classes run in parks, community halls and studios, often "
            "early morning, and many are drop-in. Come in clothes you can move "
            "in; nothing more is needed.",
        ),
        "faq": [
            (
                "What is qigong?",
                "A practice from Chinese medicine combining gentle movement, "
                "breathing and focused attention, usually done standing. Where tai "
                "chi is a long martial form, qigong is a set of short repeatable "
                "exercises.",
            ),
            (
                "Is qigong suitable for beginners or older people?",
                "Yes — it's low impact, done standing or seated, and can be scaled "
                "to almost any level of mobility. Most London classes are mixed "
                "ability and welcome complete beginners.",
            ),
            (
                "What should I wear to a qigong class?",
                "Loose, warm clothing and flat shoes, or bare feet indoors. Outdoor "
                "classes carry on in most weather, so dress for standing still in "
                "it.",
            ),
        ],
    },
    "tantra": {
        "label": "tantra",
        "seo_title": "Tantra workshops & classes in London",
        "price_q": "How much do tantra workshops cost in London?",
        "terms": ["tantra", "tantric", "neo-tantra", "neotantra"],
        "deep": ["tantra workshop", "tantric practice"],
        # tagged onto half the intimacy scene; the title says it plainly
        "tags": False,
        "intro": (
            "Tantra workshops in London are mostly neo-tantra: a modern, "
            "Western take on the Indian tradition that works with breath, "
            "slow touch, eye contact and attention to build intimacy — with "
            "yourself, a partner or a room of strangers. An evening usually "
            "mixes guided exercises in pairs with talks on desire, "
            "boundaries and energy.",
            "Formats run from two-hour introductions and masterclasses to "
            "women's and couples' days, tantric slow dating and full "
            "weekends. Clothes stay on at almost all of them; the few that "
            "differ say so clearly on the listing.",
        ),
        "faq": [
            (
                "What happens at a tantra workshop?",
                "Most begin in a circle with an introduction and some breath "
                "or movement to settle the room. Then come exercises, often in "
                "pairs: eye gazing, synchronised breathing, giving and "
                "receiving simple touch on the hands or back, and practising "
                "asking for what you want and saying no. They close with "
                "sharing.",
            ),
            (
                "Do I need a partner, and is it sexual?",
                "No partner is needed — most people come alone, and you can "
                "sit out any exercise. Introductory workshops are clothed and "
                "non-sexual; they work with sexual energy as a theme, not as "
                "an activity. Consent is taught explicitly, and every exercise "
                "is an invitation you can decline.",
            ),
            (
                "Is tantra a religion?",
                "Classical tantra is a family of Hindu and Buddhist traditions "
                "with long lineages. The London workshops are neo-tantra, "
                "which borrows some practices and language from them but "
                "asks for no belief — closer to a relationship or embodiment "
                "class than to a religious rite.",
            ),
        ],
    },
    "family-constellations": {
        "label": "family constellations",
        "seo_title": "Family constellations workshops in London",
        "price_q": "How much does a family constellations workshop cost in London?",
        "terms": ["family constellation", "systemic constellation"],
        "deep": ["family constellation", "systemic constellation"],
        "tags": False,
        "intro": (
            "Family constellations is a group process developed by Bert "
            "Hellinger. One person brings an issue — usually about family, "
            "relationships or a repeating pattern — and chooses people from "
            "the room to stand in for family members. Placed in space, the "
            "representatives often feel and say surprising things, and the "
            "facilitator works with what shows up.",
            "London workshops run as evenings and full days, often with "
            "three to six constellations set up in a session. You can come "
            "to bring an issue of your own or just to represent; the "
            "listing usually says which places are left.",
        ),
        "faq": [
            (
                "What happens in a family constellations workshop?",
                "The group sits in a circle. Someone with an issue tells the "
                "facilitator briefly what it is, then picks representatives "
                "for themselves and the people involved and places them in the "
                "room. The representatives report what they sense, and the "
                "facilitator moves people and offers sentences until the "
                "picture settles. Each constellation takes thirty to sixty "
                "minutes.",
            ),
            (
                "Do I have to bring an issue?",
                "No. Many people come as participants only, representing in "
                "other people's constellations, which is how most people learn "
                "what the work is like. Bringing an issue is often priced "
                "higher and booked ahead.",
            ),
            (
                "Is family constellations therapy?",
                "It's used alongside therapy but isn't a regulated "
                "treatment, and the evidence for it is limited. It can bring up "
                "strong feelings, so look for experienced facilitators, and "
                "tell them beforehand if you are in crisis or recently "
                "bereaved.",
            ),
        ],
    },
    "authentic-relating": {
        "label": "authentic relating",
        "chip": "relating",
        "seo_title": "Authentic relating & circling in London",
        "price_q": "How much does an authentic relating event cost in London?",
        "terms": ["authentic relating", "circling", "relating games", "relating room"],
        "deep": ["authentic relating"],
        "tags": False,
        "intro": (
            "Authentic relating is a set of guided conversation practices "
            "for meeting people honestly: saying what is actually true for "
            "you in the moment, and getting curious about the person in "
            "front of you instead of performing. Evenings are a sequence of "
            "games in pairs and small groups, each with a simple structure "
            "and a facilitator holding time.",
            "Circling is its deeper sibling: a group of five to ten turns its "
            "whole attention on one person, or on the group itself, and "
            "reflects back what it notices. London has weekly drop-ins, "
            "beginner evenings and full circling weekends.",
        ),
        "faq": [
            (
                "What happens at an authentic relating evening?",
                "After a short welcome you're paired up for structured games — "
                "one person asks a question, the other answers, then you swap, "
                "or you take turns finishing a sentence like \"right now I "
                "notice…\". The structures make it easy to go past small talk "
                "quickly. Expect plenty of rotation and a sharing circle at the "
                "end.",
            ),
            (
                "What is circling?",
                "A group practice in which everyone gives their attention to "
                "one person's experience in the moment — what they feel, how "
                "they're landing with the group — and says what they notice in "
                "return. It's slower and more intimate than relating games, and "
                "usually led by a trained facilitator.",
            ),
            (
                "Is it therapy?",
                "No. It's a social practice, not treatment, and nobody is asked "
                "to dig into their history. You choose how much to share, and "
                "\"I'd rather not say\" is a welcome answer.",
            ),
        ],
    },
    "contact-improvisation": {
        "label": "contact improvisation",
        "chip": "contact improv",
        "seo_title": "Contact improvisation jams & classes in London",
        "terms": ["contact improv", "contact jam", "ci jam"],
        "deep": ["contact improvisation"],
        "intro": (
            "Contact improvisation is a movement practice built on a shared "
            "point of contact: two people lean, roll, lift and give weight, "
            "following momentum rather than choreography. It's danced barefoot "
            "and usually in silence or to quiet music.",
            "London has weekly jams — open floors where anyone can dance with "
            "anyone — plus classes for people who want the technique first. "
            "Jams often ask for some prior experience; classes never do.",
        ),
        "faq": [
            (
                "What is contact improvisation?",
                "A duet form developed by Steve Paxton in the 1970s in which "
                "dancers share weight through a moving point of contact. Nothing "
                "is set in advance: the dance follows physics — momentum, gravity "
                "and the surface between two bodies.",
            ),
            (
                "Is contact improv a beginner-friendly practice?",
                "Classes are, and are the right place to start: you learn rolling, "
                "giving weight and how to keep each other safe. Open jams assume "
                "you already have those basics, so check the listing before turning "
                "up to one.",
            ),
            (
                "Is contact improvisation intimate?",
                "It involves a lot of physical contact but is not romantic or "
                "sexual, and consent is explicit throughout — you can decline a "
                "dance or end one at any point without explanation.",
            ),
        ],
    },
    "bohm-dialogue": {
        "label": "Bohm dialogue",
        "chip": "Bohm dialogue",
        "seo_title": "Bohm dialogue groups in London",
        "price_q": "How much does a Bohm dialogue cost in London?",
        "terms": ["bohm"],
        "deep": ["bohm dialogue", "bohmian dialogue"],
        # London runs one or two a month: worth a page from the first
        "min_events": 1,
        "intro": (
            "A Bohm dialogue is a group conversation with no agenda, no "
            "leader and nothing to decide. Ten to forty people sit in a "
            "circle and talk, and the practice is to notice your own "
            "assumptions and reactions as they arise — to hold them up for "
            "the group to look at rather than defend them.",
            "It comes from the physicist David Bohm, who saw thought as a "
            "collective process and dialogue as a way of watching it work. "
            "London groups usually meet for two to three hours on a weekday "
            "evening or weekend afternoon, often opening with a short "
            "introduction for newcomers.",
        ),
        "faq": [
            (
                "What happens in a Bohm dialogue?",
                "The group sits in a circle and a facilitator opens the space, "
                "sometimes with a theme or a reading, then steps back. Anyone "
                "can speak; there is no turn-taking and no goal to reach. Long "
                "silences are normal, and the facilitator may pause the room to "
                "reflect on what the conversation itself is doing.",
            ),
            (
                "How is it different from a debate or a discussion group?",
                "A debate tries to win and a discussion tries to reach a "
                "conclusion. A Bohm dialogue does neither: the aim is to "
                "suspend judgement — your own as much as anyone's — and see "
                "the shared assumptions underneath what people say.",
            ),
            (
                "Do I need to have read Bohm first?",
                "No. Introductory sessions explain the principles at the "
                "start, and most groups welcome people who've never heard of "
                "it. If you want to read ahead, Bohm's short book On Dialogue "
                "is the usual starting point.",
            ),
        ],
    },
    "kirtan": {
        "label": "kirtan & chanting",
        "chip": "kirtan",
        "seo_title": "Kirtan & chanting circles in London",
        "price_q": "How much does kirtan cost in London?",
        "terms": ["kirtan", "mantra circle", "mantra singing", "chanting", "bhajan"],
        "deep": [],
        "tags": False,
        "intro": (
            "Kirtan is call-and-response chanting from the Indian devotional "
            "tradition: a leader sings a line of a mantra, usually with "
            "harmonium and drum, and the room sings it back, slowly at first "
            "and then faster until it lifts. Nobody needs to sing well, and "
            "the words are simple and repeated.",
            "London's chanting circles also cover Vedic chanting, mantra "
            "circles and Celtic and devotional song. Most are an evening of "
            "ninety minutes to two hours, seated on cushions or chairs.",
        ),
        "faq": [
            (
                "What happens at a kirtan?",
                "You sit in a circle or rows facing the musicians. The leader "
                "introduces a mantra and what it means, sings each line, and "
                "everyone answers. Each chant builds over ten to twenty minutes, "
                "and there's usually silence between them. Song sheets are "
                "often handed out.",
            ),
            (
                "Do I need to be religious or able to sing?",
                "No. The mantras come from Hindu devotion, but London kirtans "
                "welcome people of any faith or none, and the point is joining "
                "in rather than singing well. You can also just listen.",
            ),
        ],
    },
}

MIN_PRACTICE_EVENTS = 5

# Turned away from a noindex site: these fetch to train or to answer,
# not to rank, so a noindex meta means nothing to them.
AI_CRAWLERS = [
    "GPTBot", "OAI-SearchBot", "ChatGPT-User", "ClaudeBot", "Claude-User",
    "anthropic-ai", "PerplexityBot", "Perplexity-User", "Google-Extended",
    "Applebot-Extended", "Bytespider", "CCBot", "meta-externalagent",
    "Amazonbot", "cohere-ai", "Diffbot", "Timpibot", "Omgilibot",
]

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

# Organiser pages (/o/<slug>/) answer the searches made by name. Anyone
# hosting at least MIN_ORGANIZER_EVENTS events — upcoming plus the last
# PAST_WINDOW_DAYS — gets one under the name the source reports. An entry
# here merges the variants sources report for one host, tidies a name that
# arrived as a bio, and may carry intro copy of its own. Aliases match the
# raw organizer_name exactly, ignoring case.
ORGANIZERS: dict[str, dict] = {
    "psyconnect": {
        "label": "PsyConnect",
        "aliases": ["PsyConnect", "PsyConnect London"],
    },
    "unseen": {
        "label": "Unseen",
        "aliases": ["Unseen", "Unseen London"],
    },
    "ecstatic-dance-london": {
        "label": "Ecstatic Dance London",
        "aliases": [
            "Ecstatic Dance London",
            "Ecstatic Dance London & URUBU Wellbeing Events",
        ],
    },
    "roy-graff": {
        "label": "Roy Graff",
        "aliases": ["Roy Graff", "Roy Graff, Envokari"],
    },
    "kat-pugowska": {
        "label": "Kat Pugowska",
        "aliases": [
            "Kat Pugowska Acreditated 5Rhythms Teacher, Heartbeat level; "
            "Arts Therapists and Psychologist",
        ],
    },
    "andy-rohkraehmer": {
        "label": "Andy Rohkraehmer",
        "aliases": ["Andy Rohkraehmer, Jungian Psychology practicioner"],
    },
    "baphomets-book-club": {
        "label": "Baphomet's Book Club",
        "aliases": ["Baphomet's Book Club", "BaphometsBookClub"],
    },
    "the-offline-club-london": {
        "label": "The Offline Club",
        "aliases": ["The Offline Club | London"],
    },
    "younited-breath-space": {
        "label": "YOUnited Breath Space",
        "aliases": ["YOUnited_Breath_Space"],
    },
    "inner-rainbow-rise": {
        "label": "Inner Rainbow Rise",
        "aliases": ["Inner_Rainbow_Rise"],
    },
    "majik-moments": {"label": "Majik Moments", "aliases": ["majikmoments"]},
    "the-healing-journeys": {
        "label": "The Healing Journeys",
        "aliases": ["thehealingjourneys"],
    },
    "undercurrent": {"label": "Undercurrent", "aliases": ["undercurrent"]},
    "city-daze": {"label": "City Daze", "aliases": ["City Daze Calendar"]},
}
# Names that aren't a host: Luma calls every unnamed calendar "Personal",
# so unrelated events share it.
ORGANIZER_SKIP = {"personal"}
MIN_ORGANIZER_EVENTS = 3
PAST_WINDOW_DAYS = 365  # a host quiet for longer than this loses its page
PAST_SHOWN = 12  # "recently hosted" lines on an organiser page
_ORGANIZER_ALIASES = {
    alias.lower(): slug_
    for slug_, spec in ORGANIZERS.items()
    for alias in spec["aliases"]
}

# /about/ page copy, per site id. A site with no entry here gets no about
# page — londo doesn't have one yet. Deliberately impersonal: no name, no
# contact address, just what the site is and how an event ends up on it.
ABOUT_CONTENT: dict[str, list[str]] = {
    "psyconnect": [
        "PsyConnect collects in-person psychedelic, consciousness, ceremony "
        "and connection gatherings happening across London, in one place.",
        "Most listings arrive automatically: the site checks around a "
        "dozen ticketing platforms and calendars several times a day and "
        "keeps whatever matches this scene. A short list of organisers "
        "we trust — The Psychedelic Society, Numinity and a few others — "
        "is always included, whatever they're hosting.",
        "Nothing here is sponsored. If an event looks wrong or missing, "
        "the listing came from the organiser's own page — start there.",
    ],
}

# /privacy/ page copy, per site id. Same opt-in pattern as ABOUT_CONTENT —
# a site with no entry gets no privacy page.
PRIVACY_CONTENT: dict[str, list[str]] = {
    "psyconnect": [
        "PsyConnect has no user accounts, no sign-up, and no cookies. "
        "It doesn't collect or store any personal data about visitors.",
        "Page views and outbound clicks (e.g. to a ticket link) are "
        "counted with GoatCounter, a cookieless analytics tool that "
        "doesn't track individuals or build profiles across sites.",
        "Event listings are read from a public database built from "
        "ticketing platforms and organisers' own calendars — nothing "
        "you do on this site is written back to it.",
        "Clicking through to buy a ticket takes you to the organiser's "
        "own site or ticketing platform, which has its own privacy "
        "policy; this site has no visibility into what happens there.",
        "One exception: the community join page runs a Cloudflare Turnstile "
        "check, which sends your IP address to Cloudflare to confirm you're "
        "not a bot. Nothing about that check is stored here.",
    ],
}


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


def _excluded(event: dict, flt: dict) -> bool:
    """Mirror of isExcluded() in web/app.js — keep the two in step.

    Title/organizer/tags only, matched at the start of a word. Descriptions
    are long prose, so a bare substring over them deletes on-brand events by
    accident ("founder of Om Being" in a cacao ceremony blurb was hitting the
    "founder" term meant for startup nights). Left boundary only: plural forms
    ("founders meet up") still match, "remedy" no longer trips "emed".
    """
    terms = flt.get("exclude") or []
    text_terms = flt.get("excludeText") or []
    if not terms and not text_terms:
        return False
    hay = " ".join(
        p
        for p in (
            event.get("title") or "",
            event.get("organizer_name") or "",
            " ".join(event.get("tags") or []),
        )
        if p
    ).lower()
    if any(
        re.search(r"(^|[^a-z0-9])" + re.escape(term), hay) for term in terms
    ):
        return True
    # excludeText: same matching, but the description counts too. Only for
    # terms distinctive enough that a mention in the blurb really does mean
    # the event is off-brand (e.g. "shibari").
    deep_hay = (hay + " " + (event.get("description") or "")).lower()
    return any(
        re.search(r"(^|[^a-z0-9])" + re.escape(term), deep_hay)
        for term in text_terms
    )


def _matches(hay: str, terms: list[str]) -> bool:
    """Word-start matching, as _excluded() does it."""
    return any(
        re.search(r"(^|[^a-z0-9])" + re.escape(term), hay) for term in terms
    )


def practice_match(event: dict, spec: dict, slug: str | None = None) -> bool:
    """Whether this listing belongs on a practice page.

    Same two tiers as the site filter's exclude terms: the safe fields
    (title, organizer, tags) take any of the practice's terms, while the
    description — long prose, where a bare substring catches passing
    mentions — only takes the phrases listed as deep.

    A parent takes its children too: 5Rhythms is a kind of ecstatic
    dance, so the dance page lists those nights and the 5Rhythms page
    stays the narrower door.
    """
    if slug and _term_match(event, spec):
        return True
    if slug:
        return any(
            child.get("parent") == slug and _term_match(event, child)
            for child in PRACTICES.values()
        )
    return _term_match(event, spec)


def _term_match(event: dict, spec: dict) -> bool:
    tags = " ".join(event.get("tags") or []) if spec.get("tags", True) else ""
    hay = " ".join(
        p
        for p in (
            event.get("title") or "",
            event.get("organizer_name") or "",
            tags,
        )
        if p
    ).lower()
    if _matches(hay, spec.get("exclude") or []):
        return False
    if _matches(hay, spec["terms"]):
        return True
    deep = spec.get("deep") or []
    return bool(deep) and _matches((event.get("description") or "").lower(), deep)


# How far back past listings keep a practice (or area) page standing: a
# page that has ranked shouldn't vanish in a quiet fortnight and 404.
PRACTICE_PAST_DAYS = 90

# slug -> that practice's listings from the last PRACTICE_PAST_DAYS,
# filled in by site_practices()
PRACTICE_PAST: dict[str, list[dict]] = {}


def site_practices(
    events: list[dict], past: list[dict] = ()
) -> list[tuple[str, dict, list[dict]]]:
    """Each practice this site has enough listings for, with its events.

    Recent past listings count towards the threshold, so a page stays up
    through a quiet spell rather than flickering in and out of existence.
    """
    since = datetime.now(timezone.utc) - timedelta(days=PRACTICE_PAST_DAYS)
    recent = [e for e in past if _parse_ts(e["start_at"]) >= since]
    out = []
    PRACTICE_PAST.clear()
    for slug_, spec in PRACTICES.items():
        matched = [e for e in events if practice_match(e, spec, slug_)]
        was = [e for e in recent if practice_match(e, spec, slug_)]
        if len(matched) + len(was) >= spec.get("min_events", MIN_PRACTICE_EVENTS):
            out.append((slug_, spec, matched))
            PRACTICE_PAST[slug_] = was
    return out


def _curated(event: dict) -> bool:
    """Mirror of isCurated() in web/app.js — keep the two in step.

    Trusted third-party organisers/series (SITE.curated) pass the
    topic/category filter on their name alone, minus curated.exclude title
    matches (a trusted organiser's off-brand series, e.g. a running club).
    """
    cur = SITE_JSON.get("curated") or {}
    title = (event.get("title") or "").lower()
    if any(t.lower() in title for t in cur.get("exclude") or []):
        return False
    org = (event.get("organizer_name") or "").lower()
    if any(org == o.lower() for o in cur.get("organizers") or []):
        return True
    return any(t.lower() in title for t in cur.get("titleMatches") or [])


def site_match(event: dict) -> bool:
    """Mirror of siteMatch() in web/app.js — keep the two in step."""
    org = (event.get("organizer_name") or "").lower()
    featured = SITE_JSON.get("featured") or {}
    if any(org == o.lower() for o in featured.get("organizers") or []):
        return True
    flt = SITE_JSON.get("filter")
    if not flt:
        return True
    # exclude terms outrank curation, same as the SPA
    if _excluded(event, flt):
        return False
    if _curated(event):
        return True

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


def robots_meta() -> str:
    """On a site marked noindex, every page says so in its head.

    The meta tag, not a robots.txt Disallow, is what actually removes a
    page from an index: a disallowed page can't be fetched, so the
    directive is never read and the URL can linger as a bare link.
    robots.txt keeps the well-behaved AI crawlers off instead.
    """
    if not SITE.get("noindex"):
        return ""
    return '<meta name="robots" content="noindex, nofollow">'


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
    return _rest_get(supabase_url, anon_key, query)


def fetch_past_events(days: int = PAST_WINDOW_DAYS) -> list[dict]:
    """Events from the last `days` days, newest first — for organiser
    pages' history only. A past event stops being re-scraped once it's
    over, so staleness is judged as the upcoming list judges it, at the
    time it started: one gone from its source more than three days before
    was likely withdrawn. Paged, since a year of the whole city is more
    than one response."""
    supabase_url, anon_key = read_config()
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    page_size = 1000
    while True:
        query = urllib.parse.urlencode(
            [
                ("select", "*"),
                # unique tiebreak, or offset paging can skip/repeat rows
                ("order", "start_at.desc,source.asc,source_id.asc"),
                ("limit", str(page_size)),
                ("offset", str(len(rows))),
                ("start_at", f"gte.{(now - timedelta(days=days)).isoformat()}"),
                ("start_at", f"lt.{now.isoformat()}"),
                ("duplicate_of", "is.null"),
                ("is_online", "eq.false"),
                ("hidden", "is.false"),
            ]
        )
        batch = _rest_get(supabase_url, anon_key, query)
        rows.extend(batch)
        if len(batch) < page_size:
            break
    return [
        e for e in rows
        if _parse_ts(e["last_seen_at"]) >= _parse_ts(e["start_at"]) - timedelta(days=3)
    ]


def fetch_gone_events() -> list[dict]:
    """Listings that no longer get a page of their own — merged into
    another listing as a duplicate, or dropped by their source before
    the date — so the URLs Google already found can point somewhere."""
    supabase_url, anon_key = read_config()
    now = datetime.now(timezone.utc)
    stale = (now - timedelta(days=3)).isoformat()
    since = (now - timedelta(days=PAST_WINDOW_DAYS)).isoformat()
    out: list[dict] = []
    for filters in (
        [("duplicate_of", "not.is.null"), ("start_at", f"gte.{since}")],
        [
            ("duplicate_of", "is.null"),
            ("start_at", f"gte.{since}"),
            ("last_seen_at", f"lt.{stale}"),
        ],
    ):
        rows: list[dict] = []
        while True:
            query = urllib.parse.urlencode(
                [
                    ("select", "source,source_id,title,start_at,duplicate_of,"
                     "organizer_name,topics,category,tags,description,"
                     "is_online,hidden,last_seen_at"),
                    ("order", "start_at.desc,source.asc,source_id.asc"),
                    ("limit", "1000"),
                    ("offset", str(len(rows))),
                    ("hidden", "is.false"),
                    *filters,
                ]
            )
            batch = _rest_get(supabase_url, anon_key, query)
            rows.extend(batch)
            if len(batch) < 1000:
                break
        out.extend(rows)
    # a past listing isn't re-scraped once over, so it was withdrawn only
    # if it vanished from its source well before the date
    return [
        e for e in out
        if e.get("duplicate_of")
        or _parse_ts(e["last_seen_at"]) < _parse_ts(e["start_at"]) - timedelta(days=3)
    ]


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rest_get(supabase_url: str, anon_key: str, query: str) -> list[dict]:
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


def organizer_slug(name: str | None) -> str:
    """Mirror of organizerSlug() in web/app.js — keep the two in step, so
    GoatCounter groups an organiser's clicks together regardless of whether
    they came from the SPA or a static page."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "unknown").lower()).strip("-")
    return slug or "unknown"


def organizer_key(event: dict) -> tuple[str, str] | None:
    """(slug, display name) of the host an organiser page is keyed on —
    ORGANIZERS merges and renames first, else the name as reported."""
    name = (event.get("organizer_name") or "").strip()
    if not name or name.lower() in ORGANIZER_SKIP:
        return None
    slug_ = _ORGANIZER_ALIASES.get(name.lower())
    if slug_:
        return slug_, ORGANIZERS[slug_]["label"]
    return organizer_slug(name), name


def legacy_event_id(event: dict) -> str:
    """Previous /e/<source>-<id>/ path — kept as a redirect target only."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", event["source_id"])[:48]
    return f"{event['source']}-{sid}"


def assign_event_slugs(
    events: list[dict], past: list[dict] = ()
) -> dict[tuple[str, str], str]:
    """Prefer bare title slug; on collision append date, then a short unique tail.

    Listings without a ticket page (chat-shared flyers) pick first: the
    SPA links them to their own page by bare title slug (eventHref() in
    web/app.js), so that slug has to be theirs.

    Past events (newest first) are slugged after every upcoming one, so
    they never take a URL from something still on: a series' bare slug
    rolls forward to its next date, and a finished one keeps the URL it
    was found at."""
    used: set[str] = set()
    mapping: dict[tuple[str, str], str] = {}
    upcoming = sorted(events, key=lambda e: bool(e.get("source_url")))
    for event in [*upcoming, *past]:
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


def practice_url(slug_: str) -> str:
    return f"{BASE_URL}/p/{slug_}/"


def organizer_page_url(slug_: str) -> str:
    return f"{BASE_URL}/o/{slug_}/"


def organizers_index_url() -> str:
    return f"{BASE_URL}/o/"


def organizer_page_for(event: dict) -> tuple[str, str] | None:
    """(slug, display name) when this event's host has a page here."""
    key = organizer_key(event)
    return key if key and key[0] in SITE_ORGANIZERS else None


def esc(value) -> str:
    return html.escape(str(value or ""), quote=True)


def thumb(url: str, width: int) -> str:
    """Mirror of thumb() in web/app.js — keep the two in step.

    Organisers upload art at whatever size they like (routinely 300KB-1.7MB);
    wsrv.nl re-encodes to webp at the size we actually render. og:image keeps
    the original — social scrapers are fussier than browsers.
    """
    if not url:
        return url
    src, crop = full_res(url, width)
    return (
        "https://wsrv.nl/?url="
        + urllib.parse.quote(src, safe="")
        + f"&w={width}{crop}&output=webp&q=75"
    )


def full_res(url: str, width: int) -> tuple[str, str]:
    """Mirror of fullRes() in web/app.js — keep the two in step.

    Some hosts hand us a small resize of the art rather than the upload
    itself. Eventbrite's og:image is a 460x230 imgix box (a portrait flyer
    clips to 129x230) and Meetup's is 600px wide, so the proxy was upscaling
    them into a blur. Point at the original and have wsrv redo the crop.
    Returns (source url, extra wsrv params).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == "secure.meetupstatic.com":
        return re.sub(r"/\d+_(\d+\.\w+)$", r"/highres_\1", url), ""
    q = dict(urllib.parse.parse_qsl(parts.query))
    if parts.hostname != "img.evbuc.com" or "w" not in q or "h" not in q:
        return url, ""
    # the path is the percent-encoded origin URL on cdn.evbuc.com
    src = urllib.parse.unquote(parts.path[1:])
    rect = q.get("rect", "").split(",")
    # the organiser's own crop box; imgix then fits it inside w x h.
    # precrop, or wsrv resizes first and crops the shrunk image
    if len(rect) == 4 and all(n.isdigit() for n in rect):
        x, y, cw, ch = rect
        return src, f"&cx={x}&cy={y}&cw={cw}&ch={ch}&precrop"
    if q.get("fit") == "crop":
        h = int(width * int(q["h"]) / int(q["w"]) + 0.5)  # JS Math.round, not banker's
        crop = f"&h={h}&fit=cover"
        if "fp-x" in q and "fp-y" in q:
            crop += f"&a=focal&fpx={q['fp-x']}&fpy={q['fp-y']}"
        return src, crop
    # plain w/h is imgix's fit=clip: the whole image, natural aspect
    return src, ""


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


def fmt_time_range(event: dict) -> str:
    """Card time line, as the SPA's formatTime: "19:00 – 21:30"."""
    if event.get("is_all_day"):
        return "All day"
    start = _start_london(event).strftime("%H:%M")
    if not event.get("end_at"):
        return start
    end = datetime.fromisoformat(event["end_at"].replace("Z", "+00:00"))
    return f"{start} – {end.astimezone(LONDON).strftime('%H:%M')}"


def time_of_day_class(event: dict) -> str:
    hour = _start_london(event).hour
    if hour < 12:
        return "dot-morning"
    if hour < 17:
        return "dot-afternoon"
    return "dot-evening"


# Mirrors CATEGORIES colours / GRADIENTS / hash() in web/app.js so a
# card without an image gets the same placeholder here as on the SPA.
CATEGORY_COLOURS = {
    "move": ("#e8836f", "#d96a9e"),
    "connect": ("#e3c08d", "#e8836f"),
    "expand": ("#9d7fd1", "#6f5bb5"),
    "think": ("#5fb5a2", "#3d8fa8"),
    "make": ("#d96a9e", "#9d7fd1"),
}
GRADIENTS = [
    ("#4f46e5", "#9333ea"), ("#0891b2", "#2563eb"), ("#059669", "#0d9488"),
    ("#d97706", "#dc2626"), ("#db2777", "#9333ea"), ("#475569", "#1e293b"),
]
PICK_THRESHOLD = 75  # quality_score at or above ⇒ "✦ pick"
HORIZON_DAYS = 30  # the date strip's window, as on the main page
# Listing pages come in the main page's two ranges. A month of a busy
# topic is an enormous page, so the week is what a listing leads with
# and the month sits one click away, pointing its canonical back.
LISTING_WINDOWS = (7, 30)
MIN_WEEK_EVENTS = 3  # below this the week is too thin to lead with
# Topics and categories are for "what's on this week"; a practice page
# answers "where do I find one of these", which a month serves better.
LEAD_WINDOW = {"topic": 7, "category": 7, "practice": 30}
# A month page with fewer listings than this also shows what's further
# ahead (up to AHEAD_DAYS out) and, on a topic, what ran recently: a
# practice that meets every six weeks shouldn't look like it's gone.
SPARSE_BELOW = 15
AHEAD_DAYS = 90
AHEAD_SHOWN = 24
# below this many in the month, a practice page points at its neighbours
THIN_BELOW = 8
# practices that draw the same people, nearest first
RELATED_PRACTICES = {
    "ecstatic-dance": ["5rhythms", "contact-improvisation", "cacao-ceremony"],
    "5rhythms": ["ecstatic-dance", "contact-improvisation", "somatics"],
    "sound-bath": ["breathwork", "kirtan", "cacao-ceremony"],
    "cacao-ceremony": ["ecstatic-dance", "sound-bath", "breathwork"],
    "breathwork": ["somatics", "sound-bath", "qigong"],
    "somatics": ["breathwork", "qigong", "contact-improvisation"],
    "qigong": ["somatics", "breathwork", "sound-bath"],
    "tantra": ["authentic-relating", "somatics", "breathwork"],
    "family-constellations": ["authentic-relating", "breathwork", "somatics"],
    "authentic-relating": ["bohm-dialogue", "tantra", "family-constellations"],
    "contact-improvisation": ["5rhythms", "ecstatic-dance", "somatics"],
    "bohm-dialogue": ["authentic-relating", "family-constellations"],
    "kirtan": ["sound-bath", "cacao-ceremony", "ecstatic-dance"],
}


def placeholder_gradient(event: dict) -> str:
    cat = event.get("category")
    if cat in CATEGORY_COLOURS:
        c1, c2 = CATEGORY_COLOURS[cat]
    else:
        h = 0
        for ch in event.get("title") or "?":
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        c1, c2 = GRADIENTS[h % len(GRADIENTS)]
    return f"linear-gradient(135deg, {c1}, {c2})"


def event_card(event: dict, dated: bool = False) -> str:
    """The SPA's card (web/app.js card()) as static markup, linking to
    the event's own page here rather than out to the ticket page.
    `dated`: the card isn't under a day heading, so its time line carries
    the day too ("Fri 23 Oct · 20:00 – 23:00")."""
    title = esc(event.get("title") or "")
    if event.get("image_url"):
        art = event["image_url"]
        fallback = esc(art.replace("'", "%27"))
        banner = (
            f'<img alt="" loading="lazy" src="{esc(thumb(art, 600))}" '
            f"onerror=\"this.onerror=null;this.src='{fallback}'\">"
        )
        banner_style = ""
    else:
        banner = f'<span class="placeholder-initial">{title}</span>'
        banner_style = f' style="background:{placeholder_gradient(event)}"'
    cat = event.get("category")
    if cat in CATEGORIES and not SITE_JSON.get("filter"):
        banner += f'<span class="badge badge-cat badge-cat-{cat}">{cat}</span>'
    if (event.get("quality_score") or 0) >= PICK_THRESHOLD:
        banner += (
            '<span class="pick-mark" title="one of the richer listings this week">'
            "✦ pick</span>"
        )

    body = [
        f'<p class="time"><span class="dot {time_of_day_class(event)}"></span>'
        + (esc(_start_london(event).strftime("%a %-d %b")) + " · " if dated else "")
        + f"{esc(fmt_time_range(event))}</p>",
        f"<h3>{title}</h3>",
    ]
    if event.get("hook"):
        body.append(f'<p class="hook">{esc(event["hook"])}</p>')
    place = event.get("venue_name") or event.get("address")
    if place:
        body.append(f'<p class="venue">{esc(place)}</p>')
    meta = []
    if event.get("is_free"):
        meta.append('<span class="free-tag">free</span>')
    elif fmt_price(event):
        meta.append(esc(fmt_price(event)))
    for key in ("organizer_name", "area"):
        if event.get(key):
            meta.append(esc(event[key]))
    if meta:
        body.append(f'<p class="meta">{" · ".join(meta)}</p>')
    if not event.get("hook") and event.get("description"):
        blurb = strip_meeting_links(" ".join(event["description"].split()))[:220]
        body.append(f'<p class="blurb">{esc(blurb)}</p>')

    return (
        f'<a class="card instant" href="{event_url(event)}">'
        f'<div class="banner"{banner_style}>{banner}</div>'
        f'<div class="card-body">{"".join(body)}</div></a>'
    )


def within_window(events: list[dict], days: int) -> list[dict]:
    """The events starting inside the next `days` days, London time."""
    last = datetime.now(LONDON).date() + timedelta(days=days - 1)
    return [e for e in events if _start_london(e).date() <= last]


def beyond_window(events: list[dict], days: int, until: int = AHEAD_DAYS) -> list[dict]:
    """The events after the next `days` days, up to `until` days out."""
    today = datetime.now(LONDON).date()
    first, last = today + timedelta(days=days), today + timedelta(days=until - 1)
    return [e for e in events if first <= _start_london(e).date() <= last]


def _tick_cell(i: int, day) -> str:
    # the date is the sub-line throughout: over a week the weekday
    # alone under "today"/"tmrw" left people guessing the date
    if i == 0:
        main = "today"
    elif i == 1:
        main = "tmrw"
    else:
        main = day.strftime("%a").lower()
    return f"{main}<small>{day.strftime('%-d')}</small>"


def _strip(ticks: list[str], ranges: list[str]) -> str:
    return (
        '<div class="static-ticker"><div class="ticker-shell">'
        f'<div class="ticker">{"".join(ticks)}</div>'
        f'<div class="ticker-fixed">{"".join(ranges)}</div>'
        "</div></div>"
    )


def date_strip_html(events: list[dict], days: int, other_url: str | None) -> str:
    """The main page's date strip, as anchors onto this page's day
    groups. A day this topic has nothing on is shown spent rather than
    hidden, so the shape of the month stays readable. `events` is the
    whole month: the strip always runs the full horizon, and on the
    7-day page a day past the week jumps into the 30-day page instead
    of leaving the rest of the bar empty."""
    have = {_start_london(e).date() for e in events}
    today = datetime.now(LONDON).date()
    ticks = []
    for i in range(max(LISTING_WINDOWS)):
        day = today + timedelta(days=i)
        cell = _tick_cell(i, day)
        if day in have and i < days:
            ticks.append(f'<a class="tick" href="#d-{day.isoformat()}">{cell}</a>')
        elif day in have and other_url:
            ticks.append(
                f'<a class="tick" href="{other_url}#d-{day.isoformat()}">{cell}</a>'
            )
        else:
            ticks.append(f'<span class="tick spent" aria-hidden="true">{cell}</span>')
    # the main page's pinned ranges: here they're the two pages
    ranges = []
    for n in LISTING_WINDOWS:
        cell = f"{n}<small>days</small>"
        if n == days:
            ranges.append(f'<span class="tick cursor" aria-current="page">{cell}</span>')
        elif other_url:
            ranges.append(f'<a class="tick" href="{other_url}">{cell}</a>')
    return _strip(ticks, ranges)


def home_date_strip_html(event: dict | None = None) -> str:
    """The date strip on a page with no day groups of its own: each tick
    opens the main page on that day, and an event page's own day is lit
    so the strip says where in the month it falls."""
    have = {_start_london(e).date() for e in SITE_EVENTS}
    own = _start_london(event).date() if event else None
    today = datetime.now(LONDON).date()
    ticks = []
    for i in range(max(LISTING_WINDOWS)):
        day = today + timedelta(days=i)
        cell = _tick_cell(i, day)
        href = f"{BASE_URL}/?day={day.isoformat()}"
        if day == own:
            ticks.append(
                f'<a class="tick cursor" href="{href}" aria-current="date">{cell}</a>'
            )
        elif day in have:
            ticks.append(f'<a class="tick" href="{href}">{cell}</a>')
        else:
            ticks.append(f'<span class="tick spent" aria-hidden="true">{cell}</span>')
    ranges = [
        f'<a class="tick" href="{BASE_URL}/?day={n}">{n}<small>days</small></a>'
        for n in LISTING_WINDOWS
    ]
    return _strip(ticks, ranges)


# the card grid's width on a wide screen: a group shorter than this
# leaves a row with holes in it
GROUP_MIN = 3


def _range_label(first, last) -> str:
    if first == last:
        return first.strftime("%A %-d %B")
    if first.month == last.month:
        return f"{first:%-d} – {last:%-d %B}"
    return f"{first:%-d %b} – {last:%-d %b}"


def week_groups(events: list[dict]) -> str:
    """Events as card grids under a few headings, each card carrying its
    own day. A listing rarely has two things on one day, or even three in
    a week, so a heading per day — or per week — left rows one card wide.
    Weeks are merged, in order, until a group's cards fill whole rows;
    only the page's last row can be short, and a short last group joins
    the one before it. A lone week keeps its name ("This
    week", "Next week"); merged weeks are named by their dates. The first
    card of each day is that day's anchor (#d-YYYY-MM-DD) for the date
    strip above."""
    today = datetime.now(LONDON).date()
    this_monday = today - timedelta(days=today.weekday())
    weeks: dict = {}
    for e in events:
        day = _start_london(e).date()
        weeks.setdefault(day - timedelta(days=day.weekday()), []).append(e)

    groups: list[list] = []  # [[monday, ...], events]
    for monday, week_events in weeks.items():
        # keep filling the open group until its rows are whole
        if groups and len(groups[-1][1]) % GROUP_MIN:
            groups[-1][0].append(monday)
            groups[-1][1].extend(week_events)
        else:
            groups.append([[monday], list(week_events)])
    if len(groups) > 1 and len(groups[-1][1]) < GROUP_MIN:
        mondays, tail = groups.pop()
        groups[-1][0].extend(mondays)
        groups[-1][1].extend(tail)

    sections = []
    anchored: set = set()
    for mondays, group_events in groups:
        if len(mondays) == 1 and mondays[0] <= this_monday:
            heading = "This week"
        elif len(mondays) == 1 and mondays[0] == this_monday + timedelta(days=7):
            heading = "Next week"
        else:
            heading = _range_label(
                _start_london(group_events[0]).date(),
                _start_london(group_events[-1]).date(),
            )
        cards = []
        for e in group_events:
            day = _start_london(e).date().isoformat()
            card = event_card(e, dated=True)
            if day not in anchored:
                anchored.add(day)
                card = card.replace('<a class="card', f'<a id="d-{day}" class="card', 1)
            cards.append(card)
        sections.append(
            f'<section class="day-group"><h2 class="day-heading">'
            f"<span>{esc(heading)}</span></h2>"
            f'<div class="grid">{"".join(cards)}</div></section>'
        )
    return "".join(sections)


def write_index(path: Path, html_text: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "index.html").write_text(html_text)


def moved_page(canonical: str) -> str:
    return (
        f'<!doctype html><html lang="en"><head>'
        f'<meta charset="utf-8">'
        f'<link rel="canonical" href="{esc(canonical)}">'
        f'<meta http-equiv="refresh" content="0;url={esc(canonical)}">'
        f"</head><body>"
        f'<p><a href="{esc(canonical)}">This page has moved</a>.</p>'
        f"</body></html>"
    )


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
    head_extra: str = "",
    body_class: str = "static-page",
    map_href: str | None = None,
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
  {theme_meta()}{theme_boot_tag()}
  <title>{esc(title)}</title>
  <meta name="description" content="{esc(description)}">
  <link rel="canonical" href="{esc(canonical)}">
  {robots_meta()}{head_extra}
  <meta property="og:site_name" content="{esc(display_name())}">
  <meta property="og:type" content="website">
  <meta property="og:title" content="{esc(title)}">
  <meta property="og:description" content="{esc(description)}">
  <meta property="og:url" content="{esc(canonical)}">
  {image}
  <link rel="icon" type="image/png" href="{css_prefix}/icons/favicon.png">
  <link rel="stylesheet" href="{css_prefix}/fonts/fonts.css">
  <link rel="stylesheet" href="{css_prefix}/styles.css">
  {extra_css(css_prefix)}
  {ld}
  {goatcounter_snippet()}
</head>
<body class="{body_class}">
  <div class="sky" aria-hidden="true"><div class="blob blob-a"></div><div class="blob blob-b"></div><div class="grain"></div></div>
  {static_topbar(css_prefix, map_href)}
  <main class="static-main">
  {body}
  </main>
  <footer class="static-footer">
    {seo_nav_html()}{channel_link_html()}
    <p class="static-footer-home"><a href="{BASE_URL}/">{esc(display_name())}</a> — {esc(SITE["tagline"])}{about_link_html()}{privacy_link_html()}</p>
    <p class="static-footer-home credit">Made by your friendly <a href="https://www.digitalhandyman.london" rel="noopener">digital handyman</a></p>
  </footer>
</body>
</html>
"""


def theme_meta() -> str:
    """theme-color for the browser chrome; theme-boot.js keeps it current."""
    if not (THEME_BOOT and THEME_COLOR):
        return ""
    return f'<meta name="theme-color" content="{esc(THEME_COLOR)}">\n  '


def theme_toggle_html() -> str:
    """Light/dark switch. Sites without a theme-boot.js don't get one.

    Keep this markup in step with the copy in the site's shell index.html —
    theme-boot.js binds whatever it finds under the id, and the icon-swap
    CSS keys off the two classes."""
    if not THEME_BOOT:
        return ""
    return (
        '<button class="theme-toggle" id="theme-toggle" type="button" '
        'aria-label="Switch to dark theme" aria-pressed="false">'
        '<svg class="theme-moon" viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'
        '<svg class="theme-sun" viewBox="0 0 24 24" aria-hidden="true">'
        '<circle cx="12" cy="12" r="4.2"/>'
        '<path d="M12 2.6v2.2M12 19.2v2.2M4.6 4.6l1.6 1.6M17.8 17.8l1.6 1.6'
        'M2.6 12h2.2M19.2 12h2.2M4.6 19.4l1.6-1.6M17.8 6.2l1.6-1.6"/></svg>'
        "</button>"
    )


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
    for slug_, spec, _ in SITE_PRACTICES:
        parts.append('<span class="seo-sep" aria-hidden="true">·</span>')
        parts.append(f'<a href="{practice_url(slug_)}">{esc(spec["label"])}</a>')
    if SITE_ORGANIZERS:
        parts.append('<span class="seo-sep" aria-hidden="true">·</span>')
        parts.append(f'<a href="{organizers_index_url()}">hosts</a>')
    if SITE_VENUES:
        parts.append('<span class="seo-sep" aria-hidden="true">·</span>')
        parts.append(f'<a href="{venues_index_url()}">venues</a>')
    if _when_pages_on():
        for which in WHEN_PAGES:
            parts.append('<span class="seo-sep" aria-hidden="true">·</span>')
            parts.append(f'<a href="{BASE_URL}/{which}/">{which.replace("-", " ")}</a>')
    return f'<nav class="seo-nav" aria-label="topics">{"".join(parts)}</nav>'


# The site's upcoming events, filled in by build() before event pages
# are written: their date strip shows which days have something on.
SITE_EVENTS: list[dict] = []

# Upcoming events per topic, filled in by build() before listing pages
# are written, so their topic chips can carry the same counts the SPA's do.
TOPIC_COUNTS: dict[str, int] = {}

# The practices this site has the listings for, filled in by build()
# before any page is written: the footer links to them from everywhere.
SITE_PRACTICES: list[tuple[str, dict, list[dict]]] = []

# Hosts with an organiser page, filled in by build() before any page is
# written: event pages link their host here. slug -> {label, url,
# upcoming, past}.
SITE_ORGANIZERS: dict[str, dict] = {}


def site_organizers(upcoming: list[dict], past: list[dict]) -> dict[str, dict]:
    """Hosts with enough on, or recently on, to be worth a page — past
    events count, so a host between seasons keeps its page."""
    found: dict[str, dict] = {}
    for when, evs in (("upcoming", upcoming), ("past", past)):
        for e in evs:
            key = organizer_key(e)
            if not key:
                continue
            slug_, label = key
            org = found.setdefault(
                slug_, {"label": label, "url": None, "upcoming": [], "past": []}
            )
            org[when].append(e)
            org["url"] = org["url"] or e.get("organizer_url")
    return {
        slug_: org
        for slug_, org in found.items()
        if len(org["upcoming"]) + len(org["past"]) >= MIN_ORGANIZER_EVENTS
    }


# ---------- venue pages ----------
# "What's on at Colet House" is a search people make, and the rooms are
# where a scene actually gathers. A venue earns a page the way a host
# does: enough on, or recently on, that the page has something to say.
MIN_VENUE_EVENTS = 4

# Venues with a page, filled in by build() before any page is written:
# event pages link their Where line here. slug -> {label, address, area,
# lat, lon, upcoming, past, aliases}.
SITE_VENUES: dict[str, dict] = {}
# (source, source_id) -> venue slug, for exactly the events on its page
VENUE_EVENTS: dict[tuple[str, str], str] = {}

_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?) ?(\d[A-Z]{2})\b", re.I)
# A venue name that is really a home ("flat 6, 4 Torriano Mews", "51
# Barnsbury Street") never gets a page: someone's front door is not a
# venue to advertise. 21Soho and 4-4-2 Together pass — no space after
# the number.
_PRIVATE_VENUE_RE = re.compile(r"\bflat\b|^\d+[a-z]?\s+[a-z]", re.I)
# "London SE1", "London N3": a postcode district standing in for a venue
_DISTRICT_VENUE_RE = re.compile(r"^london,?\s+[a-z]{1,2}\d", re.I)
# Neighbourhoods some sources put in the venue field. Parks stay in —
# "events on Hampstead Heath" is a real place to meet.
_NEIGHBOURHOODS = {
    "hackney", "hackney wick", "hackney downs", "stoke newington", "dalston",
    "shoreditch", "brixton", "peckham", "camden", "camden town", "islington",
    "clapham", "bethnal green", "walthamstow", "london fields", "soho",
    "kings cross", "king's cross", "bermondsey", "deptford", "lewisham",
    "hammersmith", "notting hill", "kentish town", "tottenham", "hoxton",
    "whitechapel", "stratford", "greenwich", "wimbledon", "croydon",
    "central london", "east london", "north london", "south london",
    "west london", "london bridge", "elephant and castle", "vauxhall",
}


def _venue_outward(event: dict) -> str | None:
    """The postcode district (W14, E8) from the address, if it has a
    whole postcode — the one place spelling every source agrees on."""
    m = _POSTCODE_RE.search(f"{event.get('address') or ''} {event.get('venue_name') or ''}")
    return m.group(1).upper() if m else None


def _venue_page_name(venue: str) -> bool:
    low = venue.lower().strip()
    return not (
        _PRIVATE_VENUE_RE.search(low)
        or _DISTRICT_VENUE_RE.search(low)
        or low in _NEIGHBOURHOODS
        or low in AREAS
    )


def _venue_slug(names) -> str:
    """One slug however the sources spell the room. Counts shift build to
    build, so this can't follow the commonest spelling ("21 Soho" one week,
    "21Soho" the next): the spelling with the most word breaks wins, then
    the alphabet — the same answer every time those names are seen."""
    slugs = {
        # St Luke's -> st-lukes, not st-luke-s
        slugify_title(re.sub(r"^the\s+|['’]", "", n.strip(), flags=re.I))
        for n in names
    }
    return sorted(slugs, key=lambda s: (-s.count("-"), s))[0]


def site_venues(upcoming: list[dict], past: list[dict]) -> dict[str, dict]:
    """Rooms with enough on, or recently on, to be worth a page. Spellings
    group by venue_key ("The Skylight Centre" = "Skylight Centre"). A name
    shared by two buildings ("St Luke's Church") keeps only the events in
    its usual postcode district, so one page never mixes two places."""
    groups: dict[str, dict] = {}
    for when, evs in (("upcoming", upcoming), ("past", past)):
        for e in evs:
            venue = clean_venue(e)
            if not venue or not _venue_page_name(venue):
                continue
            g = groups.setdefault(
                venue_key(venue), {"names": {}, "upcoming": [], "past": []}
            )
            g["names"][venue] = g["names"].get(venue, 0) + 1
            g[when].append(e)

    found: dict[str, dict] = {}
    for g in groups.values():
        every = g["upcoming"] + g["past"]
        districts = [d for d in map(_venue_outward, every) if d]
        home = max(set(districts), key=districts.count) if districts else None

        def here(e: dict) -> bool:
            d = _venue_outward(e)
            return home is None or d is None or d == home

        upcoming_ = [e for e in g["upcoming"] if here(e)]
        past_ = [e for e in g["past"] if here(e)]
        kept = upcoming_ + past_
        if len(kept) < MIN_VENUE_EVENTS:
            continue
        names = g["names"]
        slug_ = _venue_slug(names)
        label = max(sorted(names), key=names.get)  # commonest spelling, for show
        # an address with a whole postcode beats one without
        # arrival notes ride along in some addresses: "(please arrive…)"
        addresses = [
            tidy_address(re.sub(r"\s*\([^)]*\)", "", e["address"]), label)
            for e in kept
            if e.get("address")
        ]
        addresses = [a for a in addresses if a and a.lower() not in ("london", "london, uk")]
        with_pc = [a for a in addresses if _POSTCODE_RE.search(a)]
        pool = with_pc or addresses
        address = max(sorted(set(pool)), key=pool.count) if pool else ""
        areas = [e["area"] for e in kept if e.get("area")]
        geo = [(e["latitude"], e["longitude"]) for e in kept if e.get("latitude") and e.get("longitude")]
        found[slug_] = {
            "label": label,
            "address": address,
            "area": max(sorted(set(areas)), key=areas.count) if areas else None,
            "geo": geo[0] if geo else None,
            "upcoming": upcoming_,
            "past": past_,
            # other spellings' slugs, redirected to this one
            "aliases": {
                s for s in (_venue_slug([n]) for n in names) if s != slug_
            },
        }
    return found


def venue_page_url(slug_: str) -> str:
    return f"{BASE_URL}/v/{slug_}/"


def venues_index_url() -> str:
    return f"{BASE_URL}/v/"


def venue_page_for(event: dict) -> str | None:
    return VENUE_EVENTS.get((event["source"], event["source_id"]))


def filter_rows(
    current: str | None, kind: str, where: list[str] | None = None
) -> list[tuple[str, list[str]]]:
    """The home page's filter rows, as links to the other listings, with
    the one you're on lit. Served in the HTML (not built by script) — they
    are how a crawler gets from any page to the practice and topic pages.
    `where`: chips for the location row, already rendered."""
    rows = []
    site_topics = SITE_JSON.get("topics")
    topics = []
    for key in (k for k in TOPICS if site_topics is None or k in site_topics):
        if not TOPIC_COUNTS.get(key):
            continue
        slug_, _ = TOPICS[key]
        topics.append(
            _nav_chip(esc(key), topic_url(slug_), kind == "topic" and key == current)
        )
    if topics:
        rows.append(("what", topics))

    modalities = []
    for slug_, spec, _ in SITE_PRACTICES:
        if spec.get("parent"):
            continue
        here = kind == "practice" and slug_ == current
        modalities.append(
            _nav_chip(
                esc(spec.get("chip") or spec["label"]), practice_url(slug_), here
            )
        )
        for child_slug, child, _c in SITE_PRACTICES:
            if child.get("parent") != slug_:
                continue
            modalities.append(
                _nav_chip(
                    esc(child.get("chip") or child["label"]),
                    practice_url(child_slug),
                    kind == "practice" and child_slug == current,
                    child=True,
                )
            )
    if modalities:
        rows.append(("modality", modalities))
    if where:
        rows.append(("where", where))

    # the rooms: the venues index, and on a venue's own page, that venue
    # lit beneath it, at the end of the where row
    if SITE_VENUES:
        rooms = [_nav_chip("venues", venues_index_url(), kind == "venues")]
        if kind == "venue" and current in SITE_VENUES:
            rooms.append(
                _nav_chip(
                    esc(SITE_VENUES[current]["label"]),
                    venue_page_url(current),
                    True,
                    child=True,
                )
            )
        if rows and rows[-1][0] == "where":
            rows[-1] = ("where", rows[-1][1] + rooms)
        else:
            rows.append(("where", rooms))

    # who: the hosts index, and on a host's own page, that host lit
    # beneath it — so the key says where you are there too
    if SITE_ORGANIZERS:
        who = [_nav_chip("hosts", organizers_index_url(), kind == "hosts")]
        if kind == "organizer" and current in SITE_ORGANIZERS:
            who.append(
                _nav_chip(
                    esc(SITE_ORGANIZERS[current]["label"]),
                    organizer_page_url(current),
                    True,
                    child=True,
                )
            )
        rows.append(("who", who))

    return rows


PIN_SVG = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 21s-6.5-6-6.5-11a6.5 '
    '6.5 0 0 1 13 0c0 5-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.3"/></svg>'
)


def home_view_url(params: dict | None = None, on_map: bool = False) -> str:
    """The main page opened on a filtered view (its ?modality=/?topic=/
    ?area= state) and optionally on the map."""
    qs = urllib.parse.urlencode(home_view_params(params))
    return f"{BASE_URL}/" + (f"?{qs}" if qs else "") + ("#on-map" if on_map else "")


def map_available() -> bool:
    return (SITE_JSON.get("features") or {}).get("map", True) is not False


def static_topbar(css_prefix: str, map_href: str | None = None) -> str:
    """The main page's header: wordmark and tagline, then the map pin (on
    pages with something to map) and the theme switch. The wordmark sits
    in a <p>, not an <h1> — the page's own title is its one heading."""
    pin = (
        f'<a class="theme-toggle map-toggle" href="{esc(map_href)}" '
        'aria-label="Show these on a map" data-goatcounter-click="static/map">'
        f"{PIN_SVG}</a>"
        if map_href and map_available()
        else ""
    )
    return f"""<header class="topbar static-topbar">
    <div>
      <p class="site-title"><a class="home" href="{BASE_URL}/" aria-label="{esc(display_name())} home">{site_wordmark(css_prefix)}</a></p>
      <p class="tagline">{esc(SITE["tagline"])}</p>
    </div>
    <div class="topbar-actions">{pin}{theme_toggle_html()}</div>
  </header>"""


# opens and shuts the filters panel; the links in it are plain HTML
_FILTERS_SCRIPT = (
    "<script>(function(){var b=document.getElementById('filters-toggle'),"
    "p=document.getElementById('filters-panel'),c=document.getElementById('filters-close');"
    "if(!b||!p)return;function t(o){p.hidden=!o;b.setAttribute('aria-expanded',o);"
    "b.classList.toggle('lit',o)}b.addEventListener('click',function(){t(p.hidden)});"
    "if(c)c.addEventListener('click',function(){t(false)})})();</script>"
)


def static_controls(
    strip: str,
    current: str | None,
    kind: str,
    context: dict | None = None,
    where: list[str] | None = None,
    placeholder: str = "search…",
) -> str:
    """The main page's bench: search, free, filters and the date strip, in
    its markup and ids so both sites' CSS lays it out identically. Search
    and free open the main page, narrowed to `context` (this page's
    practice or topic) when there is one."""
    hidden = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
        for k, v in home_view_params(context).items()
    )
    rows = filter_rows(current, kind, where)
    panel = "".join(
        f'<div class="facet-row"><span class="legend facet-legend">{label}</span>'
        f'<div class="facet-chips">{"".join(chips)}</div></div>'
        for label, chips in rows
    )
    return f"""<div class="controls static-controls" id="controls">
    <form class="controls-row" action="{BASE_URL}/" method="get" role="search">
      <input id="search" name="q" type="search" placeholder="{esc(placeholder)}" autocomplete="off" aria-label="search events">
      <div class="unit unit-inline">
        <label class="switch" title="free events only">
          <input type="checkbox" id="free-toggle" name="free" value="1" aria-label="free events only" onchange="this.form.submit()">
          <span class="slider"></span>
        </label>
        <span class="legend">free</span>
      </div>{hidden}
      <button class="key filters-key" id="filters-toggle" type="button" aria-expanded="false" aria-controls="filters-panel">filters</button>
    </form>
    <div class="controls-row bench"><div class="unit unit-grow">{strip}</div></div>
    <div class="controls-row filters-panel" id="filters-panel" hidden>
      {panel}
      <div class="filters-foot">
        <span class="filters-foot-keys">
          <a class="key reset-key" href="{BASE_URL}/">everything on</a>
          <button class="key filters-close" id="filters-close" type="button">close</button>
        </span>
      </div>
    </div>
  </div>
  {_FILTERS_SCRIPT}"""


def home_view_params(context: dict | None) -> dict:
    """A topic carries day= so it opens as a filter rather than that
    topic's intro landing; the rest carry it for the same 30-day view."""
    params = dict(context or {})
    if params:
        params.setdefault("day", "30")
    return params


def _nav_chip(label: str, href: str, here: bool, child: bool = False) -> str:
    classes = "token" + (" token-child" if child else "") + (" lit" if here else "")
    current = ' aria-current="page"' if here else ""
    return f'<a class="{classes}" href="{href}"{current}>{label}</a>'


def display_name() -> str:
    """User-facing brand name (e.g. "PsyConnect") — distinct from
    SITE["name"], which stays lowercase since it's also a dict key
    (ABOUT_CONTENT, PRIVACY_CONTENT) and part of cache/URL identifiers."""
    return SITE_JSON.get("displayName") or SITE["name"]


def about_link_html() -> str:
    """Footer-only "about" link — never in the header. No-op for sites
    with no ABOUT_CONTENT entry (e.g. londo, today)."""
    if not ABOUT_CONTENT.get(SITE["name"]):
        return ""
    return f' · <a href="{about_url()}">about</a>'


def privacy_link_html() -> str:
    """Footer-only "privacy" link, mirroring about_link_html(). No-op for
    sites with no PRIVACY_CONTENT entry."""
    if not PRIVACY_CONTENT.get(SITE["name"]):
        return ""
    return f' · <a href="{privacy_url()}">privacy</a>'


def channel_link_html() -> str:
    """Optional "follow us" line (SITE.channel) — mirror of the SPA's
    renderChannelLink(). Empty url means the slot stays invisible."""
    channel = SITE_JSON.get("channel") or {}
    if not channel.get("url"):
        return ""
    label = channel.get("label") or "follow us"
    return (
        f'\n    <p id="channel-link"><a href="{esc(channel["url"])}" '
        f'target="_blank" rel="noopener">{esc(label)}</a></p>'
    )


def site_wordmark(css_prefix: str) -> str:
    logo = SITE_JSON.get("logo")
    if not logo:
        return esc(display_name())
    return (
        f'<img src="{css_prefix}/{esc(logo)}" alt="{esc(display_name())}" '
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
    if event.get("price_min") is None or event["price_min"] >= PRICE_CEILING:
        return ""
    low = event["price_min"]
    high = event.get("price_max")
    if high is not None and high != low:
        return f"{_pounds(low)}–{_pounds(high)}"
    return _pounds(low)


def _pounds(amount: float) -> str:
    """£7.50, not £7.5; £12, not £12.00."""
    return f"£{amount:g}" if float(amount).is_integer() else f"£{amount:.2f}"


# Some hosts fill the venue with walking directions ("13 Minutes from
# Gospel Oak…") — fine on a card, wrong in a page title or a list of rooms.
_DIRECTIONS_RE = re.compile(r"\b(mins?|minutes?|walk|from|station)\b", re.I)
_NOT_A_VENUE = {"london", "tbc", "tba", "secret location", "online"}


def clean_venue(event: dict) -> str:
    venue = (event.get("venue_name") or "").strip()
    if not venue or len(venue) > 40 or venue.lower() in _NOT_A_VENUE:
        return ""
    return "" if _DIRECTIONS_RE.search(venue) else venue


def venue_key(venue: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", re.sub(r"^the\s+", "", venue.lower()))


def tidy_address(address: str, venue: str = "") -> str:
    """An address as sources send it, minus its repeats: "3-5 Sutton Row,
    London W1D 4NR, London, W1D 4NR" -> "3-5 Sutton Row, London W1D 4NR".
    A part already said (or the venue's own name) is dropped."""
    kept: list[str] = []
    said = venue.lower()
    for part in (p.strip() for p in address.split(",")):
        low = part.lower()
        if not part or low in said or any(low in k.lower() for k in kept):
            continue
        kept.append(part)
        said += " " + low
    return ", ".join(kept)


def event_place(event: dict) -> str:
    """Where, in the fewest words: the venue, else the area."""
    venue = clean_venue(event)
    if venue:
        return venue
    return f"{event['area']} London" if event.get("area") else ""


# A hybrid listing's Zoom sign-up link, printed as text: it got the Good
# Company pages ranked for "zoom.us/meeting/register" and nothing else
_MEETING_LINK_RE = re.compile(r"(?:https?://)?[\w.-]*\bzoom\.us/\S+", re.I)


def strip_meeting_links(text: str) -> str:
    return _MEETING_LINK_RE.sub("the Zoom link on the ticket page", text)


_BULLET_RE = re.compile(r"^[•·\-*]\s+")
# sentences, prices, links, times, addresses and numbered points read as
# lines of their own, not subheads
_NOT_HEADING_RE = re.compile(
    r"[.;!…)\"'”’]$|[,£$€@]|https?:|www\.|\.(com|org|co\.uk)\b"
    r"|^[•·\-*]\s|^\d+\s*[.)]|\d\s*(am|pm)\b|\d[.:]\d\d"
    r"|\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b",
    re.I,
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def description_blocks(event: dict, limit: int = 6000) -> list[str]:
    """The blurb as the scrapers keep it: blocks between blank lines, a
    line per <br> or list item. Cut at a block edge once it runs long,
    with an opening line that only repeats the title left out."""
    text = strip_meeting_links(event.get("description") or "")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if blocks:
        first, _, rest = blocks[0].partition("\n")
        line, title = _norm(first), _norm(event.get("title") or "")
        if len(line) >= 12 and (line in title or title in line):
            blocks = ([rest.strip()] if rest.strip() else []) + blocks[1:]
    kept, size = [], 0
    for b in blocks:
        if kept and size + len(b) > limit:
            break
        kept.append(b)
        size += len(b)
    return kept


def _is_heading(block: str) -> bool:
    """A short line on its own, not a sentence: how the organisers' bold
    subheads come through ("What to expect", "Who this is for")."""
    return (
        "\n" not in block
        and len(block) <= 60
        and len(block.split()) <= 9
        and re.search(r"[A-Za-z]", block) is not None
        and not _NOT_HEADING_RE.search(block)
    )


def _inline(line: str) -> str:
    """Escaped, with Markdown **bold** (Luma, Meetup) made bold."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(line))


def prose_html(blocks: list[str]) -> str:
    # a subhead stands alone between longer blocks: a stack of short
    # lines is a poster's worth of details, not a run of headings
    # (a lone bullet over a section is a subhead in a list item's clothes)
    heads = [b if "\n" in b else _BULLET_RE.sub("", b) for b in blocks]
    short = [_is_heading(h) for h in heads] + [True]
    out = []
    for i, b in enumerate(blocks):
        lines = [ln.strip() for ln in b.split("\n") if ln.strip()]
        if short[i] and not short[i + 1] and not (i and short[i - 1]):
            out.append(f"<h3>{esc(heads[i].replace('**', '').rstrip(': '))}</h3>")
        elif all(_BULLET_RE.match(ln) for ln in lines):
            out.append(
                "<ul>"
                + "".join(f"<li>{_inline(_BULLET_RE.sub('', ln))}</li>" for ln in lines)
                + "</ul>"
            )
        else:
            out.append("<p>" + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return '<div class="static-prose">' + "".join(out) + "</div>"


def _trim(text: str, n: int = 160) -> str:
    return text if len(text) <= n else text[: n - 3].rsplit(" ", 1)[0] + "…"


def event_meta(event: dict, past: bool = False) -> tuple[str, str]:
    """The page title and meta description: what a search result shows.
    People searching an event's name want when and where before anything
    else, so both lead with it."""
    start = _start_london(event)
    day = start.strftime("%a %-d %b")
    if start.year != datetime.now(LONDON).year:
        day += start.strftime(" %Y")
    place = event_place(event)
    # "𝕤𝕡𝕖𝕔𝕚𝕒𝕝 | Acid Chess Club" reads as noise in a result: NFKC folds
    # the maths-alphabet letters back to plain ones
    name = unicodedata.normalize("NFKC", event["title"])
    title = f"{name} · {day}" + (f", {place}" if place else "")
    lead = [fmt_when_short(event).replace(" · ", ", ")]
    if place:
        lead.append(place)
    if not past and fmt_price(event):
        lead.append(fmt_price(event))
    about = event.get("hook") or strip_meeting_links(
        re.sub(r"\s+", " ", event.get("description") or "")
    )
    lead_text = ("Took place " if past else "") + " · ".join(lead)
    description = _trim(f"{lead_text}. {about}" if about else lead_text)
    return f"{title} — {display_name()}", description


RELATED_SHOWN = 3  # cards per "more like this" row (the grid's width); two rows at most

# Filled in by build() before any page is written, so the per-page blocks
# below are lookups rather than a pass over every listing per page:
# (source, source_id) -> the practice slugs it matches; host slug -> its
# upcoming listings; practice slug -> its upcoming listings.
EVENT_PRACTICES: dict[tuple[str, str], list[str]] = {}
HOST_EVENTS: dict[str, list[dict]] = {}
PRACTICE_EVENTS: dict[str, list[dict]] = {}
# the explore tiles, rendered once: (kind, key, html)
EXPLORE_TILES: list[tuple[str, str, str]] = []


def index_listings(events: list[dict], past: list[dict]) -> None:
    EVENT_PRACTICES.clear()
    HOST_EVENTS.clear()
    PRACTICE_EVENTS.clear()
    for slug_, _spec, matched in SITE_PRACTICES:
        PRACTICE_EVENTS[slug_] = matched
    for e in [*events, *past]:
        EVENT_PRACTICES[(e["source"], e["source_id"])] = [
            slug_
            for slug_, spec, _ in SITE_PRACTICES
            if practice_match(e, spec, slug_)
        ]
    for e in events:
        host = organizer_key(e)
        if host:
            HOST_EVENTS.setdefault(host[0], []).append(e)


def related_html(event: dict, past: bool = False) -> str:
    """What else to go to: the host's next dates, then more of the same
    practice (else the same topics) — two rows of cards at most, each
    with a way through to the whole listing. On a past event's page this
    is the point of the page; on an upcoming one it keeps a visitor who
    landed from a search on the site."""
    seen = {(event["source"], event["source_id"])}
    # one date per series: a row of the same night four times is one card
    titles = {slugify_title(event.get("title") or "")}
    sections = []

    def add(heading: str, pool: list[dict], more: tuple[str, str] | None) -> None:
        picked = []
        for e in pool:
            t = slugify_title(e.get("title") or "")
            if (e["source"], e["source_id"]) in seen or t in titles:
                continue
            picked.append(e)
            titles.add(t)
            if len(picked) == RELATED_SHOWN:
                break
        if not picked:
            return
        seen.update((e["source"], e["source_id"]) for e in picked)
        cards = "".join(event_card(e, dated=True) for e in picked)
        link = (
            f'<a class="static-related-more" href="{more[0]}" '
            f'data-goatcounter-click="static/related-more">{esc(more[1])} →</a>'
            if more
            else ""
        )
        sections.append(
            f'<section class="day-group static-related"><h2 class="day-heading">'
            f"<span>{esc(heading)}</span></h2>"
            f'<div class="grid">{cards}</div>{link}</section>'
        )

    host = organizer_key(event)
    if host:
        page_ = organizer_page_for(event)
        add(
            f"{'Coming up' if past else 'More'} from {host[1]}",
            HOST_EVENTS.get(host[0], []),
            (organizer_page_url(page_[0]), f"everything from {page_[1]}") if page_ else None,
        )
    practices = EVENT_PRACTICES.get((event["source"], event["source_id"]), [])
    if practices:
        slug_ = practices[0]
        spec = PRACTICES[slug_]
        add(
            f"More {spec['label']} coming up",
            PRACTICE_EVENTS.get(slug_, []),
            (practice_url(slug_), spec["seo_title"]),
        )
    else:
        topics = set(event.get("topics") or [])
        overlap = [
            (len(topics & set(e.get("topics") or [])), i, e)
            for i, e in enumerate(SITE_EVENTS)
        ]
        # most topics in common first, soonest first among equals
        add(
            "Similar gatherings coming up",
            [e for n, _, e in sorted(overlap, key=lambda t: (-t[0], t[1])) if n],
            None,
        )
    return "".join(sections)


# Line icons for the explore tiles, drawn on a 24-unit grid in the stroke
# style of the header's pin and theme icons (currentColor, round caps).
# Keyed by practice slug or topic name; anything new falls back to a spark.
EXPLORE_ICONS = {
    # practices
    "cacao-ceremony": '<path d="M5 10h11v3.5a5.5 5.5 0 0 1-5.5 5.5h0A5.5 5.5 0 0 1 5 13.5z"/>'
    '<path d="M16 11.5h1.2a2.3 2.3 0 0 1 0 4.6H15.6"/><path d="M8.5 3.5c-1 1.4 1 2.1 0 3.6M12 3.5c-1 1.4 1 2.1 0 3.6"/>'
    '<path d="M4 21.5h14"/>',
    "ecstatic-dance": '<circle cx="12" cy="4.5" r="1.8"/><path d="M12 7v6M12 9.5 7.5 6M12 9.5 16.5 6'
    'M12 13l-3.5 6.5M12 13l3.5 6"/><path d="M4 11c-1.2 2-.6 4.4.8 5.8M20 11c1.2 2 .6 4.4-.8 5.8"/>',
    "5rhythms": '<path d="M2 13c1-3.5 2.6-3.5 4 0s3 3.5 4 0 2.6-3.5 4 0 3 3.5 4 0 2.6-3.5 4 0"/>'
    '<circle cx="4" cy="19" r=".6"/><circle cx="8" cy="19" r=".6"/><circle cx="12" cy="19" r=".6"/>'
    '<circle cx="16" cy="19" r=".6"/><circle cx="20" cy="19" r=".6"/>',
    "breathwork": '<path d="M3 8.5h10.5a3 3 0 1 0-3-3"/><path d="M3 12.5h15a3 3 0 1 1-3 3"/><path d="M3 16.5h7"/>',
    "sound-bath": '<circle cx="12" cy="12" r="5.5"/><circle cx="12" cy="12" r="1.6"/>'
    '<path d="M3.5 8.5a9.5 9.5 0 0 0 0 7M20.5 8.5a9.5 9.5 0 0 1 0 7"/>',
    "somatics": '<path d="M7 13V7.5a1.5 1.5 0 0 1 3 0V12M10 11.5V5.5a1.5 1.5 0 0 1 3 0v6'
    'M13 11.5V6.5a1.5 1.5 0 0 1 3 0V13M16 11a1.5 1.5 0 0 1 3 0v4a6 6 0 0 1-6 6h-.8'
    'a6 6 0 0 1-5-2.7L4.6 14.6a1.5 1.5 0 0 1 2.4-1.8L7 13"/>',
    "contact-improvisation": '<circle cx="7.5" cy="4.5" r="1.7"/><circle cx="16.5" cy="4.5" r="1.7"/>'
    '<path d="M7.5 7 9 13l-2 7.5M16.5 7 15 13l2 7.5M8.6 10.5 12 12.5l3.4-2"/>',
    "qigong": '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a4.25 4.25 0 0 1 0 8.5 4.25 4.25 0 0 0 0 8.5"/>'
    '<circle cx="12" cy="7.75" r=".9"/><circle cx="12" cy="16.25" r=".9"/>',
    "tantra": '<path d="M12 19.5c-2.6-2.6-3.2-6.4 0-11.5 3.2 5.1 2.6 8.9 0 11.5z"/>'
    '<path d="M12 19.5c-4.4 0-8-2.8-8.5-7 3.6-.2 6.6 2 8.5 7zM12 19.5c4.4 0 8-2.8 8.5-7-3.6-.2-6.6 2-8.5 7z"/>',
    "authentic-relating": '<path d="M4 4.5h9.5a2 2 0 0 1 2 2V11a2 2 0 0 1-2 2H8.5L5.5 15.5V13H4'
    'a2 2 0 0 1-2-2V6.5a2 2 0 0 1 2-2z"/><path d="M18 8.5h2a2 2 0 0 1 2 2V15a2 2 0 0 1-2 2h-1.5v2.5'
    'L15.5 17H12a2 2 0 0 1-2-2v-.5"/>',
    "family-constellations": '<circle cx="12" cy="4.5" r="1.8"/><circle cx="5" cy="17.5" r="1.8"/>'
    '<circle cx="19" cy="17.5" r="1.8"/><circle cx="12" cy="12.5" r="1.8"/>'
    '<path d="M12 6.3v4.4M10.6 13.6 6.4 16.4M13.4 13.6l4.2 2.8M6.8 17.5h10.4"/>',
    "kirtan": '<path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4"/>',
    "bohm-dialogue": '<circle cx="12" cy="4.5" r="1.5"/><circle cx="18.5" cy="8.25" r="1.5"/>'
    '<circle cx="18.5" cy="15.75" r="1.5"/><circle cx="12" cy="19.5" r="1.5"/>'
    '<circle cx="5.5" cy="15.75" r="1.5"/><circle cx="5.5" cy="8.25" r="1.5"/>',
    # topics
    "psychedelics": '<path d="M3.5 11.5a8.5 6.5 0 0 1 17 0z"/><path d="M10 11.5v6.5a2 2 0 0 0 4 0v-6.5"/>'
    '<circle cx="9" cy="8.5" r=".8"/><circle cx="14.5" cy="7.5" r=".8"/>',
    "consciousness": '<path d="M2 12s3.8-6.5 10-6.5S22 12 22 12s-3.8 6.5-10 6.5S2 12 2 12z"/>'
    '<circle cx="12" cy="12" r="3"/>',
    "connection & intimacy": '<path d="M12 20s-7.5-4.6-7.5-10.2A4.2 4.2 0 0 1 12 7.2a4.2 4.2 0 0 1 7.5 2.6'
    'C19.5 15.4 12 20 12 20z"/>',
    "healing & wellbeing": '<path d="M5 19c0-8 5.5-14 14.5-14 0 9-6 14-14.5 14z"/><path d="M5 19l8.5-8.5"/>',
    "spirituality & ritual": '<path d="M12 3c2.2 3 4 5 4 7.8a4 4 0 0 1-8 0c0-1.9 1-3.1 2-4.1 0 1.9.9 2.9 2 2.9'
    ' 0-2-1.2-3.8 0-6.6z"/><path d="M7 20.5h10M9 17.5h6"/>',
    "tech & ai": '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M10 3.5V7M14 3.5V7M10 17v3.5'
    'M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>',
    "startups & work": '<rect x="3.5" y="7.5" width="17" height="11.5" rx="1.5"/>'
    '<path d="M9 7.5V6a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 6v1.5M3.5 12.5h17"/>',
    "arts & creativity": '<path d="M12 3a9 9 0 1 0 0 18c1 0 1.6-.7 1.6-1.5 0-.6-.4-1-.4-1.6 0-.8.6-1.4 1.4-1.4'
    'H17a4 4 0 0 0 4-4C21 7.3 17 3 12 3z"/><circle cx="7.5" cy="11" r=".9"/><circle cx="10" cy="7"'
    ' r=".9"/><circle cx="14.5" cy="7" r=".9"/>',
    "music & sound": '<path d="M9 18V6l10-2v12"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="16" r="2"/>',
    "nature & outdoors": '<path d="M2.5 20 9 9.5l4 6.5 3-4 5.5 8z"/><circle cx="17.5" cy="5.5" r="2"/>',
    "society & politics": '<circle cx="12" cy="7" r="2.3"/><circle cx="5.5" cy="9" r="1.8"/>'
    '<circle cx="18.5" cy="9" r="1.8"/><path d="M7.5 19.5v-2.5a4.5 4.5 0 0 1 9 0v2.5'
    'M2.5 18.5v-1.5a3 3 0 0 1 4-2.8M21.5 18.5v-1.5a3 3 0 0 0-4-2.8"/>',
    "science & ideas": '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6'
    'c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/>',
}
_SPARK_ICON = '<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18M18 6l-2.5 2.5M8.5 15.5 6 18"/>'


def explore_icon(key: str, tone: int) -> str:
    return (
        f'<span class="explore-art explore-tone-{tone % 3}" aria-hidden="true">'
        '<svg viewBox="0 0 24 24">'
        f"{EXPLORE_ICONS.get(key, _SPARK_ICON)}</svg></span>"
    )


def build_explore_tiles(events: list[dict]) -> None:
    """Every practice and topic with something on in the next 30 days, as
    a tile with its icon and count — the site's whole range in one
    glance, at the foot of every event and listing page."""
    EXPLORE_TILES.clear()
    month = within_window(events, 30)

    def tile(kind: str, key: str, href: str, label: str, n: int) -> None:
        EXPLORE_TILES.append((
            kind,
            key,
            f'<a class="explore-tile" href="{href}" '
            f'data-goatcounter-click="static/explore-{kind}">'
            f"{explore_icon(key, len(EXPLORE_TILES))}"
            f'<span class="explore-text"><span class="explore-name">{esc(label)}</span>'
            f'<span class="explore-count">{n} this month</span></span></a>',
        ))

    for slug_, spec, matched in SITE_PRACTICES:
        n = len(within_window(matched, 30))
        if n:
            tile("practice", slug_, practice_url(slug_), spec.get("chip") or spec["label"], n)
    site_topics = SITE_JSON.get("topics")
    for key, (slug_, _) in TOPICS.items():
        if site_topics is not None and key not in site_topics:
            continue
        n = sum(1 for e in month if key in (e.get("topics") or []))
        if n:
            tile("topic", key, topic_url(slug_), key, n)


def explore_html(kind: str | None = None, key: str | None = None) -> str:
    tiles = "".join(h for k, kk, h in EXPLORE_TILES if not (k == kind and kk == key))
    if not tiles:
        return ""
    return (
        '<section class="static-explore">'
        f"<h2>Explore {esc(display_name())}</h2>"
        f'<div class="explore-grid">{tiles}</div></section>'
    )


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
    for slug_ in EVENT_PRACTICES.get((event["source"], event["source_id"]), []):
        chips.append(
            f'<a class="static-chip" href="{practice_url(slug_)}">'
            f'{esc(PRACTICES[slug_]["label"])}</a>'
        )
    if not chips:
        return ""
    return f'<p class="static-chips">{"".join(chips)}</p>'


# "← back" when the reader came from one of our pages (history keeps
# their place in the list); a plain way home when they landed from search
_BACK_SCRIPT = (
    "<script>(()=>{const a=document.getElementById('crumb-back');"
    "let ours=false;try{ours=new URL(document.referrer).origin===location.origin}catch(_){}"
    "if(a&&ours&&history.length>1){a.textContent='← back';"
    "a.addEventListener('click',e=>{e.preventDefault();history.back()})}})();</script>"
)


# A long blurb folds: on a wide screen where the picture-and-details
# column ends, so the two columns finish together; on a phone after a
# paragraph or two. Measured on the page — art and text vary — and only
# when there's a good few lines to hide.
_READ_MORE_SCRIPT = """<script>
(() => {
  const body = document.querySelector(".static-event-body");
  const prose = body && body.querySelector(".static-prose");
  const btn = body && body.querySelector(".read-more");
  const aside = document.querySelector(".static-event-aside");
  const article = document.querySelector(".static-event");
  if (!prose || !btn) return;
  const wide = matchMedia("(min-width: 900px)");
  let open = false;
  function cap() {
    const top = prose.getBoundingClientRect().top;
    if (wide.matches && aside) {
      // the aside is sticky, so measure its height from the article top,
      // not where it happens to sit on screen
      const end = article.getBoundingClientRect().top + aside.offsetHeight;
      return Math.max(end - top, 160);
    }
    const ps = prose.querySelectorAll("p");
    const p = ps[1] || ps[0];
    const after = p ? p.getBoundingClientRect().bottom - top : 260;
    return Math.min(Math.max(after, 160), 260);
  }
  function fit() {
    if (open) return;
    prose.style.maxHeight = "";
    body.classList.remove("folded");
    const limit = cap();
    const fold = prose.scrollHeight - limit > 110;
    if (fold) {
      prose.style.maxHeight = limit + "px";
      body.classList.add("folded");
    }
    btn.hidden = !fold;
  }
  btn.addEventListener("click", () => {
    open = true;
    prose.style.maxHeight = "";
    body.classList.remove("folded");
    btn.hidden = true;
  });
  fit();
  addEventListener("resize", fit);
  addEventListener("load", fit);
  if (aside) aside.querySelectorAll("img").forEach((i) => i.addEventListener("load", fit));
})();
</script>"""


def event_crumbs(event: dict) -> str:
    """Above the title: a way back, then the practice (or host) page the
    event belongs to — one more step into the site from a search landing."""
    parts = [
        f'<a id="crumb-back" href="{BASE_URL}/" '
        'data-goatcounter-click="static/crumb-back">← all events</a>'
    ]
    # the most specific practice: 5Rhythms before ecstatic dance
    practices = sorted(
        EVENT_PRACTICES.get((event["source"], event["source_id"]), []),
        key=lambda p: not PRACTICES[p].get("parent"),
    )
    host = organizer_page_for(event)
    if practices:
        spec = PRACTICES[practices[0]]
        parts.append(
            f'<a href="{practice_url(practices[0])}">{esc(spec.get("chip") or spec["label"])}</a>'
        )
    elif host:
        parts.append(f'<a href="{organizer_page_url(host[0])}">{esc(host[1])}</a>')
    sep = '<span aria-hidden="true">·</span>'
    return (
        f'<nav class="static-crumbs" aria-label="breadcrumb">{sep.join(parts)}</nav>'
        + _BACK_SCRIPT
    )


def event_page(event: dict, past: bool = False) -> str:
    """An event's own page. `past`: it has happened — the page stays up
    for the searches still landing on it, minus the ticket offer, and
    points on to what's coming up instead."""
    canonical = event_url(event)
    when = fmt_when(event)
    venue = event.get("venue_name") or ""
    address = event.get("address") or ""
    where = ", ".join(p for p in (venue, address) if p) or "London"
    price = fmt_price(event)
    org = event.get("organizer_name") or ""
    page_title, description = event_meta(event, past)

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
    # the site's share card stands in when a listing has no picture
    image = event.get("image_url") or DEFAULT_OG_IMAGE
    if image:
        json_ld["image"] = [image]
    if event.get("description"):
        json_ld["description"] = strip_meeting_links(
            re.sub(r"\s+", " ", event["description"])
        )[:500]
    elif event.get("hook"):
        json_ld["description"] = event["hook"]
    host_page = organizer_page_for(event)
    if org:
        # the host's own site, else their page here
        org_url = event.get("organizer_url") or (
            organizer_page_url(host_page[0]) if host_page else None
        )
        json_ld["organizer"] = {"@type": "Organization", "name": org}
        if org_url:
            json_ld["organizer"]["url"] = org_url
        # these are hosted sessions — the host is who leads the room
        json_ld["performer"] = dict(json_ld["organizer"])
    # no price, no offer: a bare ticket link only trades Google's
    # "missing offers" warning for "missing price"
    offer: dict = {}
    if past:
        pass  # nothing left to sell
    elif event.get("is_free"):
        json_ld["isAccessibleForFree"] = True
        offer = {"price": "0", "priceCurrency": "GBP"}
    elif fmt_price(event):
        offer = {"price": str(event["price_min"]), "priceCurrency": "GBP"}
    if offer:
        json_ld["offers"] = {
            "@type": "Offer",
            **offer,
            "availability": "https://schema.org/InStock",
            **({"url": event["source_url"]} if event.get("source_url") else {}),
            # on sale no later than when we first saw it listed
            **(
                {"validFrom": event["first_seen_at"]}
                if event.get("first_seen_at")
                else {}
            ),
        }

    # the details as a short list under the picture: the day and the
    # hours; the room, then its address and a map; price; who's hosting
    start = _start_london(event)
    day_text = start.strftime("%A %-d %B")
    if start.year != datetime.now(LONDON).year:
        day_text += start.strftime(" %Y")
    when_html = f'<span class="fact-main">{esc(day_text)}</span>'
    if not event.get("is_all_day"):
        when_html += f'<span class="fact-sub">{esc(fmt_time_range(event))}</span>'
    place = venue or address or "London"
    rest = tidy_address(address, venue) if venue and address and address != venue else ""
    map_q = urllib.parse.quote_plus(", ".join(p for p in (venue, address, "London") if p))
    room = venue_page_for(event)
    where_html = (
        (
            f'<a class="fact-main fact-venue" href="{venue_page_url(room)}">{esc(place)}</a>'
            if room
            else f'<span class="fact-main">{esc(place)}</span>'
        )
        + (f'<span class="fact-sub">{esc(rest)}</span>' if rest else "")
        + f'<a class="fact-link" href="https://www.google.com/maps/search/?api=1&amp;query={map_q}" '
        f'rel="noopener" target="_blank" data-goatcounter-click="static/map-venue">map ↗</a>'
    )
    facts = []
    facts.append(
        f'<div class="static-fact"><dt>When</dt><dd>{when_html}</dd></div>'
    )
    facts.append(
        f'<div class="static-fact"><dt>Where</dt><dd>{where_html}</dd></div>'
    )
    if price and not past:
        facts.append(
            f'<div class="static-fact"><dt>Price</dt><dd>{esc(price)}</dd></div>'
        )
    if host_page:
        host_slug, host_label = host_page
        facts.append(
            f'<div class="static-fact"><dt>Host</dt><dd>'
            f'<a href="{organizer_page_url(host_slug)}" '
            f'data-goatcounter-click="host/{esc(host_slug)}">{esc(host_label)}</a>'
            f"</dd></div>"
        )
    elif org:
        facts.append(
            f'<div class="static-fact"><dt>Host</dt><dd>{esc(org)}</dd></div>'
        )

    hook = (
        f'<p class="static-hook">{esc(event["hook"])}</p>'
        if event.get("hook")
        else ""
    )
    img = ""
    if event.get("image_url"):
        art = event["image_url"]
        # the proxy is a single point of failure for every image on the site,
        # so fall back to the organiser's own CDN. %27 rather than a bare
        # quote: esc() emits &#x27;, which the parser turns back into ' and
        # closes the JS string early.
        fallback = esc(art.replace("'", "%27"))
        img = (
            f'<figure class="static-figure">'
            f'<img src="{esc(thumb(art, 1200))}" alt="{esc(event["title"])}" '
            f'loading="lazy" '
            f"onerror=\"this.onerror=null;this.src='{fallback}'\">"
            f"</figure>"
        )
    paragraphs = description_blocks(event)
    if paragraphs:
        desc_html = prose_html(paragraphs)
    else:
        desc_html = (
            '<div class="static-prose">'
            f"<p>{esc(event.get('hook') or 'An in-person gathering in London.')}</p>"
            "</div>"
        )

    top_cta = ""
    if past:
        cta = (
            '<p class="static-past-note">'
            f"This took place on {esc(day_text)}. What's on instead is below.</p>"
        )
    elif event.get("source_url"):
        # one button, up by the title, where a visitor from a search wants it
        top_cta = f"""<p class="static-cta-wrap static-cta-top">
      <a class="static-cta" href="{esc(with_utm(event["source_url"]))}" rel="noopener"
         data-goatcounter-click="out/{esc(organizer_slug(org))}"
         data-goatcounter-title="{esc(event["title"])}">
        Tickets &amp; details <span aria-hidden="true">↗</span>
      </a>{f'<span class="static-cta-price">{esc(price)}</span>' if price else ''}
    </p>"""
        cta = ""
    else:
        # a flyer shared in the community chat, with no page of its own:
        # the details above are all there is
        cta = ('<p class="static-cta-wrap static-cta-note">'
               "shared in the community chat — no ticket page; "
               "details as posted above</p>")

    related = related_html(event, past)

    # past: the old blurb is context, not the point — what's on instead is
    if past and paragraphs:
        flat = re.sub(r"\s+", " ", " ".join(paragraphs).replace("**", ""))
        desc_html = (
            '<div class="static-prose">'
            f"<p>{esc(_trim(flat, 420))}</p>"
            "</div>"
        )
    # title, line and ticket button first; the picture with the details
    # under it alongside (below, on a phone); then the blurb; then, full
    # width, what else to go to
    body = f"""
  {static_controls(home_date_strip_html(event), None, "event")}
  <article class="static-event">
    <div class="static-event-head">
      {event_crumbs(event)}
      <h1 class="static-title">{esc(event["title"])}</h1>
      {hook}
      {top_cta}{cta}
    </div>
    <aside class="static-event-aside">
      {img}
      <dl class="static-facts static-facts-list">{"".join(facts)}</dl>
      {topic_chips(event)}
    </aside>
    <div class="static-event-body">
      {desc_html}
      <button class="read-more" type="button" hidden>Read more</button>
    </div>
    {_READ_MORE_SCRIPT}
  </article>
  {related}
  {explore_html()}
  <p class="static-back">
    <a href="{BASE_URL}/">← more in-person gatherings on {esc(display_name())}</a>
  </p>"""
    return page(
        page_title,
        description,
        canonical,
        event.get("image_url") or DEFAULT_OG_IMAGE,
        body,
        json_ld,
    )


def listing_intro_paragraphs(
    key: str, kind: str, label: str, count: int, days: int = 7
) -> list[str]:
    """Warm prose for listing pages, plus a light freshness line."""
    if kind == "category":
        paras = list(CATEGORY_INTROS.get(key) or ())
    elif kind == "practice":
        paras = list(PRACTICES[key]["intro"])
    elif kind == "organizer":
        paras = list(ORGANIZERS.get(key, {}).get("intro") or ()) or [
            f"In-person gatherings hosted by {label} in London, collected on "
            f"{display_name()} with the rest of what's on."
        ]
    else:
        paras = list(TOPIC_INTROS.get(key) or ())
    if not paras:
        paras = [
            f"In-person {label} gatherings in London, collected on {display_name()} "
            f"so you can find the good rooms without scrolling forever."
        ]
    window = "the next seven days" if days == 7 else f"the next {days} days"
    if kind == "organizer":
        return [
            *paras,
            f"{count} coming up — times, venues and tickets, "
            "refreshed several times a day."
            if count > 1
            else "One coming up — time, venue and tickets below."
            if count
            else f"Nothing on from {label} right now — "
            "here's what they've hosted recently.",
        ]
    freshness = (
        f"Nothing on in {window} right now — recent ones are listed below, "
        "and new dates appear here as soon as they're announced."
        if count == 0
        else f"{count} on in {window} — times, venues and tickets, "
        f"refreshed several times a day."
        if count != 1
        else f"One on in {window} — times, venue and tickets below."
    )
    return [*paras, freshness]


def faq_html(faq: list[tuple[str, str]]) -> str:
    """The questions people actually type, answered in prose. Marked up
    as a definition list, and repeated as FAQPage JSON-LD."""
    items = "".join(
        f'<div class="faq-item"><dt>{esc(q)}</dt><dd>{esc(a)}</dd></div>'
        for q, a in faq
    )
    return (
        '<section class="static-faq">'
        "<h2>Common questions</h2>"
        f"<dl>{items}</dl>"
        "</section>"
    )


def listing_json_ld(
    seo_title: str, canonical: str, description: str,
    events: list[dict], faq: list[tuple[str, str]],
) -> dict:
    """A collection page, the events on it, and its FAQ — one graph."""
    graph: list[dict] = [
        {
            "@type": "CollectionPage",
            "name": seo_title,
            "description": description,
            "url": canonical,
        },
        {
            "@type": "ItemList",
            "name": seo_title,
            "numberOfItems": len(events),
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": i + 1,
                    "url": event_url(e),
                    "name": e["title"],
                }
                for i, e in enumerate(events[:20])
            ],
        },
    ]
    if faq:
        graph.append(
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": a},
                    }
                    for q, a in faq
                ],
            }
        )
    return {"@context": "https://schema.org", "@graph": graph}


def listing_page(
    key: str,
    label: str,
    seo_title: str,
    canonical: str,
    events: list[dict],
    kind: str = "topic",
    days: int = 7,
    other_url: str | None = None,
    css_prefix: str = NESTED_PREFIX,
    strip_events: list[dict] | None = None,
    intro: list[str] | None = None,
    extra_html: str = "",
    faq_items: list[tuple[str, str]] | None = None,
    nav: tuple[str, str] | None = None,
    context: dict | None = None,
    where: list[str] | None = None,
    placeholder: str = "search…",
    head_html: str = "",
    past_html: str = "",
    json_ld_extra: list[dict] | None = None,
    ahead: list[dict] | None = None,
) -> str:
    paras = intro or listing_intro_paragraphs(key, kind, label, len(events), days)
    lead_html = "".join(f'<p class="static-lead">{esc(p)}</p>' for p in paras)
    # meta description: first paragraph, kept short
    meta_desc = paras[0]
    if len(meta_desc) > 160:
        meta_desc = meta_desc[:157].rsplit(" ", 1)[0] + "…"

    groups = week_groups(events[:200])
    # a thin month carries on past its end; the week page has the month
    # one click away instead
    ahead_html = (
        further_ahead_html(ahead)
        if ahead and days == 30 and len(events) < SPARSE_BELOW
        else ""
    )
    # a host's or a room's page is its whole diary, not a range of the
    # calendar: its strip opens the main page on a day instead
    diary = kind in ("organizer", "venue")
    strip = (
        home_date_strip_html()
        if diary
        else date_strip_html(strip_events or events, days, other_url)
    )
    # the pin maps this page's own listings; a host's, everything (the
    # main page has no host filter)
    map_href = (
        home_view_url(context, on_map=True)
        if context or diary
        else None
    )
    # leave out the page's own tile (an area page is its practice's)
    explore_key = ("practice", key) if kind in ("practice", "area") else (kind, key)
    if faq_items is None:
        faq_items = PRACTICES[key].get("faq") or [] if kind == "practice" else []
    faq = faq_html(faq_items) if faq_items else ""
    json_ld = (
        listing_json_ld(seo_title, canonical, meta_desc, events, faq_items)
        if kind in ("practice", "organizer", "area", "venue")
        else None
    )
    if json_ld and json_ld_extra:
        json_ld["@graph"].extend(json_ld_extra)
    org = SITE_ORGANIZERS.get(key) if kind == "organizer" else None
    own_site = past = ""
    back_hosts = ""
    if org:
        node = {"@type": "Organization", "name": label}
        if org["url"]:
            node["url"] = org["url"]
            own_site = (
                f'<p class="static-back static-org-site"><a href="{esc(org["url"])}" '
                f'rel="noopener" data-goatcounter-click="org-site/{esc(key)}">'
                f"{esc(label)}'s own page ↗</a></p>"
            )
        json_ld["@graph"].append(node)
        past = past_events_html(org["past"])
        back_hosts = (
            f'\n  <p class="static-back">'
            f'<a href="{organizers_index_url()}">← all hosts on {esc(display_name())}</a></p>'
        )

    body = f"""
  {static_controls(strip, *(nav or (key, kind)), context=context, where=where, placeholder=placeholder)}
  <header class="static-list-head">
    <p class="static-kicker">in person · London</p>
    <h1 class="static-title">{esc(seo_title)}</h1>
    <div class="static-intro">
      {lead_html}
    </div>
    {own_site}{head_html}
  </header>
  {groups}
  {ahead_html}
  {extra_html}
  {past or past_html}
  {faq}
  {explore_html(*explore_key)}{back_hosts}
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>"""
    return page(
        f"{seo_title} — {display_name()}",
        meta_desc,
        canonical,
        DEFAULT_OG_IMAGE,
        body,
        json_ld=json_ld,
        css_prefix=css_prefix,
        body_class="static-page static-listing",
        map_href=map_href,
    )


def past_events_html(past: list[dict], heading: str = "Recently hosted") -> str:
    """Recent history: plain muted lines rather than cards, so nothing
    here reads as something you could still go to. Each links to the
    event's own page, which says plainly that it has passed."""
    if not past:
        return ""
    this_year = datetime.now(LONDON).year
    items = []
    for e in past[:PAST_SHOWN]:
        start = _start_london(e)
        when = start.strftime(
            "%a %-d %b" if start.year == this_year else "%a %-d %b %Y"
        )
        bits = [f'<a href="{event_url(e)}">{esc(e["title"])}</a>']
        if e.get("venue_name"):
            bits.append(esc(e["venue_name"]))
        items.append(
            f'<li><time datetime="{start.date().isoformat()}">{esc(when)}</time>'
            f' · {" · ".join(bits)}</li>'
        )
    return (
        f'<section class="static-past"><h2>{esc(heading)}</h2>'
        f'<ul>{"".join(items)}</ul></section>'
    )


def further_ahead_html(ahead: list[dict]) -> str:
    """Past the month, as lines by month rather than cards: dates that
    far out still move, so they're a heads-up, not the main listing."""
    if not ahead:
        return ""
    months: dict[str, list[str]] = {}
    for e in ahead[:AHEAD_SHOWN]:
        start = _start_london(e)
        bits = [f'<a href="{event_url(e)}">{esc(e["title"])}</a>']
        if e.get("venue_name"):
            bits.append(esc(e["venue_name"]))
        months.setdefault(start.strftime("%B"), []).append(
            f'<li><time datetime="{start.date().isoformat()}">'
            f'{esc(start.strftime("%a %-d %b"))}</time> · {" · ".join(bits)}</li>'
        )
    more = len(ahead) - AHEAD_SHOWN
    return (
        '<section class="static-past static-ahead"><h2>Further ahead</h2>'
        + "".join(
            f"<h3>{esc(m)}</h3><ul>{''.join(rows)}</ul>" for m, rows in months.items()
        )
        + (f'<p class="static-glance-note">and {more} more</p>' if more > 0 else "")
        + "</section>"
    )


def related_practices_html(slug_: str, count: int) -> str:
    """For a quiet month: the practices nearest this one, with what they
    have on, so a visitor has somewhere to go."""
    if count >= THIN_BELOW:
        return ""
    on = {s: len(within_window(m, 30)) for s, _, m in SITE_PRACTICES}
    near = [
        s for s in RELATED_PRACTICES.get(slug_, [])
        if on.get(s, 0) > count and s in PRACTICES
    ][:3]
    if not near:
        return ""
    links = " · ".join(
        f'<a href="{practice_url(s)}">{esc(PRACTICES[s].get("chip") or PRACTICES[s]["label"])}</a>'
        f" ({on[s]})"
        for s in near
    )
    # the intro has already said how few: this is just where else to look
    return f'<p class="static-back static-areas static-thin">More on in similar practices: {links}</p>'


def organizers_index_page() -> str:
    """Every host with a page, busiest first."""
    canonical = organizers_index_url()
    intro = (
        f"The organisers, teachers and communities whose gatherings appear "
        f"on {display_name()} — each with what's coming up and what "
        f"they've hosted recently."
    )
    rows = sorted(
        SITE_ORGANIZERS.items(),
        key=lambda kv: (-len(kv[1]["upcoming"]), kv[1]["label"].lower()),
    )
    items = "".join(
        f'<li><a href="{organizer_page_url(slug_)}">{esc(org["label"])}</a>'
        f'<span class="static-hosts-count">'
        f'{len(org["upcoming"]) or "nothing"} coming up</span></li>'
        for slug_, org in rows
    )
    body = f"""
  {static_controls(home_date_strip_html(), None, "hosts")}
  <header class="static-list-head">
    <p class="static-kicker">in person · London</p>
    <h1 class="static-title">Hosts &amp; organisers</h1>
    <div class="static-intro">
      <p class="static-lead">{esc(intro)}</p>
    </div>
  </header>
  <ul class="static-hosts">{items}</ul>
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>"""
    return page(
        f"Hosts & organisers — {display_name()}",
        intro,
        canonical,
        DEFAULT_OG_IMAGE,
        body,
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Hosts & organisers",
            "description": intro,
            "url": canonical,
        },
        css_prefix="..",
        body_class="static-page static-listing",
    )


def venue_title(v: dict) -> str:
    label = v["label"]
    return f"What's on at {label}" + ("" if "london" in label.lower() else ", London")


def _and_list(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def venue_page_extras(slug_: str, v: dict) -> dict:
    """A room's page: what tends to happen there and who runs it, where
    it is, then its diary and what's been on."""
    label, every = v["label"], v["upcoming"] + v["past"]
    practices: dict[str, int] = {}
    hosts: dict[tuple[str, str], int] = {}
    for e in every:
        for p in EVENT_PRACTICES.get((e["source"], e["source_id"]), []):
            if not PRACTICES[p].get("parent"):
                practices[p] = practices.get(p, 0) + 1
        h = organizer_key(e)
        if h:
            hosts[h] = hosts.get(h, 0) + 1
    top = sorted(practices, key=lambda p: (-practices[p], p))[:3]
    kinds = ", ".join(PRACTICES[p]["label"] for p in top)
    where = f"in {v['area']} London" if v.get("area") else "in London"
    lead = (
        f"{kinds[:1].upper()}{kinds[1:]} and more at {label}, {where}"
        if kinds
        else f"Gatherings at {label}, {where}"
    ) + f" — everything on {display_name()} that's held there, with times, prices and tickets."
    n = len(v["upcoming"])
    fresh = (
        f"{n} coming up — refreshed several times a day."
        if n > 1
        else "One coming up — time, price and tickets below."
        if n == 1
        else "Nothing listed here right now — here's what's been on "
        "recently, and new dates appear as soon as they're announced."
    )

    bits = []
    if v["address"]:
        bits.append(esc(v["address"]))
    map_q = urllib.parse.quote_plus(", ".join(p for p in (label, v["address"], "London") if p))
    bits.append(
        f'<a href="https://www.google.com/maps/search/?api=1&amp;query={map_q}" '
        f'rel="noopener" target="_blank" data-goatcounter-click="static/venue-map">map ↗</a>'
    )
    host_links = []
    for (h_slug, h_label) in sorted(hosts, key=lambda h: (-hosts[h], h[1].lower()))[:5]:
        host_links.append(
            f'<a href="{organizer_page_url(h_slug)}">{esc(h_label)}</a>'
            if h_slug in SITE_ORGANIZERS
            else esc(h_label)
        )
    head = f'<p class="static-venue-facts">{" · ".join(bits)}</p>'
    if host_links:
        head += (
            f'<p class="static-venue-facts">Hosts here include '
            f'{_and_list(host_links)}.</p>'
        )

    place: dict = {"@type": "Place", "name": label, "url": venue_page_url(slug_)}
    if v["address"]:
        place["address"] = {
            "@type": "PostalAddress",
            "streetAddress": v["address"],
            "addressLocality": "London",
            "addressCountry": "GB",
        }
    if v.get("geo"):
        place["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": v["geo"][0],
            "longitude": v["geo"][1],
        }
    return {
        "intro": [lead, fresh],
        "head_html": head,
        "past_html": past_events_html(v["past"], f"Recently at {label}"),
        "json_ld_extra": [place],
        "nav": (slug_, "venue"),
    }


def venues_index_page() -> str:
    """Every room with a page, busiest first, with its part of town."""
    canonical = venues_index_url()
    intro = (
        f"The studios, halls and community spaces where the gatherings on "
        f"{display_name()} happen — each with what's coming up there and "
        f"what's been on recently."
    )
    rows = sorted(
        SITE_VENUES.items(),
        key=lambda kv: (-len(kv[1]["upcoming"]), kv[1]["label"].lower()),
    )
    items = "".join(
        f'<li><a href="{venue_page_url(slug_)}">{esc(v["label"])}</a>'
        f'<span class="static-hosts-count">'
        + (f'{esc(v["area"])} · ' if v.get("area") else "")
        + f'{len(v["upcoming"]) or "nothing"} coming up</span></li>'
        for slug_, v in rows
    )
    body = f"""
  {static_controls(home_date_strip_html(), None, "venues")}
  <header class="static-list-head">
    <p class="static-kicker">in person · London</p>
    <h1 class="static-title">Venues</h1>
    <div class="static-intro">
      <p class="static-lead">{esc(intro)}</p>
    </div>
  </header>
  <ul class="static-hosts">{items}</ul>
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>"""
    return page(
        f"Venues — {display_name()}",
        intro,
        canonical,
        DEFAULT_OG_IMAGE,
        body,
        json_ld={
            "@context": "https://schema.org",
            "@type": "CollectionPage",
            "name": "Venues",
            "description": intro,
            "url": canonical,
        },
        css_prefix="..",
        body_class="static-page static-listing",
    )


# ---------- tonight / this weekend ----------
# Landing pages for the searches people make on the day ("things to do
# tonight london", "this weekend"): crawlable, in the sitemap and the
# static footer, not part of the app. They're rebuilt with the site four
# times a day, so the page carries its own clock: a card hides once its
# event is over, and a page whose day has gone says so and points at today.
WHEN_PAGES = ("tonight", "this-weekend")
EVENING = 17  # the hour "tonight" starts


def _when_pages_on() -> bool:
    return bool((SITE_JSON.get("features") or {}).get("whenPages"))


def _ends_at(event: dict) -> datetime:
    if event.get("end_at"):
        return _parse_ts(event["end_at"])
    return _parse_ts(event["start_at"]) + timedelta(hours=2)


def _when_sections(days: list[tuple[str, list[dict]]]) -> str:
    sections = []
    for heading, evs in days:
        if not evs:
            continue
        cards = "".join(
            event_card(e).replace(
                '<a class="card',
                f'<a data-ends="{int(_ends_at(e).timestamp() * 1000)}" class="card',
                1,
            )
            for e in evs
        )
        sections.append(
            f'<section class="day-group"><h2 class="day-heading">'
            f"<span>{esc(heading)}</span></h2>"
            f'<div class="grid">{cards}</div></section>'
        )
    return "".join(sections)


_WHEN_SCRIPT = """<script>
(() => {
  // the HTML is from the last build: drop what has finished since, and
  // own up if the day itself has passed
  const now = Date.now();
  document.querySelectorAll(".card[data-ends]").forEach((c) => {
    if (+c.dataset.ends < now) c.remove();
  });
  document.querySelectorAll(".static-when .day-group").forEach((g) => {
    if (!g.querySelector(".card")) g.remove();
  });
  const stale = document.getElementById("when-stale");
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London" }).format(new Date());
  if (stale && today > stale.dataset.until) stale.hidden = false;
})();
</script>"""


def when_page(which: str, events: list[dict]) -> str:
    now = datetime.now(LONDON)
    today = now.date()
    live = [e for e in events if _ends_at(e) > now]

    def on(day) -> list[dict]:
        return [e for e in live if _start_london(e).date() == day]

    if which == "tonight":
        todays = on(today)
        days = [
            ("Today", [e for e in todays if _start_london(e).hour < EVENING]),
            ("Tonight", [e for e in todays if _start_london(e).hour >= EVENING]),
        ]
        shown = todays
        until = today
        when_text = "today and tonight"
        if not todays:
            tomorrow = today + timedelta(days=1)
            shown = on(tomorrow)
            days = [(f"Nothing more on today — tomorrow, {tomorrow:%A %-d %B}", shown)]
            until = tomorrow
            when_text = "tomorrow"
        seo_title = "What's on in London today & tonight"
        dates = today.strftime("%A %-d %B")
    else:
        # this week's Friday: ahead on a weekday, just gone on Sat/Sun
        friday = today + timedelta(days=4 - today.weekday())
        weekend = [friday, friday + timedelta(days=1), friday + timedelta(days=2)]
        days = [
            (
                f"{d:%A %-d %B}" + (" evening" if i == 0 else ""),
                [e for e in on(d) if i or _start_london(e).hour >= EVENING],
            )
            for i, d in enumerate(weekend)
        ]
        shown = [e for _, evs in days for e in evs]
        until = weekend[-1]
        seo_title = "What's on in London this weekend"
        when_text = "this weekend"
        dates = (
            f"{weekend[0]:%a %-d} – {weekend[-1]:%a %-d %B}"
            if weekend[0].month == weekend[-1].month
            else f"{weekend[0]:%a %-d %b} – {weekend[-1]:%a %-d %b}"
        )

    counts: dict[str, int] = {}
    for e in shown:
        for p in EVENT_PRACTICES.get((e["source"], e["source_id"]), []):
            if not PRACTICES[p].get("parent"):
                counts[p] = counts.get(p, 0) + 1
    kinds = ", ".join(
        PRACTICES[p]["label"] for p in sorted(counts, key=lambda p: (-counts[p], p))[:3]
    )
    n = len(shown)
    lead = ("Nothing more on today. " if when_text == "tomorrow" else "") + (
        f"{n} gatherings in London {when_text}"
        if n != 1
        else f"One gathering in London {when_text}"
    ) + (f" — {kinds} and more" if kinds else "") + (
        ", with times, prices and tickets. Refreshed several times a day."
    )
    canonical = f"{BASE_URL}/{which}/"
    groups = _when_sections(days) or (
        '<p class="static-lead">Nothing listed yet — '
        f'<a href="{BASE_URL}/">see everything coming up</a>.</p>'
    )
    body = f"""
  {static_controls(home_date_strip_html(), None, which)}
  <header class="static-list-head">
    <p class="static-kicker">in person · London · {esc(dates)}</p>
    <h1 class="static-title">{esc(seo_title)}</h1>
    <div class="static-intro">
      <p class="static-lead">{esc(lead)}</p>
    </div>
    <p class="static-venue-facts" id="when-stale" data-until="{until.isoformat()}" hidden>
      This list was for {esc(until.strftime("%A %-d %B"))}. <a href="{BASE_URL}/?day=today">See what's on today →</a></p>
  </header>
  <div class="static-when">{groups}</div>
  {explore_html()}
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>
  {_WHEN_SCRIPT}"""
    return page(
        f"{seo_title} — {display_name()}",
        lead,
        canonical,
        DEFAULT_OG_IMAGE,
        body,
        json_ld=listing_json_ld(seo_title, canonical, lead, shown, []),
        css_prefix="..",
        body_class="static-page static-listing",
        map_href=home_view_url(None, on_map=True),
    )


def not_found_page() -> str:
    """GitHub Pages serves this for any missing path, so its links and
    assets are absolute. Most misses are a listing that was cancelled or
    renamed: say so, and offer the ways back in."""
    practices = "".join(
        f'<a class="static-chip" href="{practice_url(slug_)}">'
        f'{esc(spec.get("chip") or spec["label"])}</a>'
        for slug_, spec, matched in SITE_PRACTICES
        if matched and not spec.get("parent")
    )
    body = f"""
  <header class="static-list-head">
    <h1 class="static-title">That page isn't here</h1>
    <div class="static-intro">
      <p class="static-lead">If it was an event, it may have been cancelled,
      renamed or moved to a new date — its organiser's other dates are on
      their page, and everything on in London is on the
      <a href="{BASE_URL}/">front page</a>.</p>
    </div>
  </header>
  {f'<p class="static-chips">{practices}</p>' if practices else ""}
  <p class="static-back"><a href="{BASE_URL}/">← everything on {esc(display_name())}</a></p>"""
    return page(
        f"Not found — {display_name()}",
        "That page isn't here any more.",
        f"{BASE_URL}/",
        DEFAULT_OG_IMAGE,
        body,
        css_prefix=BASE_URL,
        head_extra='\n  <meta name="robots" content="noindex">',
    )


def about_url() -> str:
    return f"{BASE_URL}/about/"


def about_page(paragraphs: list[str]) -> str:
    canonical = about_url()
    lead_html = "".join(f'<p class="static-lead">{esc(p)}</p>' for p in paragraphs)
    meta_desc = paragraphs[0]
    if len(meta_desc) > 160:
        meta_desc = meta_desc[:157].rsplit(" ", 1)[0] + "…"

    body = f"""
  <nav class="static-crumbs" aria-label="breadcrumb">
    <a href="{BASE_URL}/">{esc(display_name())}</a>
    <span aria-hidden="true">/</span>
    <span>about</span>
  </nav>
  <header class="static-list-head">
    <p class="static-kicker">about</p>
    <h1 class="static-title">about {esc(display_name())}</h1>
    <div class="static-intro">
      {lead_html}
    </div>
  </header>
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>"""
    return page(
        f"about — {display_name()}",
        meta_desc,
        canonical,
        DEFAULT_OG_IMAGE,
        body,
        css_prefix="..",
    )


def privacy_url() -> str:
    return f"{BASE_URL}/privacy/"


def privacy_page(paragraphs: list[str]) -> str:
    canonical = privacy_url()
    lead_html = "".join(f'<p class="static-lead">{esc(p)}</p>' for p in paragraphs)
    meta_desc = paragraphs[0]
    if len(meta_desc) > 160:
        meta_desc = meta_desc[:157].rsplit(" ", 1)[0] + "…"

    body = f"""
  <nav class="static-crumbs" aria-label="breadcrumb">
    <a href="{BASE_URL}/">{esc(display_name())}</a>
    <span aria-hidden="true">/</span>
    <span>privacy</span>
  </nav>
  <header class="static-list-head">
    <p class="static-kicker">privacy</p>
    <h1 class="static-title">privacy at {esc(display_name())}</h1>
    <div class="static-intro">
      {lead_html}
    </div>
  </header>
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>"""
    return page(
        f"privacy — {display_name()}",
        meta_desc,
        canonical,
        DEFAULT_OG_IMAGE,
        body,
        css_prefix="..",
    )


def join_community_url() -> str:
    return f"{BASE_URL}/join-community/"


# Kept out of the f-string below because it is dense with JS braces. The three
# __PLACEHOLDERS__ are substituted, not interpolated.
JOIN_SCRIPT = """
(function () {
  var form = document.getElementById("join-form");
  var button = document.getElementById("join-submit");
  var status = document.getElementById("join-status");
  var result = document.getElementById("join-result");
  var loadedAt = Date.now();

  // Dwell gate: a real person needs a moment to read the page anyway, and
  // instant submits are the signature of a script.
  button.disabled = true;
  setTimeout(function () { button.disabled = false; }, __DWELL_MS__);

  var MESSAGES = {
    rate: "too many tries. wait a minute, then try again.",
    passphrase: "that's not the word \\u2014 check where you found this link.",
    token: "the human check didn't pass. try again.",
    stale: "the human check expired. try again.",
    fast: "that was too quick. give the page a moment, then try again.",
    config: "the invite isn't set up right now. try again later.",
  };

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var token = (form.elements["cf-turnstile-response"] || {}).value || "";
    if (!token) {
      status.textContent = "still checking you're human \\u2014 one moment, then try again.";
      return;
    }
    button.disabled = true;
    status.textContent = "checking\\u2026";

    fetch("__WORKER_URL__", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        token: token,
        passphrase: (form.elements.passphrase || {}).value || "",
        website: (form.elements.website || {}).value || "",
        dwellMs: Date.now() - loadedAt,
      }),
    })
      .then(function (res) {
        return res.json().then(function (data) { return { ok: res.ok, data: data }; });
      })
      .then(function (r) {
        if (!r.ok || !r.data.url) {
          throw new Error(r.data.error || "failed");
        }
        reveal(r.data.url);
      })
      .catch(function (err) {
        status.textContent =
          MESSAGES[err.message] || "something went wrong. reload the page and try again.";
        button.disabled = false;
        // Turnstile tokens are single-use, so the widget needs a fresh one.
        if (window.turnstile) window.turnstile.reset();
      });
  });

  function reveal(url) {
    form.hidden = true;
    status.textContent = "";
    var link = document.createElement("a");
    link.className = "join-invite";
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = url;
    var copy = document.createElement("button");
    copy.type = "button";
    copy.className = "join-copy";
    copy.textContent = "copy link";
    copy.addEventListener("click", function () {
      navigator.clipboard.writeText(url).then(function () {
        copy.textContent = "copied";
        setTimeout(function () { copy.textContent = "copy link"; }, 2000);
      });
    });
    result.appendChild(link);
    result.appendChild(copy);
    result.hidden = false;
    link.focus();
  }
})();
"""


def join_community_page(join: dict) -> str:
    """Turnstile-gated page revealing the WhatsApp invite. The invite URL is
    never here — the Worker at join["workerUrl"] holds it and only returns it
    once the token, passphrase, honeypot and rate limit all pass."""
    canonical = join_community_url()
    hint = join.get("passphraseHint") or ""
    passphrase_field = (
        f"""
      <label class="join-label" for="join-passphrase">passphrase</label>
      <p class="join-hint">{esc(hint)}</p>
      <input class="join-input" id="join-passphrase" name="passphrase" type="text"
             autocomplete="off" autocapitalize="none" spellcheck="false" required>"""
        if hint
        else ""
    )

    script = (
        JOIN_SCRIPT.replace("__WORKER_URL__", join["workerUrl"].rstrip("/"))
        .replace("__DWELL_MS__", "2500")
        .replace("</", "<\\/")  # never let a value break out of <script>
    )

    body = f"""
  <header class="static-list-head">
    <p class="static-kicker">community</p>
    <h1 class="static-title">join the {esc(display_name())} community</h1>
    <div class="static-intro">
      <p class="static-lead">A WhatsApp group for people who come to these
      gatherings — plans, questions, and the odd last-minute ticket.</p>
      <p class="static-lead">Confirm you're human and the invite link appears.</p>
    </div>
  </header>
  <form class="join-form" id="join-form" novalidate>
    {passphrase_field}
    <div class="join-honeypot" aria-hidden="true">
      <label for="join-website">leave this empty</label>
      <input id="join-website" name="website" type="text" tabindex="-1" autocomplete="off">
    </div>
    <div class="cf-turnstile" data-sitekey="{esc(join["turnstileSiteKey"])}"
         data-theme="auto" data-appearance="always"></div>
    <button class="join-submit" id="join-submit" type="submit">show me the invite</button>
    <p class="join-status" id="join-status" role="status" aria-live="polite"></p>
  </form>
  <div class="join-result" id="join-result" hidden></div>
  <p class="static-back">
    <a href="{BASE_URL}/">← all of {esc(display_name())}</a>
  </p>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
  <script>{script}</script>"""

    return page(
        f"join the community — {display_name()}",
        f"Join the {display_name()} WhatsApp community.",
        canonical,
        None,
        body,
        css_prefix="..",
        head_extra='<meta name="robots" content="noindex, nofollow">',
    )


# Practice pages by London area (/p/<practice>/<area>/): the searches
# that reach the practice pages name a part of town ("sound bath east
# london", "breathwork shoreditch"). An area gets a page once it has had
# this many listings, upcoming plus the last PRACTICE_PAST_DAYS.
AREAS = ("north", "east", "south", "west", "central")
AREA_MIN_EVENTS = 4
# slug -> the areas that practice has a page for, filled in by build()
PRACTICE_AREAS: dict[str, list[str]] = {}

# listings needed before the at-a-glance numbers mean anything
GLANCE_MIN = 5
# a ticket above this is a placeholder (Momence's "book via the link" £999)
PRICE_CEILING = 500


def area_url(slug_: str, area: str) -> str:
    return f"{practice_url(slug_)}{area}/"


def area_title(spec: dict, area: str) -> str:
    title = spec["seo_title"]
    if " in London" in title:
        return title.replace(" in London", f" in {area} London")
    return f"{spec['label']} in {area} London"


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _top(counter: dict, n: int, at_least: int = 2) -> list:
    return [k for k, c in sorted(counter.items(), key=lambda kv: -kv[1]) if c >= at_least][:n]


def practice_glance(
    slug_: str, spec: dict, pool: list[dict], area: str | None = None
) -> tuple[str, tuple[str, str] | None]:
    """What the listings themselves say about a practice in London — the
    typical ticket, when and where it happens, the rooms and hosts that
    run it most — from upcoming listings plus the last three months'.
    Returns the section and an FAQ entry on price, built from the same
    numbers so the two never disagree."""
    if len(pool) < GLANCE_MIN:
        return "", None
    label = spec["label"]
    where = f"{area} London" if area else "London"
    rows: list[tuple[str, str]] = []
    price_faq = None

    paid = sorted(
        e["price_min"]
        for e in pool
        if not e.get("is_free") and e.get("price_min")
        and 0 < e["price_min"] < PRICE_CEILING
    )
    free = sum(1 for e in pool if e.get("is_free"))
    if len(paid) >= GLANCE_MIN:
        typical = round(statistics.median(paid))
        low, _, high = (round(q) for q in statistics.quantiles(paid, n=4))
        text = f"£{typical}" + (f", most £{low}–£{high}" if low != high else "")
        if free:
            text += f" · {free} of {len(pool)} free"
        rows.append(("Typical ticket", esc(text)))
        answer = (
            f"Across the {len(pool)} {label} listings in {where} we've seen "
            f"over the last three months and coming up, the typical ticket "
            f"is £{typical}"
            + (f", and most cost between £{low} and £{high}." if low != high else ".")
        )
        if free:
            answer += (
                f" {free} {'was' if free == 1 else 'were'} free or "
                "donation-based."
            )
        question = spec.get("price_q") or f"How much does {label} cost in London?"
        if area:
            question = question.replace("in London", f"in {area} London")
        price_faq = (question, answer)

    days = {}
    parts = {}
    for e in pool:
        start = _start_london(e)
        day = start.strftime("%A") + "s"
        days[day] = days.get(day, 0) + 1
        if not e.get("is_all_day"):
            part = (
                "mornings" if start.hour < 12
                else "afternoons" if start.hour < 17
                else "evenings"
            )
            parts[part] = parts.get(part, 0) + 1
    common_days = [d for d in _top(days, 2) if days[d] >= len(pool) / 5]
    if common_days:
        text = f"Mostly {_joined(common_days)}"
        part = _top(parts, 1)
        if part and parts[part[0]] >= len(pool) / 2:
            text += f", usually {part[0]}"
        rows.append(("When", esc(text)))

    if not area:
        areas = {}
        for e in pool:
            if e.get("area") in AREAS:
                areas[e["area"]] = areas.get(e["area"], 0) + 1
        links = []
        for a in _top(areas, 5, at_least=1):
            text = f"{a} ({areas[a]})"
            links.append(
                f'<a href="{area_url(slug_, a)}">{esc(text)}</a>'
                if a in PRACTICE_AREAS.get(slug_, [])
                else esc(text)
            )
        if links:
            rows.append(("Where", " · ".join(links)))

    # "The Skylight Centre" and "Skylight Centre" are one room
    venues: dict[str, int] = {}
    names: dict[str, str] = {}
    for e in pool:
        v = clean_venue(e)
        if v:
            key = venue_key(v)
            venues[key] = venues.get(key, 0) + 1
            names.setdefault(key, v)
    rooms = [names[k] for k in _top(venues, 5)]
    if rooms:
        rows.append(("Regular rooms", esc(" · ".join(rooms))))

    hosts = {}
    labels = {}
    for e in pool:
        key = organizer_key(e)
        if key:
            hosts[key[0]] = hosts.get(key[0], 0) + 1
            labels[key[0]] = key[1]
    top_hosts = _top(hosts, 5)
    if top_hosts:
        rows.append((
            "Hosts who run most",
            " · ".join(
                f'<a href="{organizer_page_url(h)}">{esc(labels[h])}</a>'
                if h in SITE_ORGANIZERS
                else esc(labels[h])
                for h in top_hosts
            ),
        ))

    if not rows:
        return "", price_faq
    facts = "".join(
        f'<div class="static-fact"><dt>{dt}</dt><dd>{dd}</dd></div>'
        for dt, dd in rows
    )
    return (
        '<section class="static-glance">'
        f"<h2>At a glance: {esc(area_title(spec, area) if area else spec['seo_title'])}</h2>"
        f'<dl class="static-facts">{facts}</dl>'
        f'<p class="static-glance-note">From {len(pool)} listings: '
        "what's coming up and the last three months.</p></section>"
    ), price_faq


def search_placeholder(label: str) -> str:
    """"search cacao…" says the box searches within this page's subject;
    a label too long for a phone-width box falls back to plain."""
    return f"search {label}…" if len(label) <= 10 else "search within…"


def where_chips(link_for, current: str | None = None, map_href: str | None = None) -> list[str]:
    """The filters panel's location row: an area chip wherever there's
    somewhere to go, and the map."""
    chips = [
        _nav_chip(a, link_for(a), a == current) for a in AREAS if link_for(a)
    ]
    if map_href and map_available():
        chips.append(
            f'<a class="token token-map" href="{esc(map_href)}">{PIN_SVG}map</a>'
        )
    return chips


def practice_view(slug_: str, area: str | None = None) -> dict | None:
    """The main page's filter for this practice, if the main page can show
    it — a page kept up by past listings alone has no chip there."""
    if not PRACTICE_EVENTS.get(slug_):
        return None
    return {"modality": slug_, **({"area": area} if area else {})}


def practice_where(slug_: str, current: str | None = None) -> list[str]:
    def link(a: str) -> str | None:
        if a in PRACTICE_AREAS.get(slug_, []):
            return area_url(slug_, a)
        if any(e.get("area") == a for e in PRACTICE_EVENTS.get(slug_, [])):
            return home_view_url(practice_view(slug_, a))
        return None

    view = practice_view(slug_)
    return where_chips(link, current, home_view_url(view, on_map=True) if view else None)


def practice_page_extras(slug_: str, spec: dict, matched: list[dict]) -> dict:
    """The live-data parts of a practice page, as listing_page kwargs."""
    past = PRACTICE_PAST.get(slug_, [])
    glance, price_faq = practice_glance(slug_, spec, matched + past)
    areas = PRACTICE_AREAS.get(slug_, [])
    by_area = (
        '<p class="static-back static-areas">By area: '
        + " · ".join(
            f'<a href="{area_url(slug_, a)}">{esc(area_title(spec, a))}</a>'
            for a in areas
        )
        + "</p>"
        if areas
        else ""
    )
    return {
        "head_html": related_practices_html(slug_, len(within_window(matched, 30))),
        "extra_html": glance
        + by_area
        + past_events_html(past, "Recently in London"),
        "faq_items": list(spec.get("faq") or []) + ([price_faq] if price_faq else []),
        "context": practice_view(slug_),
        "where": practice_where(slug_),
        "placeholder": search_placeholder(spec.get("chip") or spec["label"])
        if practice_view(slug_)
        else "search…",
    }


def area_page(
    slug_: str, spec: dict, area: str, upcoming: list[dict], past: list[dict]
) -> str:
    """One practice in one part of London: its own intro, numbers and
    rooms (with addresses — the neighbourhood names people search for),
    not a copy of the London-wide page."""
    label = spec["label"]
    month = within_window(upcoming, 30)
    venues: dict[str, dict] = {}
    seen_rooms: set[str] = set()
    for e in upcoming + past:
        v = clean_venue(e)
        if v and venue_key(v) not in seen_rooms:
            seen_rooms.add(venue_key(v))
            venues[v] = e
    names = list(venues)[:3]
    intro = [
        f"Every {label} listing we can find in {area} London"
        + (f" — at {_joined(names)} and more" if len(names) > 1 else "")
        + ", with times, prices and tickets.",
        (
            f"Nothing on in {area} London in the next 30 days right now — "
            "recent ones are listed below, and new dates appear here as soon "
            "as they're announced."
            if not month
            else f"{len(month)} on in the next 30 days, refreshed several times a day."
            if len(month) != 1
            else "One on in the next 30 days — time, venue and tickets below."
        ),
    ]
    glance, price_faq = practice_glance(slug_, spec, upcoming + past, area)
    rooms = "".join(
        f"<li><strong>{esc(v)}</strong>"
        + (f" — {esc(e['address'])}" if e.get("address") else "")
        + "</li>"
        for v, e in list(venues.items())[:12]
    )
    rooms_html = (
        f'<section class="static-past static-rooms"><h2>Where in {esc(area)} London</h2>'
        f"<ul>{rooms}</ul></section>"
        if rooms
        else ""
    )
    others = [a for a in PRACTICE_AREAS.get(slug_, []) if a != area]
    back = (
        f'<p class="static-back"><a href="{practice_url(slug_)}">'
        f"← {esc(spec['seo_title'])}: what to expect, and every area</a></p>"
    )
    if others:
        back += (
            '<p class="static-back static-areas">Elsewhere: '
            + " · ".join(
                f'<a href="{area_url(slug_, a)}">{esc(a)} London</a>' for a in others
            )
            + "</p>"
        )
    return listing_page(
        slug_, label, area_title(spec, area), area_url(slug_, area), month,
        kind="area", days=30, css_prefix="../../..", intro=intro,
        ahead=beyond_window(upcoming, 30),
        extra_html=glance + rooms_html
        + past_events_html(past, f"Recently in {area} London") + back,
        faq_items=[price_faq] if price_faq else [],
        nav=(slug_, "practice"),
        context=practice_view(slug_, area),
        where=practice_where(slug_, area),
        placeholder=search_placeholder(spec.get("chip") or label)
        if practice_view(slug_)
        else "search…",
    )


def write_listing(
    outdir: Path,
    parts: tuple[str, ...],
    canonical: str,
    key: str,
    label: str,
    seo_title: str,
    events: list[dict],
    kind: str,
    urls: list[str],
    **page_kw,
) -> None:
    """A listing at both ranges: the lead page at `parts`, the other one
    a directory below it.

    Which range leads depends on the family (see LEAD_WINDOW), except
    that a week too thin to land on gives way to the month. The second
    page is the
    same listing over a different range, so its canonical points at the
    lead and it stays out of the sitemap — one page competes for the
    query, the other is just a wider look.
    """
    if kind in ("organizer", "venue"):
        # one host's (or room's) diary is short: everything ahead on one
        # page, and a quiet one still has its page
        write_index(
            outdir.joinpath(*parts),
            listing_page(
                key, label, seo_title, canonical, events, kind=kind, **page_kw
            ),
        )
        urls.append(canonical)
        return

    week, month = (within_window(events, n) for n in LISTING_WINDOWS)
    page_kw.setdefault("ahead", beyond_window(events, 30))
    lead_days = LEAD_WINDOW.get(kind, 7)
    if lead_days == 7 and len(week) < MIN_WEEK_EVENTS:
        lead_days = 30
    other_days = 30 if lead_days == 7 else 7
    by_days = {7: week, 30: month}
    other_url = f"{canonical}{other_days}-days/"

    write_index(
        outdir.joinpath(*parts),
        listing_page(
            key, label, seo_title, canonical, by_days[lead_days],
            kind=kind, days=lead_days, other_url=other_url, strip_events=month,
            **page_kw,
        ),
    )
    urls.append(canonical)

    write_index(
        outdir.joinpath(*parts, f"{other_days}-days"),
        listing_page(
            key, label, seo_title, canonical, by_days[other_days],
            kind=kind, days=other_days, other_url=canonical,
            css_prefix="../../..", strip_events=month, **page_kw,
        ),
    )


def build(outdir: Path) -> None:
    global _EVENT_SLUGS, DEFAULT_OG_IMAGE
    events = [e for e in fetch_events() if site_match(e)]
    print(f"Building {SITE['name']} with {len(events)} events")

    if outdir.exists():
        shutil.rmtree(outdir)
    shutil.copytree(ROOT / "web", outdir)
    if SITE["overlay"]:
        shutil.copytree(SITE["overlay"], outdir, dirs_exist_ok=True)
    global THEME_BOOT, THEME_COLOR
    THEME_BOOT = load_theme_boot(outdir)
    shell_meta = re.search(
        r'<meta name="theme-color" content="([^"]+)"',
        (outdir / "index.html").read_text(),
    )
    THEME_COLOR = shell_meta.group(1) if shell_meta else ""
    inject_startup_images(outdir)
    inject_theme_boot(outdir)
    inject_robots_meta(outdir)
    inline_shell_css(outdir)

    global SITE_PRACTICES, SITE_ORGANIZERS, SITE_EVENTS
    # a year back: host pages' history, past event pages, and what keeps a
    # practice page standing through a quiet spell
    past_events = [e for e in fetch_past_events() if site_match(e)]
    SITE_PRACTICES = site_practices(events, past_events)
    SITE_EVENTS = events
    # before the event pages: their filters key carries these
    TOPIC_COUNTS.clear()
    for e in events:
        for t in e.get("topics") or []:
            TOPIC_COUNTS[t] = TOPIC_COUNTS.get(t, 0) + 1
    # before the event pages: their Host line links to these
    SITE_ORGANIZERS = (
        site_organizers(events, past_events)
        if (SITE_JSON.get("features") or {}).get("organizerPages")
        else {}
    )
    # likewise their Where line links to the room's page
    SITE_VENUES.clear()
    VENUE_EVENTS.clear()
    if (SITE_JSON.get("features") or {}).get("venuePages"):
        SITE_VENUES.update(site_venues(events, past_events))
        for slug_, v in SITE_VENUES.items():
            for e in v["upcoming"] + v["past"]:
                VENUE_EVENTS[(e["source"], e["source_id"])] = slug_

    DEFAULT_OG_IMAGE = (
        f"{BASE_URL}/og-image.jpg" if (outdir / "og-image.jpg").exists() else None
    )

    _EVENT_SLUGS = assign_event_slugs(events, past_events)
    index_listings(events, past_events)
    build_explore_tiles(events)
    # the static top-picks row links to event pages, so it waits for slugs
    inject_spotlight(outdir, events)
    # Which listings have a page of their own, for the main page's cards:
    # one added since this build has none yet, and links out instead.
    (outdir / "event-pages.json").write_text(
        json.dumps({f"{e['source']}:{e['source_id']}": event_slug(e) for e in events})
    )
    urls = [f"{BASE_URL}/"]

    about_paras = ABOUT_CONTENT.get(SITE["name"])
    if about_paras:
        write_index(outdir / "about", about_page(about_paras))
        urls.append(about_url())

    privacy_paras = PRIVACY_CONTENT.get(SITE["name"])
    if privacy_paras:
        write_index(outdir / "privacy", privacy_page(privacy_paras))
        urls.append(privacy_url())

    # Unlisted by design: reachable only by typing the URL. No sitemap entry,
    # no .html twin, no link from any other page. Off until SITE.joinCommunity
    # names a deployed Worker.
    join = SITE_JSON.get("joinCommunity") or {}
    has_join = bool(join.get("workerUrl") and join.get("turnstileSiteKey"))
    if has_join:
        write_index(outdir / "join-community", join_community_page(join))

    for event in events:
        slug = event_slug(event)
        canonical = event_url(event)
        write_index(outdir / "e" / slug, event_page(event))
        # legacy source-id paths keep working for old sitemaps / shares
        legacy = legacy_event_id(event)
        if legacy != slug:
            write_html_redirect(outdir / "e" / f"{legacy}.html", canonical)
            write_index(outdir / "e" / legacy, moved_page(canonical))
        write_html_redirect(outdir / "e" / f"{slug}.html", canonical)
        urls.append(canonical)

    # Past events keep their pages: searches for a name keep landing after
    # the date, and a 404 throws away what the page had earned. Left out
    # of the sitemap — Google already knows the ones that matter.
    for event in past_events:
        slug = event_slug(event)
        write_index(outdir / "e" / slug, event_page(event, past=True))
        write_html_redirect(outdir / "e" / f"{slug}.html", event_url(event))

    # A series' later dates live at <slug>-<date> until the date before
    # them passes and the bare slug rolls on to them: the dated URL Google
    # found keeps working, pointing at wherever that date lives now.
    taken = set(_EVENT_SLUGS.values())
    for event in [*events, *past_events]:
        if not event.get("start_at"):
            continue
        dated = f"{slugify_title(event.get('title') or 'event')}-{event['start_at'][:10]}"
        if dated not in taken:
            taken.add(dated)
            write_index(outdir / "e" / dated, moved_page(event_url(event)))

    # Listings gone since Google found them: a merged duplicate points at
    # the listing it was merged into, a withdrawn one at its host's page.
    # Anything else falls through to 404.html.
    by_key = {f"{e['source']}:{e['source_id']}": e for e in [*events, *past_events]}
    for row in fetch_gone_events():
        target = by_key.get(row.get("duplicate_of") or "")
        if target:
            dest = event_url(target)
        elif site_match(row) and organizer_page_for(row):
            dest = organizer_page_url(organizer_page_for(row)[0])
        else:
            continue
        base = slugify_title(row.get("title") or "")
        for slug in (base, f"{base}-{(row.get('start_at') or '')[:10]}"):
            if base and slug not in taken:
                taken.add(slug)
                write_index(outdir / "e" / slug, moved_page(dest))

    # category pages only make sense when the site spans all categories;
    # on a filtered site one of them would just mirror the homepage
    if not SITE_JSON.get("filter"):
        for key, (label, seo_title) in CATEGORIES.items():
            cat_events = [e for e in events if e.get("category") == key]
            if not cat_events:
                continue
            canonical = category_url(key)
            write_listing(
                outdir, ("c", key), canonical,
                key, label, seo_title, cat_events, "category", urls,
            )
            write_html_redirect(outdir / "c" / f"{key}.html", canonical)

    site_topics = SITE_JSON.get("topics")
    for key, (slug_, seo_title) in TOPICS.items():
        if site_topics is not None and key not in site_topics:
            continue
        topic_events = [e for e in events if key in (e.get("topics") or [])]
        if not topic_events:
            continue
        canonical = topic_url(slug_)
        month = within_window(topic_events, 30)
        view = {"topic": slug_}
        since = datetime.now(timezone.utc) - timedelta(days=PRACTICE_PAST_DAYS)
        recent = [
            e for e in past_events
            if key in (e.get("topics") or []) and _parse_ts(e["start_at"]) >= since
        ]
        write_listing(
            outdir, ("t", slug_), canonical,
            key, key, seo_title, topic_events, "topic", urls,
            context=view,
            where=where_chips(
                lambda a, view=view, month=month: home_view_url({**view, "area": a})
                if any(e.get("area") == a for e in month)
                else None,
                map_href=home_view_url(view, on_map=True),
            ),
            placeholder=search_placeholder(key),
            past_html=past_events_html(recent, "Recently in London")
            if len(month) < SPARSE_BELOW
            else "",
        )
        write_html_redirect(outdir / "t" / f"{slug_}.html", canonical)

    PRACTICE_AREAS.clear()
    for slug_, spec, matched in SITE_PRACTICES:
        pool = matched + PRACTICE_PAST.get(slug_, [])
        PRACTICE_AREAS[slug_] = [
            a for a in AREAS
            if sum(1 for e in pool if e.get("area") == a) >= AREA_MIN_EVENTS
        ]
    for slug_, spec, matched in SITE_PRACTICES:
        canonical = practice_url(slug_)
        write_listing(
            outdir, ("p", slug_), canonical,
            slug_, spec["label"], spec["seo_title"], matched, "practice", urls,
            **practice_page_extras(slug_, spec, matched),
        )
        write_html_redirect(outdir / "p" / f"{slug_}.html", canonical)
        for area in PRACTICE_AREAS[slug_]:
            write_index(
                outdir / "p" / slug_ / area,
                area_page(
                    slug_, spec, area,
                    [e for e in matched if e.get("area") == area],
                    [e for e in PRACTICE_PAST.get(slug_, []) if e.get("area") == area],
                ),
            )
            urls.append(area_url(slug_, area))

    for slug_, org in SITE_ORGANIZERS.items():
        write_listing(
            outdir, ("o", slug_), organizer_page_url(slug_),
            slug_, org["label"], f"{org['label']} events in London",
            org["upcoming"], "organizer", urls,
        )
    if SITE_ORGANIZERS:
        write_index(outdir / "o", organizers_index_page())
        urls.append(organizers_index_url())

    for slug_, v in SITE_VENUES.items():
        write_listing(
            outdir, ("v", slug_), venue_page_url(slug_),
            slug_, v["label"], venue_title(v), v["upcoming"], "venue", urls,
            **venue_page_extras(slug_, v),
        )
        for alias in v["aliases"]:
            write_index(outdir / "v" / alias, moved_page(venue_page_url(slug_)))
    if SITE_VENUES:
        write_index(outdir / "v", venues_index_page())
        urls.append(venues_index_url())

    if _when_pages_on():
        for which in WHEN_PAGES:
            write_index(outdir / which, when_page(which, events))
            urls.append(f"{BASE_URL}/{which}/")

    # The home page's practice chips read this: same definitions, same
    # per-site set, so the app filters by exactly what the pages list.
    (outdir / "practices.json").write_text(
        json.dumps(
            [
                {
                    "slug": slug_,
                    "label": spec.get("chip") or spec["label"],
                    "title": spec["seo_title"],
                    "url": practice_url(slug_),
                    "terms": spec["terms"],
                    "deep": spec.get("deep") or [],
                    "exclude": spec.get("exclude") or [],
                    "parent": spec.get("parent"),
                    "tags": spec.get("tags", True),
                }
                for slug_, spec, matched in SITE_PRACTICES
                # a page kept up by past listings alone has no chip to light
                if matched
            ]
        )
    )
    # the explore tiles' line icons, for the app's practice rail and filter
    # chips — keyed like EXPLORE_ICONS, by practice slug or topic name
    site_topics = SITE_JSON.get("topics")
    (outdir / "icons.json").write_text(
        json.dumps({
            k: v for k, v in EXPLORE_ICONS.items()
            if k in PRACTICES or site_topics is None or k in site_topics
        } | {"_": _SPARK_ICON})
    )

    (outdir / "404.html").write_text(not_found_page())

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
    if SITE.get("noindex"):
        # No sitemap to invite anyone in. Search engines stay allowed so
        # they can read the noindex meta and drop what they already have;
        # the AI crawlers, which index rather than rank, are turned away
        # outright.
        (outdir / "robots.txt").write_text(
            "User-agent: *\nAllow: /\n\n"
            + "".join(
                f"User-agent: {bot}\nDisallow: /\n\n"
                for bot in AI_CRAWLERS
            )
        )
    else:
        (outdir / "sitemap.xml").write_text(sitemap)
        disallow = "Disallow: /join-community/\n" if has_join else ""
        (outdir / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\n{disallow}Sitemap: {BASE_URL}/sitemap.xml\n"
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
