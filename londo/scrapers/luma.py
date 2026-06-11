from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import icalendar

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

PLACE_ID = "discplace-QCcNk3HXowOR97j"  # London
DISCOVER_URL = "https://api.lu.ma/discover/get-paginated-events"
ICS_URL = f"https://api.luma.com/ics/get?entity=discover&id={PLACE_ID}"
PAGE_SIZE = 50


class LumaScraper(BaseScraper):
    """Scrapes luma.com/london.

    The unofficial discover API provides structured listings (times, geo,
    cover images, ticket info) but no descriptions; the public iCal feed
    provides descriptions. The two are merged by event api_id.
    """

    source_name = "luma"

    def scrape(self) -> list[Event]:
        descriptions = self._fetch_descriptions()
        logger.info("Loaded %d descriptions from iCal feed", len(descriptions))

        events: list[Event] = []
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
                    events.append(self._build_event(entry, descriptions))
                except Exception:
                    logger.exception(
                        "Failed to parse entry %s", entry.get("api_id")
                    )

            if not data.get("has_more") or not data.get("next_cursor"):
                break
            cursor = data["next_cursor"]

        logger.info("Scraped %d events from Luma", len(events))
        return events

    def _fetch_descriptions(self) -> dict[str, str]:
        """Map event api_id -> plain-text description from the iCal feed."""
        response = self.get(ICS_URL)
        cal = icalendar.Calendar.from_ical(response.text)

        descriptions: dict[str, str] = {}
        for component in cal.walk("VEVENT"):
            uid = str(component.get("UID", ""))  # evt-XXX@events.lu.ma
            api_id = uid.split("@")[0]
            desc = str(component.get("DESCRIPTION", ""))
            if api_id and desc:
                descriptions[api_id] = _clean_ics_description(desc)
        return descriptions

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
