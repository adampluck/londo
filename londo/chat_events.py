"""Events shared in a group chat as a flyer plus a blurb, with no event
page to fetch — or a page (linktr.ee, a personal site) that carries no
schema.org data. One Claude call per post reads the caption and the
flyer photo(s) and returns the listing fields; it's only kept when it
has everything a fetched event page would give us."""
from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from londo.geo import is_london
from londo.models import Event, Location, Organizer, PriceTier
from londo.whatsapp import Post

logger = logging.getLogger(__name__)

MODEL = "claude-opus-5"
LONDON = ZoneInfo("Europe/London")
MAX_PHOTOS = 3  # flyer + a couple of extras; the rest are repeats

SYSTEM_PROMPT = """\
You extract event listings from posts shared in a London community WhatsApp
group. Each post is a message (the caption) plus one or more attached
photos, usually a flyer. Read both: the flyer often carries the date, time,
venue or price that the caption leaves out.

Return is_event=false when the post is not announcing a specific, dated,
in-person gathering (a general offer, a course with no date, a retreat
elsewhere, a job ad, chit-chat, a multi-city tour listing with no London
date). Otherwise fill in the listing:

- title: the event's own name as written on the flyer or caption, in normal
  title case, without emoji, decorations or "TONIGHT"-style urgency.
- description: 2-6 sentences in the organiser's own words, lightly tidied —
  what happens, who it's for, who holds it. No emoji, no phone numbers, no
  "DM me". Keep it factual; don't invent detail.
- start / end: ISO 8601 local London time (e.g. 2026-09-25T19:00). Resolve
  relative dates ("tonight", "this Friday", "Sat 27th") against the date the
  message was sent. A date with no time of day → leave start null.
  If the post lists several dates (a series), use the first upcoming one.
- venue_name / address: as given. If only a neighbourhood is named
  ("Maida Vale", "Walthamstow E17"), put it in address followed by ", London".
- in_london: true only if the venue is in Greater London. A retreat in
  Dorset, a Bristol workshop or an online session is false.
- is_online: true for Zoom/online-only events.
- organizer: the collective, studio or public facilitator brand named in
  the flyer or caption — never the WhatsApp sender's handle. null if none.
- price_gbp: the cheapest ticket price in pounds, 0 if free, null if not
  stated. donation-based → 0 with is_donation true.
- booking_url: the URL in the post that leads to tickets or details, if any.\
"""


class ChatEvent(BaseModel):
    is_event: bool
    title: str | None = None
    description: str | None = None
    start: str | None = None
    end: str | None = None
    venue_name: str | None = None
    address: str | None = None
    in_london: bool = False
    is_online: bool = False
    organizer: str | None = None
    price_gbp: float | None = None
    is_donation: bool = False
    booking_url: str | None = None
    reason: str = Field(default="", description="one line on what was kept or why not")


def extract_event(client, post: Post, media_dir: Path) -> ChatEvent | None:
    """Ask the model for the listing in a post. Returns None (with a log
    line) when it isn't a listable event."""
    content: list[dict] = []
    for name in post.photos[:MAX_PHOTOS]:
        path = media_dir / name
        if not path.exists():
            logger.warning("Attachment missing from export: %s", name)
            continue
        media_type = mimetypes.guess_type(name)[0] or "image/jpeg"
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(path.read_bytes()).decode(),
                },
            }
        )
    if not content:
        return None

    sent = post.sent_at.strftime("%A %d %B %Y, %H:%M")
    content.append(
        {
            "type": "text",
            "text": f"Message sent: {sent} (London time)\n\nCaption:\n{post.text or '(no caption)'}",
        }
    )
    response = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
        output_format=ChatEvent,
    )
    extracted: ChatEvent = response.parsed_output
    logger.info("Extracted from %s post: %s — %s", post.sender, extracted.title, extracted.reason)
    return extracted


def build_event(extracted: ChatEvent, post: Post) -> Event | None:
    """Apply the completeness gate (title, description, dated start with a
    time, London venue) and build the Event, or return None. The caller
    sets image_url once it has hosted the flyer — a post without a photo
    never reaches extraction."""
    title = (extracted.title or "").strip()
    description = (extracted.description or "").strip()
    start = _parse_local(extracted.start)
    address = (extracted.address or "").strip()
    venue = (extracted.venue_name or "").strip() or None
    place = " ".join(p for p in (venue, address) if p)

    missing = []
    if not extracted.is_event:
        missing.append("not an event")
    if not title:
        missing.append("title")
    if len(description) < 40:
        missing.append("description")
    if start is None:
        missing.append("date+time")
    if not place:
        missing.append("location")
    elif extracted.is_online or not (extracted.in_london or is_london(place)):
        missing.append("London venue")
    if missing:
        logger.info("Skipping chat post from %s (%s)", post.sender, ", ".join(missing))
        return None

    if not is_london(address):
        address = f"{address}, London" if address else "London"

    price_tiers: list[PriceTier] = []
    if extracted.price_gbp is not None:
        price_tiers.append(
            PriceTier(
                name="Donation" if extracted.is_donation else "Ticket",
                amount=Decimal(str(extracted.price_gbp)),
            )
        )

    # Stable across re-exports (photo counters shift) and reposts of the
    # same flyer: identity is the title on the day.
    slug = re.sub(r"[^a-z0-9]+", "", title.lower())
    source_id = hashlib.sha1(f"{slug}|{start.date().isoformat()}".encode()).hexdigest()[:16]

    booking = _match_post_url(extracted.booking_url, post)

    return Event(
        source="whatsapp",
        source_id=source_id,
        source_url=booking,
        title=title,
        description=description,
        start_datetime=start,
        end_datetime=_parse_local(extracted.end),
        location=Location(venue_name=venue, address=address, city="London"),
        is_online=False,
        price_tiers=price_tiers,
        is_free=bool(price_tiers) and all(t.amount == 0 for t in price_tiers),
        organizer=Organizer(name=extracted.organizer.strip()) if extracted.organizer else None,
        scraped_at=datetime.now(timezone.utc),
    )


def _match_post_url(candidate: str | None, post: Post) -> str:
    """The model may only hand back a link that was actually in the post,
    and only one that could be an event/details page (no chat invites,
    map pins or socials)."""
    from londo.links import classify_url

    if not candidate:
        return ""
    want = _url_key(candidate)
    for url in post.urls:
        if _url_key(url) == want and classify_url(url) is not None:
            return url
    return ""


def _url_key(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.strip().lower()).split("?")[0].rstrip("/")


def _parse_local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LONDON)
    return dt


def photo_digest(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()[:20]
