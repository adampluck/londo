from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import NamedTuple

import icalendar
from bs4 import BeautifulSoup

from londo.geo import POSTCODE_RE, is_london
from londo.models import Event, Location, Organizer
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

ICS_URL = "https://www.meetup.com/{slug}/events/ical/"

MEETUP_GROUPS = [
    "socialsportsmix",
    "the-philosophy-cafe-london",
    "watkinsbooks",
    "thought-experiments-in-pubs",
    "spiritual-vibes",
    "spiritualunderground",
]

_MEETUP_URL_RE = re.compile(r"https?://(?:www\.)?meetup\.com/\S+")

LONDON_FALLBACK = Location(address="London, UK", city="London")


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
            if event is None:
                continue
            details = self._fetch_details(event.source_url)
            if details.elsewhere:
                logger.info("Skipping online/non-London event: %s", event.title)
                continue
            events.append(
                event.model_copy(
                    update={
                        "image_url": details.image_url or event.image_url,
                        "location": (
                            details.location or event.location or LONDON_FALLBACK
                        ),
                    }
                )
            )
        return events

    def _fetch_details(self, url: str) -> _PageDetails:
        """Read the cover image and venue off an event page.

        Meetup's iCal feed no longer carries LOCATION, but each event page
        is server-rendered with a schema.org Event blob holding the venue
        (and marking online-only events as a VirtualLocation), so one fetch
        covers both the image and the address.
        """
        try:
            html = self.get(url).text
        except Exception:
            logger.debug("Could not fetch event page %s", url)
            return _PageDetails()

        soup = BeautifulSoup(html, "html.parser")
        image_url = None
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            image_url = tag["content"]

        place = _event_place(soup)
        if place is None:
            return _PageDetails(image_url=image_url)
        if place.get("@type") == "VirtualLocation":
            return _PageDetails(image_url=image_url, elsewhere=True)

        location, elsewhere = _place_location(place)
        return _PageDetails(image_url=image_url, location=location, elsewhere=elsewhere)

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
            # left unset; the event page's schema.org venue fills it in,
            # falling back to a bare "London" only if that is missing too
            location = None

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


class _PageDetails(NamedTuple):
    image_url: str | None = None
    location: Location | None = None
    # Set only on positive evidence — an online-only listing, or a venue
    # demonstrably outside London. These groups are London ones that
    # occasionally post a retreat abroad or a Zoom call; an address we
    # cannot read leaves the event in, rather than silently dropping it.
    elsewhere: bool = False


def _event_place(soup: BeautifulSoup) -> dict | None:
    """The `location` of the page's schema.org Event, if any."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Event":
                continue
            place = node.get("location")
            if isinstance(place, list):
                place = place[0] if place else None
            if isinstance(place, dict):
                return place
    return None


# Meetup writes junk into addressRegion ("17", "al"), so only the locality,
# country and any London postcode in the street address are trusted.
_UK = {"gb", "uk", "united kingdom", "england"}


def _place_location(place: dict) -> tuple[Location | None, bool]:
    """Parse a schema.org Place into a Location, plus whether it sits
    outside London."""
    address = place.get("address")
    if not isinstance(address, dict):
        address = {}

    venue_name = (place.get("name") or "").strip() or None
    region = (address.get("addressRegion") or "").strip()
    street = (address.get("streetAddress") or "").strip()
    city = (address.get("addressLocality") or "").strip()
    country = (address.get("addressCountry") or "").strip()

    # Judged on the address as given, before the tidying below removes
    # anything the London test might rely on.
    text = " ".join(part for part in (venue_name, street, city) if part)
    london = is_london(text)

    # Meetup appends that junk region to the street ("7 Newcourt Street,
    # London, 17") and packs the postcode into the locality.
    street = _drop_suffix(street, region)
    postcode = POSTCODE_RE.search(city)
    if postcode:
        city = city[: postcode.start()].strip(" ,") or city
    known_country = country.lower() in _UK if country else None

    elsewhere = bool((known_country is False) or (city and not london))

    if not (venue_name or street or city):
        return None, elsewhere

    return (
        Location(
            venue_name=venue_name,
            address=street or venue_name or city,
            city=city or ("London" if london else None),
            country="GB" if known_country else None,
        ),
        elsewhere,
    )


def _drop_suffix(text: str, suffix: str) -> str:
    """Drop a trailing ", <suffix>" from an address."""
    if suffix and text.lower().rstrip(" ,").endswith(suffix.lower()):
        trimmed = text.rstrip(" ,")[: -len(suffix)]
        return trimmed.strip(" ,") or text
    return text


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
