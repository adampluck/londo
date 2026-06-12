from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper
from londo.scrapers.dandelion import DandelionScraper
from londo.scrapers.eventbrite import BROWSER_UA, build_events
from londo.scrapers.luma import build_event_from_event_api

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

LUMA_RE = re.compile(
    r"https?://(?:www\.)?(?:luma\.com|lu\.ma)/([A-Za-z0-9_-]+)", re.I
)
LUMA_NON_EVENT_PATHS = {
    "discover", "user", "signin", "create", "pricing", "explore", "london",
}
EVENTBRITE_RE = re.compile(
    r"https?://(?:www\.)?eventbrite\.[a-z.]+/e/(?:[^/?#]*?-)?(\d{8,})", re.I
)
DANDELION_RE = re.compile(
    r"https?://(?:www\.)?dandelion\.events/(?:events|e)/([A-Za-z0-9-]+)", re.I
)

EVENTBRITE_DEST_API = (
    "https://www.eventbrite.co.uk/api/v3/destination/events/"
    "?event_ids={ids}&expand=series,primary_venue,ticket_availability,"
    "image,primary_organizer&page_size=50"
)
LUMA_EVENT_API = "https://api.lu.ma/url?url={slug}"

# Domains that never host event pages — don't bother fetching.
SKIP_DOMAINS = (
    "chat.whatsapp.com", "wa.me", "instagram.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "tiktok.com", "facebook.com", "spotify.com",
    "maps.google.com", "goo.gl", "maps.app.goo.gl", "forms.gle",
    "docs.google.com", "drive.google.com", "linkedin.com", "t.me",
    "amazon.co.uk", "amazon.com",
)


def _is_private_host(url: str) -> bool:
    """True for localhost/private/link-local hosts — submitted URLs must
    not be able to point the fetcher at internal addresses (SSRF)."""
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".local"):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False  # a domain name, not an IP literal


def classify_url(url: str) -> tuple[str, str] | None:
    """Return (kind, key) for a URL, or None if it can't be an event link."""
    if any(d in url.lower() for d in SKIP_DOMAINS):
        return None
    if _is_private_host(url):
        return None
    m = LUMA_RE.match(url)
    if m and m.group(1).lower() not in LUMA_NON_EVENT_PATHS:
        return ("luma", m.group(1))
    m = EVENTBRITE_RE.match(url)
    if m:
        return ("eventbrite", m.group(1))
    m = DANDELION_RE.match(url)
    if m:
        return ("dandelion", m.group(1))
    if url.lower().startswith("http"):
        return ("other", url)
    return None


