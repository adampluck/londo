"""psyconnect's weekly email: one version per interest ("edition"), built
from the same events and filters as the site, handed to Kit as broadcasts.

Each subscriber carries exactly one `edition:<topic>` tag in Kit — the
Worker (workers/subscribe) sets it from the page they signed up on, and
`tidy_editions` below repairs anyone with none or several. Each edition's
email leads with its topic, then related gatherings; everyone without an
edition gets the general version. One tag per person is what stops anyone
getting two emails: Kit's broadcast filter takes a single all/any/none
group, so "general" is "none of the edition tags".

Modes:
    preview   write each edition's HTML to --out; never calls Kit
    draft     create Kit drafts to review and send by hand
    schedule  create Kit broadcasts scheduled for --send-at

Re-running is safe: broadcasts are named weekly:<date>:<edition> in their
description, and any draft or scheduled one with the same name is deleted
before it's created again.

Stdlib only, like build_site.py, so CI needs no installs:
    python3 scripts/weekly_email.py preview --out /tmp/weekly
    KIT_API_KEY=... python3 scripts/weekly_email.py draft
The key comes from the environment, or KIT_API_KEY= in the repo's .env.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402

KIT = "https://api.kit.com/v4"
SITE_ID = "psyconnect"

# Edition slug -> label. Priority order too: someone tagged with several
# topics but no edition (signed up before editions existed) gets the first.
EDITIONS = {
    "psychedelics": "Psychedelics",
    "consciousness": "Consciousness",
    "spirituality": "Ceremony & ritual",
    "connection": "Connection & intimacy",
    "healing": "Healing & wellbeing",
}
# What sits next to each topic in the "also this week" section
RELATED = {
    "psychedelics": ("consciousness", "healing"),
    "consciousness": ("spirituality", "psychedelics"),
    "spirituality": ("consciousness", "healing"),
    "connection": ("healing", "spirituality"),
    "healing": ("connection", "spirituality"),
}
TEMPLATE = bs.ROOT / "kit" / "email-template.html"
TEMPLATE_NAME = "PsyConnect"
LEAD_MAX = 6
ALSO_MAX = 6
ORG_MAX = 2  # per host, per email: one busy venue shouldn't fill it
SEND_WEEKDAY = 3  # Thursday: lands before the weekend's plans are made
SEND_HOUR = 9
WINDOW = timedelta(days=7)

# palette: psyconnect's light theme, as literal colours for email clients
CREAM, CARD, EDGE = "#f6efe4", "#fffcf5", "#e6d8c3"
INK, DIM, PLUM, OCHRE = "#3a2b20", "#6b5443", "#6d4160", "#8a5f14"
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


# ---------- data -----------------------------------------------------------

def setup_site() -> None:
    bs.SITE = bs.SITES[SITE_ID]
    bs.BASE_URL = bs.SITE["base_url"]
    bs.SITE_JSON = bs.read_site_block()


def topic_name(slug: str) -> str:
    return next(name for name, (s, _) in bs.TOPICS.items() if s == slug)


def topic_slugs(event: dict) -> set[str]:
    return {bs.TOPICS[t][0] for t in event.get("topics") or [] if t in bs.TOPICS}


def live_slugs() -> dict[str, str]:
    """Event pages the live site has right now: a listing scraped since the
    last deploy has none yet, and its link would 404."""
    url = f"{bs.BASE_URL}/event-pages.json"
    req = urllib.request.Request(url, headers={"User-Agent": "psyconnect-weekly"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def next_send(now: datetime) -> datetime:
    local = now.astimezone(bs.LONDON)
    days = (SEND_WEEKDAY - local.weekday()) % 7
    at = (local + timedelta(days=days)).replace(
        hour=SEND_HOUR, minute=0, second=0, microsecond=0
    )
    return at if at > local else at + timedelta(days=7)


def week_events(send_at: datetime) -> list[dict]:
    events = [e for e in bs.fetch_events() if bs.site_match(e)]
    end = send_at + WINDOW
    return [e for e in events if send_at <= bs._parse_ts(e["start_at"]) < end]


def score(e: dict) -> float:
    s = float(e.get("quality_score") or 0)
    if bs._curated(e):
        s += 30
    if e.get("image_url"):
        s += 5
    return s


def is_featured(e: dict) -> bool:
    orgs = {o.lower() for o in (bs.SITE_JSON.get("featured") or {}).get("organizers") or []}
    return (e.get("organizer_name") or "").lower() in orgs


def one_per_series(events: list[dict]) -> list[dict]:
    """A weekly class shouldn't fill a section: keep its best-placed date."""
    seen: set[str] = set()
    out = []
    for e in events:
        key = re.sub(r"\W+", " ", (e.get("title") or "").lower()).strip()
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


