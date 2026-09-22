from __future__ import annotations

import html
import logging
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from londo.geo import is_london
from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

SITE_URL = "https://www.jimeaton.org/"
EVENTS_URL = SITE_URL + "events/"
# WooCommerce's public Store API. Events are products in the
# 'event-bookings' category; 'one-to-one-bookings' is coaching.
API_URL = (
    SITE_URL + "wp-json/wc/store/v1/products"
    "?category=event-bookings&per_page=50&page={page}"
)
MAX_PAGES = 5

# A product's short description is a labelled blurb: "Time:" (one line
# per day, or "Dates & Times:" with one <li> per session), "Venue:" and
# "Description:" (or "Info:"). The year appears only in the title.
_LABEL_RE = re.compile(
    r"^(time|dates?\s*&\s*times?|dates?|venue|description|info)\s*:?\s*$", re.I
)
_LABEL_PREFIX_RE = re.compile(
    r"^(time|dates?\s*&\s*times?|dates?|venue|description|info)\s*:\s*", re.I
)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
# "Saturday 24th October, 10:00-17:00" / "Session 1: Tuesday 22nd
# September, 6:30pm to 9:00pm" — day-of-month, month, then a time range.
_SESSION_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)"
    r"(?:\s+(20\d\d))?\s*,?\s*"
    r"(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)",
    re.I,
)
_TIME_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)
# Venue lines that give directions rather than a place.
_DIRECTIONS_RE = re.compile(r"address|will be sent|closest tube|close to|nearest", re.I)


class JimEatonScraper(BaseScraper):
    """Scrapes Jim Eaton's Circling and Surrendered Leadership events.

    The site is a WordPress/WooCommerce shop in which each event is a
    product, so the public Store API lists them with description, cover
    image and ticket prices. When and where are prose in the short
    description, and the year only in the product title.

    Events run around the country (Totnes, Stroud, Bristol), so London
    is required positively on the venue text. London venues are given
    only as an area ("North London, N4"): the address is emailed on
    booking.
    """

    source_name = "jimeaton"

    def scrape(self) -> list[Event]:
        events: list[Event] = []
        dropped = 0
        page = 1
        while page <= MAX_PAGES:
            response = self.get(API_URL.format(page=page))
            for item in response.json():
                try:
                    event = _build_event(item)
                except Exception:
                    logger.exception("Failed to parse product %s", item.get("permalink"))
                    continue
                if event is None:
                    dropped += 1
                    continue
                events.append(event)
                logger.info("Scraped: %s", event.title)
            if page >= int(response.headers.get("X-WP-TotalPages") or 1):
                break
            page += 1

        logger.info(
            "Scraped %d London events from Jim Eaton (%d elsewhere/past)",
            len(events),
            dropped,
        )
        return events


def _build_event(item: dict) -> Event | None:
    title = html.unescape(BeautifulSoup(item.get("name") or "", "html.parser").get_text())
    title = re.sub(r"\s+", " ", title).strip()
    if not title:
        return None

    sections = _sections(item.get("short_description") or "")
    venue_text = " ".join(sections.get("venue", []))
    if not is_london(venue_text):
        logger.debug("Skipping non-London event: %s", title)
        return None

    year_match = _YEAR_RE.search(title)
    year = int(year_match.group(1)) if year_match else None
    sessions = _sessions(sections.get("time", []), year)
    if not sessions:
        logger.warning("No usable date for %s", title)
        return None
    start = sessions[0][0]
    end = sessions[-1][1]
    if end < datetime.now(timezone.utc):
        logger.debug("Skipping past event: %s", title)
        return None

    venue_lines = [line.strip(" .,") for line in sections.get("venue", [])]
    place = [line for line in venue_lines if not _DIRECTIONS_RE.search(line)] or venue_lines[:1]
    location = Location(
        venue_name=place[0] if len(venue_lines) > 1 else None,
        address=", ".join(place) or "London, UK",
        city="London",
        country="GB",
    )

    description_html = " ".join(sections.get("description", []))
    description = _html_text(description_html) or _html_text(item.get("description"))

    return Event(
        source="jimeaton",
        source_id=str(item["id"]),
        source_url=item.get("permalink") or EVENTS_URL,
        title=title,
        description=description,
        start_datetime=start,
        end_datetime=end,
        start_date=start.astimezone(LONDON).date(),
        location=location,
        image_url=((item.get("images") or [{}])[0]).get("src") or None,
        price_tiers=_price(item),
        is_free=False,
        organizer=Organizer(name="Jim Eaton", url=SITE_URL),
        scraped_at=datetime.now(timezone.utc),
    )


