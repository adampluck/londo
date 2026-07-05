from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import icalendar

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

PLACE_ID = "discplace-QCcNk3HXowOR97j"  # London
DISCOVER_URL = "https://api.lu.ma/discover/get-paginated-events"
CALENDAR_ITEMS_URL = "https://api.lu.ma/calendar/get-items"
ICS_URL = f"https://api.luma.com/ics/get?entity=discover&id={PLACE_ID}"
EVENT_API = "https://api.lu.ma/url?url={slug}"
PROFILE_URL = "https://api.luma.com/user/profile?username={username}"
USER_EVENTS_URL = "https://api.luma.com/user/profile/events-hosting"
PAGE_SIZE = 50

SLUG_RE = re.compile(r"https?://(?:www\.)?(?:luma\.com|lu\.ma)/([A-Za-z0-9_-]+)")

# Extra Luma calendars to scrape in addition to the London discover feed.
# Add slugs from luma.com/<slug> calendar pages here.
EXTRA_CALENDARS = [
    "unseen",
    "cml",
]

# Luma user profiles whose hosted events are scraped in addition to the
# feeds above (luma.com/user/<username>). PsyConnect London is our own
# organizer account — the psyconnect site features its next event.
EXTRA_USERS = [
    "psyconnect",
]


class LumaScraper(BaseScraper):
    """Scrapes luma.com/london.

    Two listings are merged by event api_id: the unofficial discover API
    (structured: times, geo, cover images, ticket info) and the public
    iCal feed (descriptions). Neither is reliably a superset of the other,
    so events only present in the iCal are fetched individually via the
    per-event API (the iCal description embeds each event's URL slug).
    """

    source_name = "luma"

    def scrape(self) -> list[Event]:
        ics_info = self._parse_ics()
        descriptions = {
            api_id: info["description"]
            for api_id, info in ics_info.items()
            if info.get("description")
        }
        logger.info("Loaded %d events from iCal feed", len(ics_info))

        events: list[Event] = []
        seen: set[str] = set()
        cursor: str | None = None
        while True:
            params = {
                "discover_place_api_id": PLACE_ID,
                "pagination_limit": str(PAGE_SIZE),
            }
            if cursor:
                params["pagination_cursor"] = cursor

            data = self.get(f"{DISCOVER_URL}?{urlencode(params)}").json()
            entries = data.get("entries", [])
            for entry in entries:
                try:
                    event = self._build_event(entry, descriptions)
                    events.append(event)
                    seen.add(event.source_id)
                except Exception:
                    logger.exception(
                        "Failed to parse entry %s", entry.get("api_id")
                    )

            if not data.get("has_more") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]

        n_api = len(events)
        for api_id, info in ics_info.items():
            if api_id in seen:
                continue
            event = self._fetch_ics_only_event(api_id, info)
            if event is not None:
                events.append(event)
        if len(events) > n_api:
            logger.info(
                "Added %d events present only in the iCal feed",
                len(events) - n_api,
            )

        # also add iCal-only source_ids so extra-calendar dedup is complete
        for e in events[n_api:]:
            seen.add(e.source_id)
        for slug in EXTRA_CALENDARS:
            cal_events = self._scrape_calendar(slug, descriptions)
            for e in cal_events:
                if e.source_id not in seen:
                    events.append(e)
                    seen.add(e.source_id)
        for username in EXTRA_USERS:
            for e in self._scrape_user(username, descriptions):
                if e.source_id in seen:
                    # the discover/calendar copy of this event carries the
                    # host's personal-calendar name ("Personal"); the profile
                    # identity is what sites key featured events on
                    existing = next(
                        x for x in events if x.source_id == e.source_id
                    )
                    existing.organizer = e.organizer
                else:
                    events.append(e)
                    seen.add(e.source_id)
        logger.info("Scraped %d events from Luma", len(events))
        return events

    def _scrape_user(
        self, username: str, descriptions: dict[str, str]
    ) -> list[Event]:
        """Fetch upcoming events hosted by a Luma user (luma.com/user/<name>)."""
        profile = self.get(PROFILE_URL.format(username=username)).json()
        user = profile.get("user") or {}
        user_api_id = user.get("api_id")
        if not user_api_id:
            logger.warning("Could not resolve Luma user '%s'", username)
            return []

        organizer = Organizer(
            name=user.get("name") or username,
            url=f"https://luma.com/user/{username}",
        )
        events: list[Event] = []
        cursor: str | None = None
        while True:
            params = {
                "user_api_id": user_api_id,
                "period": "future",
                "pagination_limit": str(PAGE_SIZE),
            }
            if cursor:
                params["pagination_cursor"] = cursor
            resp = self.get(f"{USER_EVENTS_URL}?{urlencode(params)}").json()
            for entry in resp.get("entries", []):
                try:
                    event = self._build_event(entry, descriptions)
                    # entries surface the host's personal calendar ("Personal")
                    # as organizer — the public profile identity is the one
                    # that means anything downstream
                    event.organizer = organizer
                    events.append(event)
                except Exception:
                    logger.exception(
                        "Failed to parse hosted event from '%s'", username
                    )
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]

        logger.info(
            "Got %d upcoming events from Luma user '%s'", len(events), username
        )
        return events

    def _scrape_calendar(self, slug: str, descriptions: dict[str, str]) -> list[Event]:
        """Fetch events from a specific Luma calendar (luma.com/<slug>).

        Uses /calendar/get-items which actually filters by calendar_api_id,
        unlike the discover endpoint which ignores that parameter.
        """
        data = self.get(EVENT_API.format(slug=slug)).json()
        cal = (data.get("data") or {}).get("calendar") or {}
        cal_api_id = cal.get("api_id")
        if not cal_api_id:
            logger.warning("Could not resolve Luma calendar slug '%s'", slug)
            return []

        logger.info("Scraping Luma calendar '%s' (%s)", slug, cal_api_id)
        events: list[Event] = []
        cursor: str | None = None
        while True:
            params = {
                "calendar_api_id": cal_api_id,
                "pagination_limit": str(PAGE_SIZE),
            }
            if cursor:
                params["pagination_cursor"] = cursor
            resp = self.get(f"{CALENDAR_ITEMS_URL}?{urlencode(params)}").json()
            for entry in resp.get("entries", []):
                try:
                    events.append(self._build_event(entry, descriptions))
                except Exception:
                    logger.exception("Failed to parse calendar entry from '%s'", slug)
            if not resp.get("has_more") or not resp.get("next_cursor"):
                break
            cursor = resp["next_cursor"]

        logger.info("Got %d events from Luma calendar '%s'", len(events), slug)
        return events

    def _parse_ics(self) -> dict[str, dict]:
        """Map event api_id -> {description, slug, component} from iCal."""
        response = self.get(ICS_URL)
        cal = icalendar.Calendar.from_ical(response.text)

        info: dict[str, dict] = {}
        for component in cal.walk("VEVENT"):
            uid = str(component.get("UID", ""))  # evt-XXX@events.lu.ma
            api_id = uid.split("@")[0]
            if not api_id:
                continue
            desc = str(component.get("DESCRIPTION", ""))
            slug_match = SLUG_RE.search(desc)
            info[api_id] = {
                "description": _clean_ics_description(desc) if desc else None,
                "slug": slug_match.group(1) if slug_match else None,
                "component": component,
            }
        return info

    def _fetch_ics_only_event(self, api_id: str, info: dict) -> Event | None:
        """Full-detail fetch for an event the discover API didn't list,
        falling back to bare iCal fields if the per-event API fails."""
        slug = info.get("slug")
        if slug:
            try:
                data = self.get(EVENT_API.format(slug=slug)).json().get("data") or {}
                event = build_event_from_event_api(data, slug)
                if event is not None:
                    return event
            except Exception:
                logger.warning("Per-event fetch failed for %s", slug)
        return self._event_from_ics(api_id, info)

    def _event_from_ics(self, api_id: str, info: dict) -> Event | None:
        component = info["component"]
        start = component.get("DTSTART")
        if start is None or not isinstance(start.dt, datetime):
            return None
        end = component.get("DTEND")
        geo = component.get("GEO")
        location_str = str(component.get("LOCATION", "")).strip()
        slug = info.get("slug")

        location = None
        if location_str:
            location = Location(
                venue_name=location_str.split(",")[0],
                address=location_str,
                city="London",
                latitude=getattr(geo, "latitude", None) if geo else None,
                longitude=getattr(geo, "longitude", None) if geo else None,
            )

        return Event(
            source="luma",
            source_id=api_id,
            source_url=f"https://luma.com/{slug}" if slug else "https://luma.com/london",
            external_ref=f"luma:{slug}" if slug else None,
            title=str(component.get("SUMMARY", "")).strip(),
            description=info.get("description"),
            start_datetime=start.dt,
            end_datetime=end.dt if end is not None and isinstance(end.dt, datetime) else None,
            location=location,
            scraped_at=datetime.now(timezone.utc),
        )

    def _build_event(self, entry: dict, descriptions: dict[str, str]) -> Event:
        ev = entry["event"]
        api_id = ev["api_id"]
        slug = ev.get("url", "")
        ticket = entry.get("ticket_info") or {}
        geo = ev.get("geo_address_info") or {}
        coord = ev.get("coordinate") or {}
        hosts = entry.get("hosts") or []
        calendar = entry.get("calendar") or {}

        start_at = _parse_iso(ev.get("start_at"))
        end_at = _parse_iso(ev.get("end_at"))

        location = build_location(geo, coord)

        is_free = bool(ticket.get("is_free"))
        price_tiers: list[PriceTier] = []
        price = ticket.get("price")
        if isinstance(price, (int, float)):
            price_tiers.append(
                PriceTier(name="Ticket", amount=Decimal(price) / 100)
            )

        organizer = None
        org_name = calendar.get("name") or (hosts[0].get("name") if hosts else None)
        if org_name:
            org_slug = calendar.get("slug")
            organizer = Organizer(
                name=org_name,
                url=f"https://luma.com/{org_slug}" if org_slug else None,
            )

        return Event(
            source="luma",
            source_id=api_id,
            source_url=f"https://luma.com/{slug}",
            external_ref=f"luma:{slug}" if slug else None,
            title=ev.get("name", ""),
            description=descriptions.get(api_id),
            start_datetime=start_at,
            end_datetime=end_at,
            location=location,
            is_online=ev.get("location_type") != "offline",
            image_url=ev.get("cover_url") or ev.get("social_image_url"),
            price_tiers=price_tiers,
            is_free=is_free,
            organizer=organizer,
            scraped_at=datetime.now(timezone.utc),
        )


