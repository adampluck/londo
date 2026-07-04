from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from decimal import Decimal

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper
from londo.scrapers.eventbrite import BROWSER_UA

logger = logging.getLogger(__name__)

WHATS_ON_URL = "https://www.studysociety.org/whats-on/"
BOOT_URL = "https://core.service.elfsight.com/p/boot/"

WIDGET_ID_RE = re.compile(r"elfsight-app-([0-9a-f-]{36})")

PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
# descriptions here use "free" loosely ("free movement", a host surnamed
# Free), so only an explicit no-charge phrasing counts
FREE_RE = re.compile(
    r"\bfree\s+(?:entry|event|admission|to\s+attend)\b"
    r"|\bentry\s+is\s+free\b|\bfree\s+of\s+charge\b",
    re.I,
)

COLET_HOUSE = Location(
    venue_name="Colet House",
    address="151 Talgarth Rd, London W14 9DA",
    city="London",
    country="GB",
)


class StudySocietyScraper(BaseScraper):
    """Scrapes The Study Society (Colet House, Barons Court) events.

    Their Ticket Tailor box office is behind a Cloudflare challenge, but
    the What's On page on their own site renders an Elfsight event-calendar
    widget whose boot endpoint serves the full event list as JSON: titles,
    start/end times, HTML descriptions, cover images, locations and the
    Ticket Tailor booking link per event. The widget id is read from the
    page on each run so a re-embedded widget doesn't break the scraper.

    Recurring entries (weekly classes) are skipped: only one-off, in-person
    events are emitted. The booking link is used as source_url.
    """

    source_name = "studysociety"

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self.session.headers.update({"User-Agent": BROWSER_UA})

    def scrape(self) -> list[Event]:
        page = self.get(WHATS_ON_URL).text
        match = WIDGET_ID_RE.search(page)
        if not match:
            raise RuntimeError("No Elfsight widget found on What's On page")
        widget_id = match.group(1)

        boot_url = f"{BOOT_URL}?page={quote(WHATS_ON_URL, safe='')}&w={widget_id}"
        self.session.headers["Referer"] = WHATS_ON_URL
        data = self.get(boot_url).json()

        settings = data["data"]["widgets"][widget_id]["data"]["settings"]
        locations = {loc["id"]: loc for loc in settings.get("locations") or []}
        types = {t["id"]: t["name"] for t in settings.get("eventTypes") or []}

        items = settings.get("events") or []
        n_recurring = sum(1 for i in items if i.get("repeatPeriod") != "noRepeat")
        logger.info(
            "Widget lists %d events (%d recurring, skipped)",
            len(items),
            n_recurring,
        )

        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        events: list[Event] = []
        for item in items:
            if item.get("repeatPeriod") != "noRepeat":
                continue
            event = _build_event(item, locations, types)
            if event is None:
                continue
            if event.start_datetime and event.start_datetime < cutoff:
                continue
            if event.start_datetime is None and (
                event.start_date is None or event.start_date < cutoff.date()
            ):
                continue
            events.append(event)
            logger.info("Scraped: %s", event.title)

        logger.info("Kept %d upcoming one-off events", len(events))
        return events


def _build_event(item: dict, locations: dict, types: dict) -> Event | None:
    title = (item.get("name") or "").strip()
    if not title:
        return None

    loc_names = [
        (locations.get(lid) or {}).get("name", "").strip()
        for lid in item.get("location") or []
    ]
    is_online = bool(loc_names) and all(n == "Online" for n in loc_names)
    if is_online:
        return None
    # Colet House is the society's only venue; "Hybrid" events run there
    # with an online option, so both map to the house address.
    location = (
        COLET_HOUSE.model_copy()
        if any(n in ("Colet House", "Hybrid") for n in loc_names)
        else None
    )

    tz = ZoneInfo(item.get("timeZone") or "Europe/London")
    start_dt, start_d = _parse_when(item.get("start"), tz)
    end_dt, _ = _parse_when(item.get("end"), tz)

    description = _html_text(item.get("description"))
    price_tiers, is_free = _price_from_text(description)

    ticket_url = _ticket_url(item)
    tags = sorted(
        {types[tid].lower() for tid in item.get("eventType") or [] if tid in types}
        | {str(t).lower() for t in item.get("tags") or []}
    )

    cover = item.get("coverImage") or {}

    return Event(
        source="studysociety",
        source_id=str(item.get("id")),
        source_url=ticket_url or WHATS_ON_URL,
        title=title,
        description=description,
        start_datetime=start_dt,
        end_datetime=end_dt,
        start_date=start_d,
        is_all_day=start_dt is None and start_d is not None,
        location=location,
        image_url=cover.get("url") or None,
        tags=tags,
        price_tiers=price_tiers,
        is_free=is_free,
        organizer=Organizer(
            name="The Study Society", url="https://www.studysociety.org/"
        ),
        scraped_at=datetime.now(timezone.utc),
    )


def _parse_when(
    value, tz: ZoneInfo
) -> tuple[datetime | None, date | None]:
    if not isinstance(value, dict) or not value.get("date"):
        return None, None
    day = date.fromisoformat(value["date"])
    time_str = value.get("time")
    if not time_str:
        return None, day
    hour, minute = (int(p) for p in time_str.split(":")[:2])
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return local.astimezone(timezone.utc), day


def _ticket_url(item: dict) -> str | None:
    for action in item.get("actions") or []:
        link = (action or {}).get("link") or {}
        url = link.get("rawValue") or ""
        if link.get("type") == "url" and url.startswith("http"):
            # links copied from the site's embedded widget carry modal
            # parameters that render a bare iframe view in a normal tab
            return url.replace("?modal_widget=true&widget=true", "")
    return None


def _price_from_text(text: str | None) -> tuple[list[PriceTier], bool]:
    if not text:
        return [], False
    amounts = sorted({Decimal(m) for m in PRICE_RE.findall(text)})
    tiers = [
        PriceTier(name=f"Tier {i + 1}", amount=amount)
        for i, amount in enumerate(amounts)
    ]
    is_free = not tiers and FREE_RE.search(text) is not None
    return tiers, is_free


def _html_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text(" ", strip=True) or None
