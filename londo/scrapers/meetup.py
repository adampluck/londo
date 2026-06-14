from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import icalendar

from londo.models import Event, Location, Organizer
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

ICS_URL = "https://www.meetup.com/{slug}/events/ical/"

MEETUP_GROUPS = [
    "socialsportsmix",
    "the-philosophy-cafe-london",
    "watkinsbooks",
]

_MEETUP_URL_RE = re.compile(r"https?://(?:www\.)?meetup\.com/\S+")


class MeetupScraper(BaseScraper):
    """Scrapes Meetup groups via their public iCal feeds."""

    source_name = "meetup"

    def scrape(self) -> list[Event]:
        events: list[Event] = []
        for slug in MEETUP_GROUPS:
            try:
                group_events = self._scrape_group(slug)
                events.extend(group_events)
                logger.info("Got %d events from Meetup group '%s'", len(group_events), slug)
            except Exception:
                logger.exception("Failed to scrape Meetup group '%s'", slug)
        logger.info("Scraped %d events from Meetup", len(events))
        return events

    def _scrape_group(self, slug: str) -> list[Event]:
        response = self.get(ICS_URL.format(slug=slug))
        cal = icalendar.Calendar.from_ical(response.text)

        group_name = None
        for component in cal.walk():
            if component.name == "VCALENDAR":
                raw = component.get("X-WR-CALNAME", "")
                if isinstance(raw, list):
                    raw = raw[0] if raw else ""
                group_name = raw.to_ical().decode() if hasattr(raw, "to_ical") else str(raw).strip() or None
                break

        events = []
        for component in cal.walk("VEVENT"):
            event = self._build_event(component, slug, group_name)
            if event is not None:
                events.append(event)
        return events

    def _build_event(
        self, component, slug: str, group_name: str | None
    ) -> Event | None:
        start = component.get("DTSTART")
        if start is None or not isinstance(start.dt, datetime):
            return None

        end = component.get("DTEND")
        uid = str(component.get("UID", "")).strip()
        url = str(component.get("URL", f"https://www.meetup.com/{slug}/events/")).strip()
        title = str(component.get("SUMMARY", "")).strip()

        raw_desc = str(component.get("DESCRIPTION", ""))
        description = _clean_description(raw_desc) or None

        location_str = str(component.get("LOCATION", "")).strip()
        if location_str:
            location = Location(
                venue_name=location_str.split(",")[0].strip(),
                address=location_str,
                city="London",
            )
        else:
            location = Location(address="London, UK", city="London")

        organizer = Organizer(
            name=group_name or slug,
            url=f"https://www.meetup.com/{slug}/",
        )

        return Event(
            source="meetup",
            source_id=uid or url,
            source_url=url,
            external_ref=f"meetup:{uid}" if uid else None,
            title=title,
            description=description,
            start_datetime=start.dt,
            end_datetime=(
                end.dt
                if end is not None and isinstance(end.dt, datetime)
                else None
            ),
            location=location,
            organizer=organizer,
            scraped_at=datetime.now(timezone.utc),
        )


def _clean_description(desc: str) -> str:
    """Strip Meetup boilerplate (RSVP links, trailing meetup.com URLs)."""
    text = desc.replace("\\n", "\n")
    lines = text.splitlines()
    kept = []
    for line in lines:
        stripped = line.strip()
        if _MEETUP_URL_RE.fullmatch(stripped):
            continue
        if stripped.lower().startswith("rsvp"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()
