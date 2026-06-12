from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from bs4 import BeautifulSoup

from londo.links import LinkFetcher, classify_url
from londo.models import Event, Location, PriceTier
from londo.scrapers.base import BaseScraper
from londo.scrapers.eventbrite import BROWSER_UA

logger = logging.getLogger(__name__)

LISTING_JSON_URL = "https://www.psycalendar.com/other-psy-events?format=json"
LISTING_PAGE_URL = "https://www.psycalendar.com/other-psy-events"

LONDON_RE = re.compile(r"\blondon\b", re.I)
PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
FREE_RE = re.compile(r"\bfree\b", re.I)


class PsyCalendarScraper(BaseScraper):
    """Scrapes the PsyCalendar 'Other Psy Events' Squarespace collection.

    PsyCalendar is itself an aggregator: each listing links out to the
    actual ticketing page (Eventbrite, Dandelion, Luma, ...). Only
    in-person London listings are kept, and each is resolved via its
    ticket link so the event carries full canonical data. Listings
    without complete details (date+time, location, description, image,
    cost) are skipped. Events are emitted as source 'other' so they show
    under 'elsewhere'; when another scraper also covers an event, dedupe
    keeps that scraper's copy as canonical.
    """

    source_name = "psycalendar"

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        # Squarespace serves the ?format=json view to browsers only
        self.session.headers.update({"User-Agent": BROWSER_UA})
        self._fetcher = LinkFetcher(rate_limit=rate_limit)

    def scrape(self) -> list[Event]:
        data = self.get(LISTING_JSON_URL).json()
        items = data.get("upcoming") or []
        logger.info("PsyCalendar lists %d upcoming events", len(items))

        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        events: list[Event] = []
        for item in items:
            if not _is_london_in_person(item):
                continue
            for event in self._resolve(item):
                if not _is_complete(event):
                    logger.info(
                        "Skipping incomplete listing: %s (%s)",
                        _clean(item.get("title")),
                        event.source_url,
                    )
                    continue
                if event.start_datetime and event.start_datetime < cutoff:
                    continue
                events.append(event)
                logger.info("Scraped: %s", event.title)

        logger.info("Kept %d in-person London events", len(events))
        return events

    def _resolve(self, item: dict) -> list[Event]:
        """Fetch canonical event data from the listing's ticket link,
        falling back to the listing's own data when the link can't be
        resolved (e.g. Instagram, bare websites)."""
        url = (item.get("sourceUrl") or "").strip()
        if url and not url.lower().startswith("http"):
            url = "https://" + url

        fetched: list[Event] = []
        if url and classify_url(url) is not None:
            fetched = self._fetcher.fetch(url)
        if fetched:
            body = _body_text(item.get("body"))
            for event in fetched:
                event.source = "other"
                if event.location is None:
                    event.location = _item_location(item)
                if not event.tags:
                    event.tags = _item_tags(item)
                # some ticket pages only expose a stub description
                # (e.g. just the organizer name) — PsyCalendar's own
                # listing text is the better read then
                if body and len(body) > len(event.description or "") * 4:
                    event.description = body
            return fetched

        event = _build_from_item(item, url)
        return [event] if event is not None else []


def _is_london_in_person(item: dict) -> bool:
    loc = item.get("location") or {}
    # Vague listings ("Everywhere", "Northern Hemisphere") and online
    # events have no street address.
    if not (loc.get("addressLine1") or loc.get("addressLine2")):
        return False
    text = " ".join(
        str(loc.get(k) or "")
        for k in ("addressTitle", "addressLine1", "addressLine2")
    )
    return bool(LONDON_RE.search(text))


def _is_complete(event: Event) -> bool:
    has_cost = bool(event.price_tiers) or event.is_free
    return bool(
        event.title
        and event.start_datetime
        and event.description
        and event.image_url
        and event.location
        and not event.is_online
        and has_cost
    )


def _build_from_item(item: dict, url: str) -> Event | None:
    title = _clean(item.get("title"))
    start = _from_ms(item.get("startDate"))
    end = _from_ms(item.get("endDate"))
    description = _body_text(item.get("body"))
    image = item.get("assetUrl") or None
    location = _item_location(item)
    price_tiers, is_free = _price_from_text(description)

    source_url = url or LISTING_PAGE_URL.rstrip("/") + (item.get("fullUrl") or "")
    event = Event(
        source="other",
        source_id=f"psycal-{item.get('id') or item.get('urlId')}",
        source_url=source_url,
        title=title or "",
        description=description or None,
        start_datetime=start,
        end_datetime=end,
        location=location,
        image_url=image,
        tags=_item_tags(item),
        price_tiers=price_tiers,
        is_free=is_free,
        scraped_at=datetime.now(timezone.utc),
    )
    return event if _is_complete(event) else None


def _item_location(item: dict) -> Location | None:
    loc = item.get("location") or {}
    address = ", ".join(
        p for p in (loc.get("addressLine1"), loc.get("addressLine2")) if p
    )
    venue = (loc.get("addressTitle") or "").strip() or None
    if not address and not venue:
        return None
    return Location(venue_name=venue, address=address or venue, city="London")


def _item_tags(item: dict) -> list[str]:
    return [t.lower() for t in item.get("tags") or []]


def _price_from_text(text: str | None) -> tuple[list[PriceTier], bool]:
    """A listing without a resolvable ticket page only has prose to go on:
    accept it when the text states a £ amount or says the event is free."""
    if not text:
        return [], False
    amounts = sorted({Decimal(m) for m in PRICE_RE.findall(text)})
    tiers = [
        PriceTier(name=f"Tier {i + 1}", amount=amount)
        for i, amount in enumerate(amounts)
    ]
    is_free = not tiers and FREE_RE.search(text) is not None
    return tiers, is_free


def _body_text(body: str | None) -> str | None:
    if not body:
        return None
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()
    return soup.get_text(" ", strip=True) or None


def _clean(value) -> str | None:
    return html.unescape(str(value)).strip() if value else None


def _from_ms(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None