def build_event_from_event_api(data: dict, slug: str) -> Event | None:
    """Build a full Event from the api.lu.ma/url per-event payload."""
    ev = data.get("event") or {}
    if not ev.get("start_at"):
        return None

    description = flatten_description_mirror(data.get("description_mirror"))
    hosts = data.get("hosts") or []
    calendar = data.get("calendar") or {}
    org_name = calendar.get("name") or (hosts[0].get("name") if hosts else None)

    ticket_types = data.get("ticket_types") or []
    price_tiers = []
    for t in ticket_types:
        cents = (t.get("cents") if isinstance(t, dict) else None) or 0
        price_tiers.append(
            PriceTier(name=t.get("name") or "Ticket", amount=Decimal(cents) / 100)
        )
    is_free = bool(ticket_types) and all(t.amount == 0 for t in price_tiers)

    return Event(
        source="luma",
        source_id=ev["api_id"],
        source_url=f"https://luma.com/{ev.get('url', slug)}",
        external_ref=f"luma:{ev.get('url', slug)}",
        title=ev.get("name", ""),
        description=description or None,
        start_datetime=_parse_iso(ev.get("start_at")),
        end_datetime=_parse_iso(ev.get("end_at")),
        location=build_location(
            ev.get("geo_address_info") or {}, ev.get("coordinate") or {}
        ),
        is_online=ev.get("location_type") != "offline",
        image_url=ev.get("cover_url") or ev.get("social_image_url"),
        price_tiers=price_tiers,
        is_free=is_free,
        organizer=Organizer(name=org_name) if org_name else None,
        scraped_at=datetime.now(timezone.utc),
    )