def _sections(short_description: str) -> dict[str, list[str]]:
    """Split the blurb into its labelled parts, one entry per line.

    A label sits in its own paragraph or starts one; <br>-separated lines
    are separate entries. Description entries keep their HTML so lists
    survive.
    """
    soup = BeautifulSoup(short_description, "html.parser")
    sections: dict[str, list[str]] = {}
    current = None
    for block in soup.find_all(["p", "li", "h1", "h2", "h3", "h4"]):
        if block.find_parent("li") is not None:
            continue
        lines = [
            re.sub(r"\s+", " ", piece).strip()
            for piece in block.get_text("\n").split("\n")
        ]
        lines = [line for line in lines if line]
        if not lines:
            continue
        if _LABEL_RE.match(lines[0]):
            current = _canonical(lines[0])
            lines = lines[1:]
        elif m := _LABEL_PREFIX_RE.match(lines[0]):
            current = _canonical(m.group(1))
            lines[0] = lines[0][m.end():]
        if current is None or not lines:
            continue
        if current == "description":
            for strong in block.find_all("strong"):
                if _LABEL_PREFIX_RE.match(strong.get_text() + ":"):
                    strong.decompose()
            sections.setdefault(current, []).append(str(block))
        else:
            sections.setdefault(current, []).extend(lines)
    return sections


def _canonical(label: str) -> str:
    lowered = label.lower()
    if lowered.startswith("venue"):
        return "venue"
    if lowered.startswith(("description", "info")):
        return "description"
    return "time"


def _sessions(lines: list[str], year: int | None) -> list[tuple[datetime, datetime]]:
    """Each dated line as a UTC (start, end) pair, in order."""
    sessions = []
    # Joined: a date and its times sometimes sit on separate lines.
    for m in _SESSION_RE.finditer(" ".join(lines)):
        day, month_name, line_year, start_text, end_text = m.groups()
        session_year = int(line_year) if line_year else year
        if session_year is None:
            return []
        try:
            day_date = datetime.strptime(
                f"{int(day)} {month_name[:3]} {session_year}", "%d %b %Y"
            ).date()
        except ValueError:
            continue
        start_time = _time(start_text, None)
        end_time = _time(end_text, start_time)
        if start_time is None or end_time is None:
            continue
        # "6:30pm to 9:00pm": the start inherits the end's meridiem.
        if _TIME_RE.match(start_text).group(3) is None and end_time < start_time:
            start_time = _time(start_text, end_time)
        sessions.append((_utc(day_date, start_time), _utc(day_date, end_time)))
    return sessions


def _time(text: str, hint: time | None) -> time | None:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if meridiem is None and hint is not None and hint.hour >= 12 and hour < 12:
        hour += 12
    elif meridiem:
        hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return time(hour, minute)


def _utc(day: date, at: time) -> datetime:
    return datetime.combine(day, at, tzinfo=LONDON).astimezone(timezone.utc)


def _price(item: dict) -> list[PriceTier]:
    prices = item.get("prices") or {}
    try:
        unit = Decimal(10) ** int(prices.get("currency_minor_unit") or 2)
    except (InvalidOperation, ValueError):
        unit = Decimal(100)
    currency = prices.get("currency_code") or "GBP"
    price_range = prices.get("price_range") or {}
    tiers = []
    for key, name in (("min_amount", "From"), ("max_amount", "To")):
        raw = price_range.get(key) if price_range else prices.get("price")
        if raw is None:
            continue
        try:
            tiers.append(PriceTier(name=name, amount=Decimal(str(raw)) / unit, currency=currency))
        except (InvalidOperation, ValueError):
            continue
        if not price_range:
            break
    return tiers


def _html_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text(" ", strip=True) or None
