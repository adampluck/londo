from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag
from curl_cffi import requests as curl_requests

from londo.geo import is_london
from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

# Ticket Tailor box offices to list wholesale, slug -> display name.
# The slug is the path segment after tickettailor.com/events/ (or
# buytickets.at/). The Study Society is deliberately absent: it comes in
# via its own site's calendar widget (studysociety scraper) and any of
# its Ticket Tailor links shared in chat dedupe onto those rows.
TICKET_TAILOR_BOX_OFFICES: dict[str, str] = {}

# Ticket Tailor serves every box office under two hostnames. Cloudflare
# sits in front of both with a bot-score challenge that keys on the TLS/
# HTTP2 fingerprint rather than a JS puzzle, so a browser-impersonating
# client gets through — but which (host, browser) pairing passes varies
# from run to run and by network. Rotate through them until one lands.
HOSTS = ("https://www.tickettailor.com/events/", "https://buytickets.at/")
PROFILES = ("chrome", "safari", "chrome131", "firefox", "safari_ios")

EVENT_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:tickettailor\.com/events|buytickets\.at)"
    r"/([A-Za-z0-9_-]+)/(\d+)",
    re.I,
)
EVENT_PATH_RE = re.compile(r"/(\d+)(?:\?date=(\d{4}-\d{2}-\d{2}))?")

# recurring classes repeat forever; expand a little past the frontend's
# 30-day window so it never runs dry between scrapes
HORIZON_DAYS = 35

# The page's schema.org offers carry the ticket types and prices
# (offer_prices); failing those, any £ amount the organiser wrote into the
# description.
PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
# descriptions here use "free" loosely ("free movement", a host surnamed
# Free), so only an explicit no-charge phrasing counts
FREE_RE = re.compile(
    r"\bfree\s+(?:entry|event|admission|to\s+attend)\b"
    r"|\bentry\s+is\s+free\b|\bfree\s+of\s+charge\b",
    re.I,
)


class Blocked(Exception):
    """Every host/browser pairing was refused for this path."""


class NotFound(Exception):
    """The box office or event no longer exists (404 on both hosts)."""


class TicketTailorClient(BaseScraper):
    """Fetches Ticket Tailor pages through the Cloudflare wall.

    Paths are relative to a box office ("<slug>/<id>?date=..."); each is
    tried across HOSTS × PROFILES until a 200 comes back.
    """

    source_name = "tickettailor"

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self._curl = curl_requests.Session()
        self._order = list(PROFILES)

    def fetch_path(self, path: str) -> str:
        last_status = None
        not_found = False
        for host in HOSTS:
            for profile in list(self._order):
                url = host + path
                elapsed = time.time() - self._last_request_time
                if elapsed < self.rate_limit:
                    time.sleep(self.rate_limit - elapsed)
                logger.debug("GET %s [%s]", url, profile)
                try:
                    response = self._curl.get(
                        url, impersonate=profile, timeout=30
                    )
                except curl_requests.RequestsError as exc:
                    logger.warning("%s [%s]: %s", url, profile, exc)
                    continue
                finally:
                    self._last_request_time = time.time()
                last_status = response.status_code
                if response.status_code == 200:
                    # a pairing that passed once tends to keep passing
                    # within the run; try it first next time
                    self._order.remove(profile)
                    self._order.insert(0, profile)
                    return response.text
                if response.status_code == 404:
                    not_found = True
                logger.debug("%s [%s] -> %s", url, profile, response.status_code)
        if not_found:
            raise NotFound(f"Ticket Tailor has no {path}")
        raise Blocked(f"Ticket Tailor refused {path} (last status {last_status})")

    def scrape(self) -> list[Event]:
        raise NotImplementedError("Use TicketTailorBoxOfficeScraper")

    # -- per-URL fetch (chat links / seeds) ---------------------------------

    def scrape_event_url(self, url: str) -> list[Event]:
        """Events for a single event link, one per upcoming occurrence.

        Recurring events link without a date; their select-date page lists
        the occurrences, which are expanded up to HORIZON_DAYS out.
        """
        m = EVENT_URL_RE.match(url)
        if not m:
            return []
        slug, event_id = m.group(1), m.group(2)
        detail = _parse_detail(self.fetch_path(f"{slug}/{event_id}"))
        if detail is None:
            return []
        slots = detail.pop("slots")
        if slots is None:  # "Multiple dates and times"
            slots = _parse_select_date(
                self.fetch_path(f"{slug}/{event_id}/select-date")
            )
        return _build_events(slug, event_id, detail, slots)


