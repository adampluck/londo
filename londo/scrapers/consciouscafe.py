from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from londo.geo import is_london
from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

API_URL = (
    "https://consciouscafe.org/wp-json/tribe/events/v1/events"
    "?categories=group-events&per_page=50&page={page}"
)
MAX_PAGES = 10

# The site's own categories split the programme cleanly: 'group-events' is
# the in-person meet-up track, 'online-events' the webinars. Only the
# former is fetched.
GROUP_URL = "https://consciouscafe.org/events/category/group-events/"

# Descriptions state the address on a "Venue: ..." line when the event has
# no venue record attached.
VENUE_LINE_RE = re.compile(r"Venue:\s*(.+?)(?:\s{2,}|\s*Admission\b|$)", re.I)
CENTRAL_RE = re.compile(r"\bcentral london\b", re.I)


class ConsciousCafeScraper(BaseScraper):
    """Scrapes ConsciousCafe's in-person group events.

    The site runs The Events Calendar (WordPress), whose REST API serves
    upcoming events with descriptions, cover images, venues and prices —
    no HTML scraping needed.

    ConsciousCafe is a national network, so the 'group-events' category
    mixes London meet-ups with other chapters (Canterbury and the like).
    Events are kept only on positive evidence of a London location: a
    London venue record, a London 'Venue:' line, or — for the roving
    London lunches and dinners, which are booked too late to carry a
    venue — the listing naming London itself.
    """

    source_name = "consciouscafe"

    def scrape(self) -> list[Event]:
        events: list[Event] = []
        page = 1
        while page <= MAX_PAGES:
            data = self.get(API_URL.format(page=page)).json()
            items = data.get("events") or []
            if not items:
                break
            for item in items:
                try:
                    event = _build_event(item)
                except Exception:
                    logger.exception("Failed to parse event %s", item.get("url"))
                    continue
                if event is None:
                    continue
                events.append(event)
                logger.info("Scraped: %s", event.title)
            if page >= int(data.get("total_pages") or 1):
                break
            page += 1

        logger.info("Scraped %d events from ConsciousCafe", len(events))
        return events


def _build_event(item: dict) -> Event | None:
    title = html.unescape((item.get("title") or "").strip())
    if not title:
        return None

    description = _html_text(item.get("description"))
    location = _location(item, description, title)
    if location is None:
        logger.debug("Skipping non-London event: %s", title)
        return None

    tz = ZoneInfo(item.get("timezone") or "Europe/London")
    start = _parse_when(item.get("start_date"), tz)
    end = _parse_when(item.get("end_date"), tz)
    if start is None:
        return None

    price_tiers, is_free = _price(item)

    return Event(
        source="consciouscafe",
        source_id=str(item.get("id")),
        source_url=item.get("url") or GROUP_URL,
        title=title,
        description=description,
        start_datetime=start,
        end_datetime=end,
        start_date=start.astimezone(tz).date(),
        is_all_day=bool(item.get("all_day")),
        location=location,
        image_url=(item.get("image") or {}).get("url") or None,
        price_tiers=price_tiers,
        is_free=is_free,
        organizer=Organizer(name="ConsciousCafe", url="https://consciouscafe.org/"),
        scraped_at=datetime.now(timezone.utc),
    )


def _location(item: dict, description: str | None, title: str) -> Location | None:
    """The event's London location, or None if it isn't a London event."""
    # `venue` is an object when set and an empty list when not.
    venue = item.get("venue")
    if isinstance(venue, dict):
        parts = [
            (venue.get(key) or "").strip()
            for key in ("venue", "address", "city", "zip")
        ]
        text = ", ".join(part for part in parts if part)
        if not is_london(text):
            return None
        name, address, city, postcode = parts
        return Location(
            venue_name=name or None,
            address=", ".join(p for p in (address, city, postcode) if p) or text,
            city=city or "London",
            country="GB",
            latitude=_to_float(venue.get("geo_lat")),
            longitude=_to_float(venue.get("geo_lng")),
        )

    match = VENUE_LINE_RE.search(description or "")
    if match:
        address = match.group(1).strip(" .,")
        if not is_london(address):
            return None
        return Location(address=address, city="London", country="GB")

    # No venue on the record: the London lunches and dinners settle on a
    # restaurant late, and say only which part of town they are in.
    blurb = f"{title} {description or ''}"
    if not is_london(blurb):
        return None
    return Location(
        address="Central London" if CENTRAL_RE.search(blurb) else "London, UK",
        city="London",
        country="GB",
    )


def _parse_when(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        return None
    return local.replace(tzinfo=tz).astimezone(timezone.utc)


def _price(item: dict) -> tuple[list[PriceTier], bool]:
    values = (item.get("cost_details") or {}).get("values") or []
    amounts = []
    for value in values:
        try:
            amounts.append(Decimal(str(value)))
        except (InvalidOperation, ValueError):
            continue
    amounts = sorted(set(amounts))
    if amounts and all(amount == 0 for amount in amounts):
        return [], True
    tiers = [
        PriceTier(name=f"Tier {i + 1}", amount=amount)
        for i, amount in enumerate(amounts)
    ]
    is_free = not tiers and "free" in html.unescape(item.get("cost") or "").lower()
    return tiers, is_free


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _html_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text(" ", strip=True) or None