def build_location(geo: dict, coord: dict) -> Location | None:
    if not geo:
        return None
    localized = (geo.get("localized") or {}).get("en-GB") or {}
    return Location(
        venue_name=geo.get("address"),
        address=localized.get("full_address")
        or geo.get("full_address")
        or geo.get("city_state")
        or "London, UK",
        city=geo.get("city") or "London",
        country=geo.get("country"),
        latitude=coord.get("latitude"),
        longitude=coord.get("longitude"),
    )


def flatten_description_mirror(node) -> str:
    """Flatten Luma's ProseMirror description document to plain text."""
    out: list[str] = []

    def walk(n) -> None:
        if not isinstance(n, dict):
            return
        if n.get("type") == "text":
            out.append(n.get("text", ""))
        for child in n.get("content") or []:
            walk(child)
        if n.get("type") in ("paragraph", "heading", "bullet_list_item"):
            out.append("\n")

    walk(node)
    return "".join(out).strip()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _clean_ics_description(desc: str) -> str:
    """Strip Luma's boilerplate (info link, address block, hosted-by footer)."""
    chunks = [c.strip() for c in desc.split("\n\n")]
    kept = []
    skip_next_address = False
    for chunk in chunks:
        if not chunk:
            continue
        if chunk.startswith("Get up-to-date information at:"):
            continue
        if chunk.startswith("Address:"):
            # address body may be in the same chunk or the next one
            if chunk == "Address:":
                skip_next_address = True
            continue
        if skip_next_address:
            skip_next_address = False
            continue
        if chunk.startswith("Hosted by "):
            continue
        kept.append(chunk)
    return "\n\n".join(kept).strip()