class TicketTailorBoxOfficeScraper(TicketTailorClient):
    """Scrapes one box office: the listing page is the occurrence truth
    (one card per date with times and venue); each distinct event's page
    is fetched once for the description, cover image and address."""

    def __init__(self, slug: str, name: str, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self.slug = slug
        self.name = name

    def scrape(self) -> list[Event]:
        soup = BeautifulSoup(self.fetch_path(self.slug), "html.parser")
        cards = soup.select("li.main-events-listing__event")
        logger.info("%s lists %d occurrences", self.name, len(cards))

        by_event: dict[str, list[dict]] = {}
        for card in cards:
            slot = _parse_card(card)
            if slot:
                by_event.setdefault(slot["event_id"], []).append(slot)

        events: list[Event] = []
        for event_id, slots in by_event.items():
            try:
                detail = _parse_detail(self.fetch_path(f"{self.slug}/{event_id}"))
            except (Blocked, NotFound) as exc:
                logger.warning("Skipping %s/%s: %s", self.slug, event_id, exc)
                continue
            if detail is None:
                continue
            detail.pop("slots")  # listing cards carry the dates
            if any(slot["start"] is None for slot in slots):
                card = slots[0]
                try:
                    dates = _parse_select_date(
                        self.fetch_path(f"{self.slug}/{event_id}/select-date")
                    )
                except (Blocked, NotFound) as exc:
                    logger.warning("Skipping %s/%s dates: %s", self.slug, event_id, exc)
                    continue
                slots = [{**card, **d} for d in dates]
            detail["organizer"] = self.name or detail["organizer"]
            built = _build_events(self.slug, event_id, detail, slots)
            events.extend(built)
            if built:
                logger.info("Scraped: %s (%d occurrence(s))", built[0].title, len(built))
        logger.info("Kept %d upcoming events for %s", len(events), self.name)
        return events


class TicketTailorScraper(BaseScraper):
    """Scrapes every box office in TICKET_TAILOR_BOX_OFFICES as source
    'tickettailor'."""

    source_name = "tickettailor"

    def scrape(self) -> list[Event]:
        events: list[Event] = []
        for slug, name in TICKET_TAILOR_BOX_OFFICES.items():
            scraper = TicketTailorBoxOfficeScraper(slug, name, rate_limit=self.rate_limit)
            try:
                events.extend(scraper.scrape())
            except Exception:
                logger.exception("Box office %s (%s) failed", name, slug)
        logger.info(
            "Scraped %d events across %d Ticket Tailor box offices",
            len(events),
            len(TICKET_TAILOR_BOX_OFFICES),
        )
        return events


# -- parsing -----------------------------------------------------------------

MONTHS = {
    m: i
    for i, m in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1
    )
}
# "Sat 18 Jul 2026 9:30 PM - 7:00 AM" / "Mon 14 Sep 2026 6:00 PM"
WHEN_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]{3})\w*\s+(\d{4})\s+(\d{1,2}:\d{2}\s*[AP]M)"
    r"(?:\s*-\s*(\d{1,2}:\d{2}\s*[AP]M))?",
    re.I,
)


def _parse_when(text: str) -> tuple[datetime, datetime | None] | None:
    m = WHEN_RE.search(text)
    if not m:
        return None
    day = date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    start = _at(day, m.group(4))
    end = _at(day, m.group(5)) if m.group(5) else None
    if end is not None and end <= start:  # crosses midnight
        end += timedelta(days=1)
    return start, end


def _at(day: date, clock: str) -> datetime:
    t = datetime.strptime(clock.replace(" ", "").upper(), "%I:%M%p").time()
    return datetime.combine(day, t, tzinfo=LONDON).astimezone(timezone.utc)


def _parse_card(card: Tag) -> dict | None:
    link = card.select_one("a.event__link")
    when = card.select_one(".event-meta__date")
    if not link or not when:
        return None
    pm = EVENT_PATH_RE.search(link.get("href") or "")
    if not pm:
        return None
    # box offices can collapse a recurring event into one undated card
    # ("Multiple dates and times"); its select-date page has the dates
    start, end = _parse_when(when.get_text(" ", strip=True)) or (None, None)
    loc = card.select_one(".event-meta__location")
    img = card.select_one(".event__image img")
    return {
        "event_id": pm.group(1),
        "start": start,
        "end": end,
        "title": link.get_text(" ", strip=True),
        "location": loc.get_text(" ", strip=True) if loc else None,
        "image": _image_url(img.get("src")) if img else None,
    }


# Asset URLs carry Cloudinary-style transforms for the slot they render in
# (a 250px listing thumbnail, a square hero crop); swap in a plain resize.
IMAGE_TRANSFORM_RE = re.compile(r"(tickettailorassets\.com).*?(?=/v1/)")


def _image_url(src: str | None) -> str | None:
    if not src:
        return None
    return IMAGE_TRANSFORM_RE.sub(r"\1/c_scale,w_800", src, count=1)