class LinkFetcher(BaseScraper):
    """Fetches canonical event data for a single shared URL."""

    source_name = "links"

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self.session.headers.update({"User-Agent": BROWSER_UA})
        self._dandelion = DandelionScraper(rate_limit=rate_limit)

    def scrape(self) -> list[Event]:
        raise NotImplementedError("LinkFetcher is driven per-URL via fetch()")

    def fetch(self, url: str) -> list[Event]:
        """Fetch events for a URL. Returns [] when the link isn't an event
        or lacks the required details (date, time, location)."""
        classified = classify_url(url)
        if classified is None:
            logger.debug("Skipping non-event URL: %s", url)
            return []
        kind, key = classified
        try:
            if kind == "luma":
                return self._fetch_luma(key)
            if kind == "eventbrite":
                return self._fetch_eventbrite(key)
            if kind == "dandelion":
                return [self._dandelion.scrape_event_url(url)]
            return self._fetch_generic(url)
        except requests.RequestException as exc:
            logger.warning("Could not fetch %s link %s: %s", kind, url, exc)
            return []
        except Exception:
            logger.exception("Failed to fetch %s link: %s", kind, url)
            return []

    def _fetch_luma(self, slug: str) -> list[Event]:
        data = self.get(LUMA_EVENT_API.format(slug=slug)).json().get("data") or {}
        event = build_event_from_event_api(data, slug)
        return [event] if event is not None else []

    def _fetch_eventbrite(self, event_id: str) -> list[Event]:
        data = self.get(EVENTBRITE_DEST_API.format(ids=event_id)).json()
        events: list[Event] = []
        for ev in data.get("events", []):
            events.extend(build_events(ev, source_name="eventbrite"))
        return events

    def _fetch_generic(self, url: str) -> list[Event]:
        """Last resort: fetch the page and look for schema.org Event JSON-LD.
        Only emits an event when date+time and a physical location are present."""
        response = self.get(url)
        if len(response.content) > 3_000_000:
            logger.info("Page too large to be an event page: %s", url)
            return []
        soup = BeautifulSoup(response.text, "html.parser")

        events = []
        for ld in _iter_json_ld_events(soup):
            event = self._build_from_json_ld(ld, url, soup)
            if event is not None:
                events.append(event)
        if not events:
            logger.info("No usable schema.org Event found at %s", url)
        return events

    def _build_from_json_ld(
        self, ld: dict, url: str, soup: BeautifulSoup
    ) -> Event | None:
        title = _text(ld.get("name"))
        start = _parse_ld_datetime(ld.get("startDate"))
        location, is_online = _parse_ld_location(ld.get("location"))
        if not title or start is None or (location is None and not is_online):
            logger.info(
                "Incomplete event data at %s (title/start/location missing)", url
            )
            return None

        image = ld.get("image")
        if isinstance(image, list):
            image = image[0] if image else None
        if isinstance(image, dict):
            image = image.get("url")
        if not image:
            og = soup.find("meta", property="og:image")
            image = og["content"] if og and og.get("content") else None

        offers = ld.get("offers")
        if isinstance(offers, dict):
            offers = [offers]
        price_tiers = []
        for offer in offers or []:
            try:
                price_tiers.append(
                    PriceTier(
                        name=_text(offer.get("name")) or "Ticket",
                        amount=Decimal(str(offer.get("price", 0))),
                        currency=offer.get("priceCurrency") or "GBP",
                    )
                )
            except Exception:
                continue

        org = ld.get("organizer")
        if isinstance(org, list):
            org = org[0] if org else None
        organizer = None
        if isinstance(org, dict) and _text(org.get("name")):
            organizer = Organizer(name=_text(org.get("name")), url=org.get("url"))

        description = _text(ld.get("description"))
        if description:
            description = BeautifulSoup(description, "html.parser").get_text(
                " ", strip=True
            )

        return Event(
            source="other",
            source_id=hashlib.sha1(url.encode()).hexdigest()[:16],
            source_url=url,
            title=title,
            description=description or None,
            start_datetime=start,
            end_datetime=_parse_ld_datetime(ld.get("endDate")),
            location=location,
            is_online=is_online,
            image_url=image,
            price_tiers=price_tiers,
            is_free=bool(price_tiers) and all(t.amount == 0 for t in price_tiers),
            organizer=organizer,
            scraped_at=datetime.now(timezone.utc),
        )


def _iter_json_ld_events(soup: BeautifulSoup):
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for item in node.get("@graph", [node]):
                if isinstance(item, dict) and "Event" in str(item.get("@type", "")):
                    yield item


def _text(value) -> str | None:
    if isinstance(value, dict):
        value = value.get("name") or value.get("@value")
    return str(value).strip() if value else None


def _parse_ld_datetime(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LONDON)
    return dt


def _parse_ld_location(loc) -> tuple[Location | None, bool]:
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return None, False
    if "VirtualLocation" in str(loc.get("@type", "")):
        return None, True

    addr = loc.get("address")
    if isinstance(addr, dict):
        parts = [
            addr.get("streetAddress"),
            addr.get("addressLocality"),
            addr.get("postalCode"),
        ]
        address = ", ".join(p for p in parts if p) or addr.get("name", "")
        city = addr.get("addressLocality")
    else:
        address = str(addr) if addr else ""
        city = None

    venue_name = _text(loc.get("name"))
    if not address and not venue_name:
        return None, False

    geo = loc.get("geo") or {}
    return (
        Location(
            venue_name=venue_name,
            address=address or venue_name or "London, UK",
            city=city or "London",
            latitude=_to_float(geo.get("latitude")),
            longitude=_to_float(geo.get("longitude")),
        ),
        False,
    )


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

