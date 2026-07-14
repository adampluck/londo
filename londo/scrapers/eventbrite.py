from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

SHOWMORE_URL = (
    "https://www.eventbrite.co.uk/org/{org_id}/showmore/"
    "?page_size=50&type=future&page={page}"
)
DEST_API = (
    "https://www.eventbrite.co.uk/api/v3/destination/events/"
    "?event_ids={ids}&expand=series,primary_venue,ticket_availability"
    "&page_size=50"
)
BATCH = 50

# Eventbrite's WAF rejects non-browser user agents.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class EventbriteOrganizerScraper(BaseScraper):
    """Scrapes a single Eventbrite organizer's upcoming events.

    The organizer 'showmore' endpoint lists upcoming events (including
    series parents) with images and descriptions; the destination API
    expands each series into its next concrete occurrences.

    Configure via subclass attributes (see NuminityScraper) or constructor
    arguments (see EventbriteListingsScraper).
    """

    org_id: str
    org_name: str

    def __init__(
        self,
        rate_limit: float = 1.0,
        org_id: str | None = None,
        org_name: str | None = None,
        source_name: str | None = None,
    ):
        super().__init__(rate_limit=rate_limit)
        if org_id:
            self.org_id = org_id
        if org_name:
            self.org_name = org_name
        if source_name:
            self.source_name = source_name
        self.session.headers.update({"User-Agent": BROWSER_UA})

    def scrape(self) -> list[Event]:
        listings = self._fetch_listings()
        logger.info("Found %d listed events for %s", len(listings), self.org_name)
        if not listings:
            return []

        events: list[Event] = []
        ids = list(listings.keys())
        for i in range(0, len(ids), BATCH):
            url = DEST_API.format(ids=",".join(ids[i : i + BATCH]))
            data = self.get(url).json()
            for ev in data.get("events", []):
                if not _is_london_inperson(ev):
                    logger.debug("Skipping non-London/online event %s", ev.get("id"))
                    continue
                listing = listings.get(ev["id"], {})
                try:
                    events.extend(
                        build_events(
                            ev,
                            source_name=self.source_name,
                            image_url=listing.get("image_url"),
                            description=listing.get("description"),
                            organizer=Organizer(
                                name=self.org_name,
                                url=f"https://www.eventbrite.co.uk/o/{self.org_id}",
                            ),
                        )
                    )
                except Exception:
                    logger.exception("Failed to parse event %s", ev.get("id"))

        logger.info("Scraped %d events from %s", len(events), self.org_name)
        return events

    def _fetch_listings(self) -> dict[str, dict]:
        """Map event id -> listing extras (image, description) from showmore."""
        listings: dict[str, dict] = {}
        page = 1
        while True:
            url = SHOWMORE_URL.format(org_id=self.org_id, page=page)
            data = self.get(url).json().get("data", {})
            for ev in data.get("events", []):
                desc = ev.get("description") or {}
                listings[str(ev["id"])] = {
                    "image_url": (ev.get("logo") or {}).get("url"),
                    "description": desc.get("text") or ev.get("summary"),
                }
            if not data.get("has_next_page"):
                return listings
            page += 1

class NuminityScraper(EventbriteOrganizerScraper):
    source_name = "numinity"
    org_id = "33797188771"
    org_name = "Numinity"


# Followed Eventbrite organizers, aggregated under the generic
# 'eventbrite' source (Numinity stays its own source above).
EVENTBRITE_ORGANIZERS = {
    "29457876735": "Robyn Wilford",
    "70451924323": "The London School of Tantra",
    "70754628523": "London Night Cafe",
    "62049657303": "The School of Sufi Teaching",
    "8588572090": "Ecstatic Dance London & URUBU Wellbeing Events",
    "4269650797": "The Royal Institution",
    "36463428713": "Seed Talks",
    # psyconnect-leaning organisers (shared events pool)
    "67359356833": "London Psychedelic Community",
    "18247139079": "The Maudsley Psychedelic Society",
    "109324686131": "YOUnited Breath Space",
    "95867879283": "Moon Haven",
    "121274518486": "Gaia Wellbeing Collective CIC",
}

# Greater London bounding box — events outside this are skipped.
_LONDON_BBOX = (51.25, 51.75, -0.6, 0.35)


def _is_london_inperson(ev: dict) -> bool:
    """True only for offline events whose venue is plausibly in London."""
    if ev.get("is_online_event"):
        return False
    addr = (ev.get("primary_venue") or {}).get("address") or {}
    city = (addr.get("city") or "").lower()
    if "london" in city:
        return True
    lat = _to_float(addr.get("latitude"))
    lng = _to_float(addr.get("longitude"))
    if lat is not None and lng is not None:
        min_lat, max_lat, min_lng, max_lng = _LONDON_BBOX
        return min_lat <= lat <= max_lat and min_lng <= lng <= max_lng
    return False


