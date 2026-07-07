from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

STANDALONE_URL = "https://api.momence.com/schedule/GetLatestStandalone"
SESSIONS_URL = "https://api.momence.com/host-plugins/host/{host_id}/host-schedule/sessions"

# Momence hosts to scrape (momence.com/u/<slug>). Only the host page's
# Workshops tab is kept ('special-event-new' sessions); the recurring
# studio classes on the Classes tab ('fitness') are skipped.
HOSTS = [
    "om-being-5qclsU",
]


class MomenceScraper(BaseScraper):
    """Scrapes Momence host pages (momence.com/u/<slug>).

    The host page is a JS app fed by two public endpoints: the
    GetLatestStandalone schedule API resolves a host slug to its numeric
    host id and display name, and the host-plugins sessions API returns
    every upcoming session (both tabs) as JSON in one page.
    """

    source_name = "momence"

    def scrape(self) -> list[Event]:
        events: list[Event] = []
        for slug in HOSTS:
            try:
                events.extend(self._scrape_host(slug))
            except Exception:
                logger.exception("Momence host '%s' failed", slug)
        logger.info("Scraped %d events from Momence", len(events))
        return events

    def _scrape_host(self, slug: str) -> list[Event]:
        params = urlencode(
            {"hostUrl": slug, "timezoneOffset": 0, "excludeCollections": "true"}
        )
        info = self.get(f"{STANDALONE_URL}?{params}").json()["message"]["info"]
        host_id = info["hostId"]
        organizer = Organizer(
            name=info.get("name") or slug,
            url=f"https://momence.com/u/{slug}",
        )

        data = self.get(SESSIONS_URL.format(host_id=host_id)).json()
        items = data.get("payload") or []
        workshops = [i for i in items if i.get("type") == "special-event-new"]
        logger.info(
            "Host '%s' lists %d sessions, %d workshops",
            slug,
            len(items),
            len(workshops),
        )

        events = []
        for item in workshops:
            if item.get("isCancelled"):
                continue
            event = _build_event(item, organizer)
            events.append(event)
            logger.info("Scraped: %s", event.title)
        return events


def _build_event(item: dict, organizer: Organizer) -> Event:
    address = (item.get("location") or "").strip()
    location = None
    if address and item.get("inPerson"):
        location = Location(
            venue_name=organizer.name,
            address=address,
            city="London",
            country="GB",
        )

    price_tiers: list[PriceTier] = []
    amount = item.get("fixedTicketPrice") or item.get("dynamicTicketPriceMin")
    if amount is not None:
        price_tiers.append(PriceTier(name="Ticket", amount=Decimal(str(amount))))

    teacher = (item.get("teacher") or "").strip()
    description = (item.get("level") or "").strip() or None
    if description and teacher and teacher.lower() not in description.lower():
        description = f"{description}\n\nWith {teacher}."

    return Event(
        source="momence",
        source_id=str(item["id"]),
        source_url=item.get("link") or f"https://momence.com/s/{item['id']}",
        title=(item.get("sessionName") or "").strip(),
        description=description,
        start_datetime=_parse_iso(item.get("startsAt")),
        end_datetime=_parse_iso(item.get("endsAt")),
        location=location,
        is_online=not item.get("inPerson", True),
        image_url=item.get("image") or None,
        price_tiers=price_tiers,
        is_free=bool(item.get("freeEvent")),
        organizer=organizer,
        scraped_at=datetime.now(timezone.utc),
    )


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