def edition_sections(edition: str | None, events: list[dict]) -> dict:
    """{featured, lead, also} for one edition (None = general)."""
    featured = sorted((e for e in events if is_featured(e)), key=lambda e: e["start_at"])[:1]
    rest = [e for e in events if e not in featured]
    ranked = one_per_series(sorted(rest, key=score, reverse=True))
    per_org: dict[str, int] = {}

    def take(pool: list[dict], n: int) -> list[dict]:
        """Best first, but no host more than ORG_MAX times in one email."""
        out = []
        for e in pool:
            org = (e.get("organizer_name") or e.get("venue_name") or "").lower()
            if len(out) >= n or per_org.get(org, 0) >= ORG_MAX:
                continue
            per_org[org] = per_org.get(org, 0) + 1
            out.append(e)
        return out

    if edition:
        lead = take([e for e in ranked if edition in topic_slugs(e)], LEAD_MAX)
        near = set(RELATED[edition])
        others = [e for e in ranked if e not in lead]
        others.sort(key=lambda e: score(e) + (15 if topic_slugs(e) & near else 0), reverse=True)
        also = take(others, ALSO_MAX)
    else:
        lead = take(ranked, LEAD_MAX + 2)
        also = take([e for e in ranked if e not in lead], ALSO_MAX)
    by_date = lambda xs: sorted(xs, key=lambda e: e["start_at"])  # noqa: E731
    return {"featured": featured, "lead": by_date(lead), "also": by_date(also)}


# ---------- rendering ------------------------------------------------------

def link(e: dict, slugs: dict[str, str], campaign: str) -> str:
    slug = slugs.get(f"{e['source']}:{e['source_id']}")
    if slug:
        q = urllib.parse.urlencode(
            {"utm_source": "kit", "utm_medium": "email", "utm_campaign": campaign}
        )
        return f"{bs.BASE_URL}/e/{slug}/?{q}"
    return e.get("source_url") or ""


def site_link(path: str, campaign: str) -> str:
    q = urllib.parse.urlencode(
        {"utm_source": "kit", "utm_medium": "email", "utm_campaign": campaign}
    )
    return f"{bs.BASE_URL}{path}?{q}"


def email_img(url: str, width: int) -> str:
    """The site's card crop: 2:1, centred, from the full-res original
    (bs.full_res, as the site's thumb() does). jpg, not the site's webp:
    Outlook and older mail apps can't show webp. Sized at 2x for retina."""
    src, crop = bs.full_res(url, width * 2)
    # keep an Eventbrite organiser's own crop box; drop its ratio, we set ours
    keep = crop if "precrop" in crop else ""
    return (
        "https://wsrv.nl/?url=" + urllib.parse.quote(src, safe="")
        + f"{keep}&w={width * 2}&h={width}&fit=cover&output=jpg&q=72"
    )


def blurb(e: dict) -> str:
    text = (e.get("hook") or "").strip()
    if not text:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", e.get("description") or "")).strip()
        if len(text) > 120:
            text = text[:117].rsplit(" ", 1)[0] + "…"
    return text


def meta_line(e: dict) -> str:
    parts = [bs.fmt_when_short(e)]
    venue = bs.clean_venue(e)
    if venue:
        parts.append(venue)
    price = bs.fmt_price(e)
    if price:
        parts.append(price)
    return " · ".join(parts)


COL = 272  # two cards and a 16px gap fill the template's 560px column


def event_card(e: dict, href: str) -> str:
    """One site-style card: 2:1 art over the text."""
    if e.get("image_url"):
        art = (
            f'<a href="{bs.esc(href)}"><img src="{bs.esc(email_img(e["image_url"], COL))}" '
            f'width="{COL}" height="{COL // 2}" alt="" style="display:block;border:0;width:100%;'
            f'height:auto;border-radius:14px 14px 0 0;"></a>'
        )
    else:
        # the site's imageless banner: sand gradient (solid where unsupported)
        art = (
            f'<div style="height:{COL // 2}px;border-radius:14px 14px 0 0;background:#eadbc4;'
            f'background-image:linear-gradient(160deg,#f1e6d4,#e3cfb2);"></div>'
        )
    hook = blurb(e)
    hook_html = (
        f'<p style="margin:4px 0 0;font:14px/1.45 {SANS};color:{DIM};">{bs.esc(hook)}</p>'
        if hook else ""
    )
    return f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{CARD};border:1px solid {EDGE};border-radius:16px;">