class EventbriteListingsScraper(BaseScraper):
    """Scrapes every organizer in EVENTBRITE_ORGANIZERS as source 'eventbrite'."""

    source_name = "eventbrite"

    def scrape(self) -> list[Event]:
        events: list[Event] = []
        for org_id, org_name in EVENTBRITE_ORGANIZERS.items():
            scraper = EventbriteOrganizerScraper(
                rate_limit=self.rate_limit,
                org_id=org_id,
                org_name=org_name,
                source_name=self.source_name,
            )
            try:
                events.extend(scraper.scrape())
            except Exception:
                logger.exception("Organizer %s (%s) failed", org_name, org_id)
        logger.info(
            "Scraped %d events across %d Eventbrite organizers",
            len(events),
            len(EVENTBRITE_ORGANIZERS),
        )
        return events


def build_events(
    ev: dict,
    *,
    source_name: str,
    image_url: str | None = None,
    description: str | None = None,
    organizer: Organizer | None = None,
) -> list[Event]:
    """Build Events from a destination-API event dict.

    Series parents yield one Event per upcoming occurrence; plain events
    yield a single Event with their local start/end times.
    """
    if ev.get("is_cancelled"):
        return []

    if organizer is None:
        org = ev.get("primary_organizer") or {}
        if org.get("name"):
            organizer = Organizer(name=org["name"], url=org.get("url"))

    if image_url is None:
        image_url = (ev.get("image") or {}).get("url")

    base = _build_base(
        ev,
        source_name=source_name,
        image_url=image_url,
        description=description,
        organizer=organizer,
    )
    next_dates = (ev.get("series") or {}).get("next_dates") or []

    if not next_dates:
        start, end = _parse_local_times(ev)
        if start is None:
            logger.warning("Event %s has no usable start time", ev.get("id"))
            return []
        return [base.model_copy(update={"start_datetime": start, "end_datetime": end})]

    return [
        base.model_copy(
            update={
                "source_id": str(nd["id"]),
                "external_ref": f"eventbrite:{nd['id']}",
                "start_datetime": _parse_utc(nd["start"]),
                "end_datetime": _parse_utc(nd.get("end")),
            }
        )
        for nd in next_dates
    ]


def _build_base(
    ev: dict,
    *,
    source_name: str,
    image_url: str | None,
    description: str | None,
    organizer: Organizer | None,
) -> Event:
    venue = ev.get("primary_venue") or {}
    addr = venue.get("address") or {}
    location = None
    if venue:
        location = Location(
            venue_name=venue.get("name"),
            address=addr.get("localized_address_display")
            or addr.get("localized_area_display")
            or "London, UK",
            city=addr.get("city") or "London",
            country=addr.get("country"),
            latitude=_to_float(addr.get("latitude")),
            longitude=_to_float(addr.get("longitude")),
        )

    ta = ev.get("ticket_availability") or {}
    is_free = bool(ta.get("is_free"))
    price_tiers = []
    for key, name in (("minimum_ticket_price", "From"), ("maximum_ticket_price", "To")):
        p = ta.get(key) or {}
        if p.get("major_value") is not None:
            price_tiers.append(
                PriceTier(
                    name=name,
                    amount=Decimal(p["major_value"]),
                    currency=p.get("currency", "GBP"),
                )
            )

    tags = [
        t["display_name"]
        for t in ev.get("tags") or []
        if isinstance(t, dict) and t.get("display_name")
    ]

    return Event(
        source=source_name,
        source_id=str(ev["id"]),
        source_url=ev.get("url") or (ev.get("series") or {}).get("url", ""),
        external_ref=f"eventbrite:{ev['id']}",
        title=ev.get("name", ""),
        description=description or ev.get("summary"),
        short_description=ev.get("summary"),
        location=location,
        is_online=bool(ev.get("is_online_event")),
        image_url=image_url,
        tags=tags,
        price_tiers=price_tiers,
        is_free=is_free,
        organizer=organizer,
        scraped_at=datetime.now(timezone.utc),
    )


def _parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_local_times(ev: dict) -> tuple[datetime | None, datetime | None]:
    tz_name = ev.get("timezone")
    start_date, start_time = ev.get("start_date"), ev.get("start_time")
    if not (tz_name and start_date and start_time):
        return None, None
    tz = ZoneInfo(tz_name)
    start = datetime.fromisoformat(f"{start_date}T{start_time}").replace(tzinfo=tz)
    end = None
    if ev.get("end_date") and ev.get("end_time"):
        end = datetime.fromisoformat(
            f"{ev['end_date']}T{ev['end_time']}"
        ).replace(tzinfo=tz)
    return start, end


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
