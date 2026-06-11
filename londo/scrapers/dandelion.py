from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import icalendar
from bs4 import BeautifulSoup

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

ICAL_URL = (
    "https://dandelion.events/events.ics"
    "?event_tag_id=&from={from_date}&in_person=1"
    "&near=London%2C+UK&order=&q=&to="
)
EVENT_PAGE_URL = "https://dandelion.events/events/{uid}"


class DandelionScraper(BaseScraper):
    source_name = "dandelion"

    def scrape(self) -> list[Event]:
        today = date.today().isoformat()
        url = ICAL_URL.format(from_date=today)
        logger.info("Fetching iCal feed: %s", url)

        response = self.get(url)
        cal = icalendar.Calendar.from_ical(response.text)

        uids: list[str] = []
        for component in cal.walk():
            if component.name == "VEVENT":
                uid = str(component.get("UID", ""))
                if uid:
                    uids.append(uid)

        logger.info("Found %d events in iCal feed", len(uids))

        events: list[Event] = []
        for uid in uids:
            try:
                event = self._scrape_event(uid)
                events.append(event)
                logger.info("Scraped: %s", event.title)
            except Exception:
                logger.exception("Failed to scrape event %s", uid)

        logger.info("Successfully scraped %d/%d events", len(events), len(uids))
        return events

    def _scrape_event(self, uid: str) -> Event:
        url = EVENT_PAGE_URL.format(uid=uid)
        response = self.get(url)
        soup = BeautifulSoup(response.text, "html.parser")

        json_ld = self._extract_json_ld(soup)
        tags = self._extract_tags(soup)
        image_url = self._extract_og_image(soup) or json_ld.get("image")

        start_dt, end_dt, start_d, is_all_day = self._parse_json_ld_dates(json_ld)
        location = self._build_location(json_ld)
        price_tiers = self._build_price_tiers(json_ld)
        organizer = self._build_organizer(json_ld)

        is_online = "OnlineEventAttendanceMode" in json_ld.get(
            "eventAttendanceMode", ""
        )
        is_free = len(price_tiers) > 0 and all(t.amount == 0 for t in price_tiers)

        return Event(
            source="dandelion",
            source_id=uid,
            source_url=url,
            title=json_ld.get("name", ""),
            description=json_ld.get("description"),
            short_description=json_ld.get("description"),
            start_datetime=start_dt,
            end_datetime=end_dt,
            start_date=start_d,
            is_all_day=is_all_day,
            location=location,
            is_online=is_online,
            image_url=image_url,
            tags=tags,
            price_tiers=price_tiers,
            is_free=is_free,
            organizer=organizer,
            scraped_at=datetime.now(timezone.utc),
        )

    def _extract_json_ld(self, soup: BeautifulSoup) -> dict:
        script = soup.find("script", type="application/ld+json")
        if script and script.string:
            try:
                return json.loads(script.string)
            except json.JSONDecodeError:
                logger.warning("Failed to parse JSON-LD")
        return {}

    def _extract_tags(self, soup: BeautifulSoup) -> list[str]:
        tags = []
        for a in soup.find_all("a", href=True):
            if "event_tag_id=" in a["href"]:
                text = a.get_text(strip=True)
                if text:
                    tags.append(text)
        return tags

    def _extract_og_image(self, soup: BeautifulSoup) -> str | None:
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            return meta["content"]
        return None

    def _parse_json_ld_dates(
        self, json_ld: dict
    ) -> tuple[datetime | None, datetime | None, date | None, bool]:
        start_str = json_ld.get("startDate")
        end_str = json_ld.get("endDate")

        if not start_str:
            return None, None, None, False

        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str) if end_str else None

        # If time is midnight-to-midnight, treat as all-day
        if (
            start_dt.hour == 0
            and start_dt.minute == 0
            and end_dt
            and end_dt.hour == 0
            and end_dt.minute == 0
        ):
            return None, None, start_dt.date(), True

        return start_dt, end_dt, None, False

    def _build_location(self, json_ld: dict) -> Location | None:
        loc = json_ld.get("location")
        if not loc:
            return None

        addr = loc.get("address", {})
        address_str = addr.get("name", "") if isinstance(addr, dict) else str(addr)

        return Location(
            venue_name=loc.get("name"),
            address=address_str,
        )

    def _build_price_tiers(self, json_ld: dict) -> list[PriceTier]:
        offers = json_ld.get("offers", [])
        if not offers:
            return []

        tiers = []
        for i, offer in enumerate(offers):
            availability_raw = offer.get("availability", "")
            availability = availability_raw.split("/")[-1] if availability_raw else None

            try:
                amount = Decimal(str(offer.get("price", 0)))
            except Exception:
                amount = Decimal(0)

            tiers.append(
                PriceTier(
                    name=f"Tier {i + 1}",
                    amount=amount,
                    currency=offer.get("priceCurrency", "GBP"),
                    availability=availability or None,
                )
            )
        return tiers

    def _build_organizer(self, json_ld: dict) -> Organizer | None:
        org = json_ld.get("organizer")
        if not org:
            return None

        return Organizer(
            name=org.get("name", "Unknown"),
            url=org.get("url"),
        )