<tr><td>{art}</td></tr>
<tr><td style="padding:12px 14px 14px;text-align:left;">
  <p style="margin:0;font:600 12px/1.4 {SANS};color:{OCHRE};">{bs.esc(meta_line(e))}</p>
  <p style="margin:3px 0 0;font:600 16px/1.3 {SANS};"><a href="{bs.esc(href)}" style="color:{INK};text-decoration:none;">{bs.esc(e.get("title") or "")}</a></p>
  {hook_html}
</td></tr></table>"""


def card_pair(cards: list[str]) -> str:
    """Two cards side by side that stack on a phone: inline-blocks wrap by
    themselves and the template's media query widens them to full width.
    No Outlook [if mso] tables — Outlook desktop simply stacks them."""
    col = (f'<div class="pc-col" style="display:inline-block;width:100%;max-width:{COL}px;'
           f'vertical-align:top;padding:0 0 16px;">')
    gap = '<div class="pc-gap" style="display:inline-block;width:16px;"></div>'
    inner = gap.join(col + c + "</div>" for c in cards)
    return f'<tr><td style="font-size:0;line-height:0;text-align:left;">{inner}</td></tr>'


def section(title: str, events: list[dict], slugs: dict[str, str], campaign: str) -> str:
    cards = [event_card(e, href) for e in events if (href := link(e, slugs, campaign))]
    if not cards:
        return ""
    rows = "".join(card_pair(cards[i:i + 2]) for i in range(0, len(cards), 2))
    return f"""
<tr><td style="padding:8px 0 12px;">
  <p style="margin:0;font:600 12px/1.4 {SANS};letter-spacing:.08em;text-transform:uppercase;color:{OCHRE};">{bs.esc(title)}</p>
</td></tr>{rows}"""


def featured_block(e: dict, href: str) -> str:
    img = ""
    if e.get("image_url"):
        src = email_img(e["image_url"], 520)
        img = (
            f'<a href="{bs.esc(href)}"><img src="{bs.esc(src)}" width="520" height="260" alt="" '
            f'style="display:block;border:0;width:100%;max-width:520px;height:auto;border-radius:12px;margin:0 0 12px;"></a>'
        )
    label = (bs.SITE_JSON.get("featured") or {}).get("label") or "Our next event"
    return f"""
<tr><td style="padding:0 0 22px;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{CARD};border:1px solid {EDGE};border-radius:16px;">
  <tr><td style="padding:16px;">
    <p style="margin:0 0 10px;font:600 12px/1.4 {SANS};letter-spacing:.08em;text-transform:uppercase;color:{PLUM};">{bs.esc(label)}</p>
    {img}
    <p style="margin:0;font:600 12px/1.4 {SANS};color:{OCHRE};">{bs.esc(meta_line(e))}</p>
    <p style="margin:3px 0 0;font:600 19px/1.3 {SANS};"><a href="{bs.esc(href)}" style="color:{INK};text-decoration:none;">{bs.esc(e.get("title") or "")}</a></p>
    <p style="margin:4px 0 0;font:14px/1.45 {SANS};color:{DIM};">{bs.esc(blurb(e))}</p>
  </td></tr></table>
</td></tr>"""


def render(edition: str | None, secs: dict, send_at: datetime, slugs: dict[str, str]) -> dict:
    """{subject, preview_text, content} for one edition."""
    campaign = f"weekly-{send_at:%Y-%m-%d}-{edition or 'general'}"
    end = send_at + WINDOW - timedelta(days=1)
    span = f"{send_at:%a %-d %b} – {end:%a %-d %b}"
    lead_title = f"{EDITIONS[edition]} this week" if edition else "Picks this week"
    also_title = "Also this week" if edition else "More this week"

    featured = "".join(
        featured_block(e, href) for e in secs["featured"] if (href := link(e, slugs, campaign))
    )
    body = (
        featured
        + section(lead_title, secs["lead"], slugs, campaign)
        + section(also_title, secs["also"], slugs, campaign)
    )
    # Just the body: the background, footer, unsubscribe link and postal
    # address come from the PsyConnect template in Kit (kit/email-template.html).
    content = f"""<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
<tr><td style="padding:0 0 6px;"><a href="{bs.esc(site_link('/', campaign))}"><img src="{bs.BASE_URL}/logo.png" width="180" alt="PsyConnect" style="display:block;border:0;width:180px;height:auto;"></a></td></tr>
<tr><td style="padding:0 0 22px;">
  <p style="margin:0;font:15px/1.5 {SANS};color:{DIM};">In-person gatherings across London · {bs.esc(span)}</p>