def _parse_detail(html: str) -> dict | None:
    """Title, description, location, image and organiser from an event page.
    'slots' is a one-item list for dated events, None for recurring ones
    ("Multiple dates and times")."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1")
    if h1 is None:
        return None
    desc = soup.select_one(".detail-content__description")
    loc = soup.select_one(".detail-content__location p")
    # og:image falls back to the box office logo when there's no hero
    hero = soup.select_one(".hero__slide-image img")
    when = soup.select_one(".event-meta__date")
    parsed = _parse_when(when.get_text(" ", strip=True)) if when else None

    # <title> is "Select tickets – <event> – <box office name>"
    title_tag = soup.title.get_text(strip=True) if soup.title else ""
    organizer = title_tag.rsplit("–", 1)[1].strip() if "–" in title_tag else None

    return {
        "title": h1.get_text(" ", strip=True),
        "description": desc.get_text(" ", strip=True) if desc else None,
        "location": loc.get_text(" ", strip=True) if loc else None,
        "image": _image_url(hero.get("src")) if hero else None,
        "organizer": organizer,
        "prices": offer_prices(soup),
        "slots": [{"start": parsed[0], "end": parsed[1]}] if parsed else None,
    }


def offer_prices(soup: BeautifulSoup) -> tuple[list[PriceTier], bool] | None:
    """Ticket types from the page's schema.org Event offers, or None when
    it gives none. Recurring events repeat one Event per date with the
    same offers, so each (name, price) counts once."""
    seen: dict[tuple[str, Decimal], PriceTier] = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Event":
                continue
            offers = node.get("offers") or []
            for offer in [offers] if isinstance(offers, dict) else offers:
                try:
                    amount = Decimal(str(offer["price"]))
                except (KeyError, TypeError, ArithmeticError):
                    continue
                name = str(offer.get("name") or "Ticket")
                seen.setdefault(
                    (name, amount),
                    PriceTier(
                        name=name,
                        amount=amount,
                        currency=offer.get("priceCurrency") or "GBP",
                    ),
                )
    if not seen:
        return None
    tiers = sorted(seen.values(), key=lambda t: t.amount)
    if all(t.amount == 0 for t in tiers):
        return [], True
    return tiers, False


def _parse_select_date(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    slots = []
    for occ in soup.select(".occurrence.date_select"):
        parsed = _parse_when(occ.get_text(" ", strip=True))
        if parsed:
            slots.append({"start": parsed[0], "end": parsed[1]})
    return slots


def _build_events(slug: str, event_id: str, detail: dict, slots: list[dict]) -> list[Event]:
    location_text = detail.get("location") or ""
    if not location_text or not is_london(location_text):
        logger.warning("Skipping non-London/online event %s: %r", event_id, location_text)
        return []
    # "Colet House, W14 9DA" / "The Glory, 281 Kingsland Rd, London E2 8AS"
    venue, _, rest = location_text.partition(",")
    if not rest:  # bare address, no venue name
        venue = ""
    location = Location(
        venue_name=venue.strip() or None,
        address=location_text,
        city="London",
        country="GB",
    )
    price_tiers, is_free = detail.get("prices") or price_from_text(
        detail.get("description")
    )
    organizer = detail.get("organizer")

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    horizon = datetime.now(timezone.utc) + timedelta(days=HORIZON_DAYS)
    events = []
    for slot in slots:
        start: datetime = slot["start"]
        if not cutoff <= start <= horizon:
            continue
        day = start.astimezone(LONDON).date()
        events.append(
            Event(
                source="tickettailor",
                source_id=f"{event_id}:{day.isoformat()}",
                source_url=f"{HOSTS[0]}{slug}/{event_id}?date={day.isoformat()}",
                external_ref=external_ref(event_id, day),
                title=slot.get("title") or detail["title"],
                description=detail.get("description"),
                start_datetime=start,
                end_datetime=slot.get("end"),
                start_date=day,
                location=location,
                image_url=slot.get("image") or detail.get("image"),
                price_tiers=price_tiers,
                is_free=is_free,
                organizer=Organizer(name=organizer, url=f"{HOSTS[0]}{slug}")
                if organizer
                else None,
                scraped_at=datetime.now(timezone.utc),
            )
        )
    return events


def price_from_text(text: str | None) -> tuple[list[PriceTier], bool]:
    if not text:
        return [], False
    amounts = sorted({Decimal(m) for m in PRICE_RE.findall(text)})
    tiers = [
        PriceTier(name=f"Tier {i + 1}", amount=amount)
        for i, amount in enumerate(amounts)
    ]
    is_free = not tiers and FREE_RE.search(text) is not None
    return tiers, is_free


def external_ref(event_id: str, day: date) -> str:
    """Shared identity for a Ticket Tailor occurrence, so the same date
    reached via a box office, a chat link, or an organiser's own calendar
    widget dedupes to one row."""
    return f"tickettailor:{event_id}:{day.isoformat()}"
