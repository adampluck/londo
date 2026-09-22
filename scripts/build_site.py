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
# The prose is the point: it answers the query in its first sentence so
# a search engine — or a model quoting one paragraph — has something to
# lift. Keep it concrete and keep it honest about prices and safety.
PRACTICES = {
    "cacao-ceremony": {
        "label": "cacao ceremony",
        "chip": "cacao",
        "seo_title": "Cacao ceremonies in London",
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
                "How much does a cacao ceremony cost in London?",
                "Most London cacao ceremonies cost between £10 and £30, with the "
                "typical ticket around £17. A few community evenings are free or "
                "donation-based; day-long retreats and full-moon specials run higher.",
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
    "sound-bath": {
        "label": "sound bath",
        "seo_title": "Sound baths & gong baths in London",
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
    "somatics": {
        "label": "somatic practice",
        "chip": "somatic",
        "seo_title": "Somatic workshops & embodiment in London",
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


def practice_match(event: dict, spec: dict) -> bool:
    """Whether this listing belongs on a practice page.

    Same two tiers as the site filter's exclude terms: the safe fields
    (title, organizer, tags) take any of the practice's terms, while the
    description — long prose, where a bare substring catches passing
    mentions — only takes the phrases listed as deep.
    """
    hay = " ".join(
        p
        for p in (
            event.get("title") or "",
            event.get("organizer_name") or "",
            " ".join(event.get("tags") or []),
        )
        if p
    ).lower()
    if _matches(hay, spec.get("exclude") or []):
        return False
    if _matches(hay, spec["terms"]):
        return True
    deep = spec.get("deep") or []
    return bool(deep) and _matches((event.get("description") or "").lower(), deep)


def site_practices(events: list[dict]) -> list[tuple[str, dict, list[dict]]]:
    """Each practice this site has enough listings for, with its events."""
    out = []
    for slug_, spec in PRACTICES.items():
        matched = [e for e in events if practice_match(e, spec)]
        if len(matched) >= MIN_PRACTICE_EVENTS:
            out.append((slug_, spec, matched))
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


def legacy_event_id(event: dict) -> str:
    """Previous /e/<source>-<id>/ path — kept as a redirect target only."""
    sid = re.sub(r"[^A-Za-z0-9_-]", "", event["source_id"])[:48]
    return f"{event['source']}-{sid}"


def assign_event_slugs(events: list[dict]) -> dict[tuple[str, str], str]:
    """Prefer bare title slug; on collision append date, then a short unique tail.

    Listings without a ticket page (chat-shared flyers) pick first: the
    SPA links them to their own page by bare title slug (eventHref() in
    web/app.js), so that slug has to be theirs."""
    used: set[str] = set()
    mapping: dict[tuple[str, str], str] = {}
    for event in sorted(events, key=lambda e: bool(e.get("source_url"))):
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
    return (
        "https://wsrv.nl/?url="
        + urllib.parse.quote(url, safe="")
        + f"&w={width}&output=webp&q=75"
    )


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


def event_card(event: dict) -> str:
    """The SPA's card (web/app.js card()) as static markup, linking to
    the event's own page here rather than out to the ticket page."""
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
        f"{esc(fmt_time_range(event))}</p>",
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
        blurb = " ".join(event["description"].split())[:220]
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


def date_strip_html(events: list[dict], days: int, other_url: str | None) -> str:
    """The main page's date strip, as anchors onto this page's day
    groups. A day this topic has nothing on is shown spent rather than
    hidden, so the shape of the month stays readable."""
    have = {_start_london(e).date() for e in events}
    today = datetime.now(LONDON).date()
    ticks = []
    for i in range(days):
        day = today + timedelta(days=i)
        # the date is the sub-line throughout: over a week the weekday
        # alone under "today"/"tmrw" left people guessing the date
        if i == 0:
            main = "today"
        elif i == 1:
            main = "tmrw"
        else:
            main = day.strftime("%a").lower()
        sub = day.strftime("%-d %b").lower() if days <= 7 else day.strftime("%-d")
        cell = f"{main}<small>{sub}</small>"
        if day in have:
            ticks.append(f'<a class="tick" href="#d-{day.isoformat()}">{cell}</a>')
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
    return (
        '<div class="static-ticker"><div class="ticker-shell">'
        f'<div class="ticker">{"".join(ticks)}</div>'
        f'<div class="ticker-fixed">{"".join(ranges)}</div>'
        "</div></div>"
    )


def day_groups(events: list[dict]) -> str:
    """Events as the SPA's day-grouped card grids, each day an anchor
    the date strip above can jump to."""
    by_day: dict[str, list[dict]] = {}
    for e in events:
        by_day.setdefault(_start_london(e).date().isoformat(), []).append(e)
    sections = []
    for key, day_events in by_day.items():
        day = _start_london(day_events[0]).strftime("%A %-d %B")
        count = (
            "one gathering"
            if len(day_events) == 1
            else f"{len(day_events)} gatherings"
        )
        cards = "".join(event_card(e) for e in day_events)
        sections.append(
            f'<section class="day-group" id="d-{key}"><h2 class="day-heading">'
            f'<span>{esc(day)}</span><span class="count">{count}</span></h2>'
            f'<div class="grid">{cards}</div></section>'
        )
    return "".join(sections)


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
    head_extra: str = "",
    body_class: str = "static-page",
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
  <header class="static-header">
    {theme_toggle_html()}<a class="static-brand" href="{BASE_URL}/">{site_wordmark(css_prefix)}</a>
    <p class="static-tagline">{esc(SITE["tagline"])}</p>
  </header>
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
    return f'<nav class="seo-nav" aria-label="topics">{"".join(parts)}</nav>'


# Upcoming events per topic, filled in by build() before listing pages
# are written, so their topic chips can carry the same counts the SPA's do.
TOPIC_COUNTS: dict[str, int] = {}

# The practices this site has the listings for, filled in by build()
# before any page is written: the footer links to them from everywhere.
SITE_PRACTICES: list[tuple[str, dict, list[dict]]] = []


def topic_nav_html(current: str | None) -> str:
    """The main page's topic chips as links: "anything" (home) then each
    of the site's topics, the current page lit. Static pages have no
    filter state, so this is the navigation."""
    site_topics = SITE_JSON.get("topics")
    keys = [k for k in TOPICS if site_topics is None or k in site_topics]
    parts = [f'<a class="token" href="{BASE_URL}/">anything</a>']
    for key in keys:
        n = TOPIC_COUNTS.get(key, 0)
        if not n:
            continue
        slug_, _ = TOPICS[key]
        lit = " lit" if key == current else ""
        current_attr = ' aria-current="page"' if key == current else ""
        parts.append(
            f'<a class="token{lit}" href="{topic_url(slug_)}"{current_attr}>'
            f'{esc(key)} <small class="token-count">{n}</small></a>'
        )
    return f'<nav class="static-topics" aria-label="topics">{"".join(parts)}</nav>'


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
    for slug_, spec, _ in SITE_PRACTICES:
        if practice_match(event, spec):
            chips.append(
                f'<a class="static-chip" href="{practice_url(slug_)}">'
                f'{esc(spec["label"])}</a>'
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
            "availability": "https://schema.org/InStock",
            **({"url": event["source_url"]} if event.get("source_url") else {}),
        }
    elif event.get("price_min") is not None:
        json_ld["offers"] = {
            "@type": "Offer",
            "price": str(event["price_min"]),
            "priceCurrency": "GBP",
            "availability": "https://schema.org/InStock",
            **({"url": event["source_url"]} if event.get("source_url") else {}),
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

    if event.get("source_url"):
        cta = f"""<p class="static-cta-wrap">
      <a class="static-cta" href="{esc(with_utm(event["source_url"]))}" rel="noopener"
         data-goatcounter-click="out/{esc(organizer_slug(org))}"
         data-goatcounter-title="{esc(event["title"])}">
        tickets &amp; details ↗
      </a>
    </p>"""
    else:
        # a flyer shared in the community chat, with no page of its own:
        # the details above are all there is
        cta = ('<p class="static-cta-wrap static-cta-note">'
               "shared in the community chat — no ticket page; "
               "details as posted above</p>")

    area = event.get("area")
    kicker_bits = ["in person", "London"]
    if area:
        kicker_bits.append(f"{area} London")
    if event.get("category") and not SITE_JSON.get("filter"):
        kicker_bits.append(event["category"])

    body = f"""
  <nav class="static-crumbs" aria-label="breadcrumb">
    <a href="{BASE_URL}/">{esc(display_name())}</a>
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
    {cta}
    <p class="static-back">
      <a href="{BASE_URL}/">← more in-person gatherings on {esc(display_name())}</a>
    </p>
  </article>"""
    return page(
        f"{event['title']} — {display_name()}",
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
    else:
        paras = list(TOPIC_INTROS.get(key) or ())
    if not paras:
        paras = [
            f"In-person {label} gatherings in London, collected on {display_name()} "
            f"so you can find the good rooms without scrolling forever."
        ]
    window = "the next seven days" if days == 7 else f"the next {days} days"
    freshness = (
        f"{count} on in {window} — times, venues and tickets, "
        f"refreshed several times a day."
        if count != 1
        else f"One on in {window} — times, venue and tickets below."
    )
    return [*paras, freshness]


def faq_html(faq: list[tuple[str, str]]) -> str:
    """The questions people actually type, answered in prose. Marked up
    as a definition list, and repeated as FAQPage JSON-LD."""
    items = "".join(
        f"<dt>{esc(q)}</dt><dd>{esc(a)}</dd>" for q, a in faq
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
) -> str:
    paras = listing_intro_paragraphs(key, kind, label, len(events), days)
    lead_html = "".join(f'<p class="static-lead">{esc(p)}</p>' for p in paras)
    # meta description: first paragraph, kept short
    meta_desc = paras[0]
    if len(meta_desc) > 160:
        meta_desc = meta_desc[:157].rsplit(" ", 1)[0] + "…"

    groups = day_groups(events[:200])
    strip = date_strip_html(events, days, other_url)
    spec = PRACTICES[key] if kind == "practice" else {}
    faq = faq_html(spec["faq"]) if spec.get("faq") else ""
    json_ld = (
        listing_json_ld(seo_title, canonical, meta_desc, events, spec.get("faq") or [])
        if kind == "practice"
        else None
    )

    body = f"""
  {topic_nav_html(key if kind == "topic" else None)}
  {strip}
  <header class="static-list-head">
    <p class="static-kicker">in person · London</p>
    <h1 class="static-title">{esc(seo_title)}</h1>
    <div class="static-intro">
      {lead_html}
    </div>
  </header>
  {groups}
  {faq}
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
    week, month = (within_window(events, n) for n in LISTING_WINDOWS)
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
            kind=kind, days=lead_days, other_url=other_url,
        ),
    )
    urls.append(canonical)

    write_index(
        outdir.joinpath(*parts, f"{other_days}-days"),
        listing_page(
            key, label, seo_title, canonical, by_days[other_days],
            kind=kind, days=other_days, other_url=canonical,
            css_prefix="../../..",
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

    global SITE_PRACTICES
    SITE_PRACTICES = site_practices(events)

    DEFAULT_OG_IMAGE = (
        f"{BASE_URL}/og-image.jpg" if (outdir / "og-image.jpg").exists() else None
    )

    _EVENT_SLUGS = assign_event_slugs(events)
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

    TOPIC_COUNTS.clear()
    for e in events:
        for t in e.get("topics") or []:
            TOPIC_COUNTS[t] = TOPIC_COUNTS.get(t, 0) + 1

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
        write_listing(
            outdir, ("t", slug_), canonical,
            key, key, seo_title, topic_events, "topic", urls,
        )
        write_html_redirect(outdir / "t" / f"{slug_}.html", canonical)

    for slug_, spec, matched in SITE_PRACTICES:
        canonical = practice_url(slug_)
        write_listing(
            outdir, ("p", slug_), canonical,
            slug_, spec["label"], spec["seo_title"], matched, "practice", urls,
        )
        write_html_redirect(outdir / "p" / f"{slug_}.html", canonical)

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
                }
                for slug_, spec, _ in SITE_PRACTICES
            ]
        )
    )

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