</td></tr>
{body}
<tr><td style="padding:8px 0 28px;">
  <a href="{bs.esc(site_link('/', campaign))}" style="display:inline-block;background:{PLUM};color:#fdf6ee;font:600 15px/1 {SANS};text-decoration:none;padding:13px 20px;border-radius:12px;">See everything on PsyConnect</a>
</td></tr>
</table>"""

    # the subject names the strongest picks, not merely the soonest
    picks = [e for e in secs["featured"] + sorted(secs["lead"], key=score, reverse=True)
             if link(e, slugs, campaign)]
    names = [e.get("title") or "" for e in picks[:2]]  # plain text, not HTML
    if edition:
        subject = f"{EDITIONS[edition]} this week: {names[0]}" if names else f"{EDITIONS[edition]} this week in London"
    else:
        subject = f"This week: {names[0]}" if names else "This week in London"
    if len(subject) > 80:
        subject = subject[:77].rsplit(" ", 1)[0] + "…"
    count = len(secs["featured"]) + len(secs["lead"]) + len(secs["also"])
    preview = (
        f"{names[1]} and {count - 2} more gatherings across London, {span}."
        if len(names) > 1 and count > 2
        else f"In-person gatherings across London, {span}."
    )
    return {"subject": subject, "preview_text": preview, "content": content}


# ---------- Kit ------------------------------------------------------------

def api_key() -> str:
    key = os.environ.get("KIT_API_KEY", "")
    env = bs.ROOT / ".env"
    if not key and env.exists():
        m = re.search(r"^KIT_API_KEY=(.+)$", env.read_text(), re.M)
        key = m.group(1).strip().strip("'\"") if m else ""
    if not key:
        sys.exit("KIT_API_KEY missing (environment or .env)")
    return key


class Kit:
    def __init__(self, key: str):
        self.key = key

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            KIT + path, data=data, method=method,
            headers={
                "X-Kit-Api-Key": self.key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "psyconnect-weekly",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as res:
                    raw = res.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as err:
                if err.code == 429 and attempt < 3:
                    time.sleep(20 * (attempt + 1))
                    continue
                detail = err.read().decode(errors="replace")[:500]
                raise SystemExit(f"Kit {method} {path}: {err.code} {detail}") from None
        raise SystemExit(f"Kit {method} {path}: rate limited")

    def paged(self, path: str, key: str) -> list[dict]:
        out, after = [], None
        while True:
            sep = "&" if "?" in path else "?"
            page = self.call("GET", f"{path}{sep}per_page=1000" + (f"&after={after}" if after else ""))
            out.extend(page.get(key) or [])
            pg = page.get("pagination") or {}
            if not pg.get("has_next_page"):
                return out
            after = pg.get("end_cursor")

    def tags(self) -> dict[str, int]:
        return {t["name"]: t["id"] for t in self.paged("/tags", "tags")}

    def tag_id(self, tags: dict[str, int], name: str) -> int:
        if name not in tags:
            tags[name] = self.call("POST", "/tags", {"name": name})["tag"]["id"]
        return tags[name]


def tidy_editions(kit: Kit, tags: dict[str, int]) -> dict[str, int]:
    """Exactly one edition tag per subscriber; returns edition -> member count."""
    ed_ids = {ed: kit.tag_id(tags, f"edition:{ed}") for ed in EDITIONS}
    members: dict[int, dict] = {}
    for name, tid in tags.items():
        ed = name.removeprefix("edition:")
        if name not in EDITIONS and not (name.startswith("edition:") and ed in EDITIONS):
            continue
        for s in kit.paged(f"/tags/{tid}/subscribers", "subscribers"):
            m = members.setdefault(s["id"], {"topics": set(), "editions": set()})
            (m["editions"] if name.startswith("edition:") else m["topics"]).add(ed)

    counts = dict.fromkeys(EDITIONS, 0)
    for sid, m in members.items():
        have = [ed for ed in EDITIONS if ed in m["editions"]]
        if not have:
            pick = next((ed for ed in EDITIONS if ed in m["topics"]), None)
            if pick:
                kit.call("POST", f"/tags/{ed_ids[pick]}/subscribers/{sid}", {})
                print(f"  subscriber {sid}: added edition:{pick}")
                counts[pick] += 1
            continue
        for extra in have[1:]:
            kit.call("DELETE", f"/tags/{ed_ids[extra]}/subscribers/{sid}")
            print(f"  subscriber {sid}: removed extra edition:{extra}")
        counts[have[0]] += 1
    return counts


def clear_previous(kit: Kit, prefix: str) -> None:
    for status in ("draft", "scheduled"):
        for b in kit.paged(f"/broadcasts?status={status}&slim=true", "broadcasts"):
            if (b.get("description") or "").startswith(prefix):
                kit.call("DELETE", f"/broadcasts/{b['id']}")
                print(f"  removed earlier {status} broadcast {b['id']} ({b['description']})")


def html_template(kit: Kit) -> int | None:
    """The PsyConnect template if it's in Kit, else the default HTML one
    ('Starting point' templates can't take API content)."""
    temps = kit.paged("/email_templates", "email_templates")
    html_ones = [t for t in temps if (t.get("category") or "").lower() == "html"]
    for t in html_ones:
        if (t.get("name") or "").strip().lower() == TEMPLATE_NAME.lower():
            return t["id"]
    print(f"  no '{TEMPLATE_NAME}' template in Kit: using the default (paste {TEMPLATE.relative_to(bs.ROOT)} into Kit)")
    for t in html_ones:
        if t.get("is_default"):
            return t["id"]
    return html_ones[0]["id"] if html_ones else None


# ---------- main -----------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("preview", "draft", "schedule"))
    p.add_argument("--out", type=Path, default=bs.ROOT / "build-email")
    p.add_argument("--send-at", help="ISO time (London if no offset); default next Thursday 09:00")
    args = p.parse_args()

    setup_site()
    now = datetime.now(timezone.utc)
    if args.send_at:
        send_at = datetime.fromisoformat(args.send_at)
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=bs.LONDON)
        send_at = send_at.astimezone(bs.LONDON)
    else:
        send_at = next_send(now)
    events = week_events(send_at)
    slugs = live_slugs()
    print(f"Week from {send_at:%a %d %b %H:%M %Z}: {len(events)} events")

    editions: list[str | None] = [*EDITIONS, None]
    built = {ed: render(ed, edition_sections(ed, events), send_at, slugs) for ed in editions}

    if args.mode == "preview":
        args.out.mkdir(parents=True, exist_ok=True)
        links = []
        for ed, mail in built.items():
            name = ed or "general"
            # as Kit will send it: wrapped in the template, footer filled in
            page = (
                TEMPLATE.read_text()
                .replace("{{ message_content }}", mail["content"])
                .replace("{{ unsubscribe_url }}", "#")
                .replace("{{ address }}", "[your postal address, from Kit]")
                .replace(
                    "<body style=\"margin:0;padding:0;background:#f6efe4;\">",
                    "<body style=\"margin:0;padding:0;background:#f6efe4;\">"
                    f"<p style='font:13px {SANS};padding:12px;margin:0;background:#222;color:#eee'>"
                    f"<b>Subject:</b> {bs.esc(mail['subject'])}<br>"
                    f"<b>Preview:</b> {bs.esc(mail['preview_text'])}</p>",
                )
            )
            (args.out / f"{name}.html").write_text(page)
            links.append(f"<li><a href='{name}.html'>{name}</a> — {bs.esc(mail['subject'])}</li>")
        (args.out / "index.html").write_text(
            f"<!doctype html><meta charset='utf-8'><title>Weekly email preview</title>"
            f"<body style='font:15px {SANS};padding:24px'><h1>Weekly email {send_at:%-d %b}</h1><ul>{''.join(links)}</ul>"
        )
        print(f"Wrote {len(built)} previews -> {args.out}/index.html")
        return

    kit = Kit(api_key())
    tags = kit.tags()
    print("Tidying edition tags…")
    counts = tidy_editions(kit, tags)
    print("  edition subscribers:", counts)
    template = html_template(kit)
    prefix = f"weekly:{send_at:%Y-%m-%d}:"
    clear_previous(kit, prefix)

    ed_ids = [tags[f"edition:{ed}"] for ed in EDITIONS]
    for ed, mail in built.items():
        if ed and not counts.get(ed):
            print(f"  {ed}: no subscribers, skipped")
            continue
        flt = (
            {"all": [{"type": "tag", "ids": [tags[f"edition:{ed}"]]}], "any": None, "none": None}
            if ed
            else {"all": None, "any": None, "none": [{"type": "tag", "ids": ed_ids}]}
        )
        body = {
            "content": mail["content"],
            "subject": mail["subject"],
            "preview_text": mail["preview_text"],
            "description": prefix + (ed or "general"),
            "public": False,
            "published_at": send_at.isoformat(),
            "send_at": send_at.isoformat() if args.mode == "schedule" else None,
            "subscriber_filter": [flt],
        }
        if template:
            body["email_template_id"] = template
        b = kit.call("POST", "/broadcasts", body)["broadcast"]
        print(f"  {ed or 'general'}: broadcast {b['id']} {b.get('status')} — {mail['subject']}")


if __name__ == "__main__":
    main()
