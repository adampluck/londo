from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import icalendar
from bs4 import BeautifulSoup

from londo.models import Event, Location, Organizer
from londo.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

ICS_URL = "https://newspeak.house/api/events.ics"
HOME_URL = "https://newspeak.house/"
LUMA_EVENT_API = "https://api.lu.ma/url?url={slug}"

VENUE_NAME = "Newspeak House"
ADDRESS = "133-135 Bethnal Green Road, London E2 7DG"
LONDON = ZoneInfo("Europe/London")

LUMA_LINK_RE = re.compile(r"https?://(?:www\.)?(?:luma\.com|lu\.ma)/([A-Za-z0-9_-]+)")


class NewspeakScraper(BaseScraper):
    """Scrapes newspeak.house events.

    The iCal feed provides authoritative dates/times for every occurrence;
    the homepage's div.event blocks add descriptions, rooms, hosts and
    registration links. When an event registers via Luma, its cover image is
    fetched from Luma's public event API.
    """

    source_name = "newspeak"

    def scrape(self) -> list[Event]:
        blocks = self._parse_homepage()
        logger.info("Parsed %d event blocks from homepage", len(blocks))

        response = self.get(ICS_URL)
        cal = icalendar.Calendar.from_ical(response.text)
        now = datetime.now(timezone.utc)

        cover_cache: dict[str, str | None] = {}
        events: list[Event] = []
        for component in cal.walk("VEVENT"):
            try:
                start = component.get("DTSTART")
                if start is None:
                    continue
                start_dt = start.dt
                if not isinstance(start_dt, datetime) or start_dt < now:
                    continue
                events.append(self._build_event(component, blocks, cover_cache))
            except Exception:
                logger.exception(
                    "Failed to parse VEVENT %s", component.get("UID")
                )

        logger.info("Scraped %d upcoming events from Newspeak House", len(events))
        return events

    def _build_event(
        self,
        component: icalendar.Event,
        blocks: dict[str, dict],
        cover_cache: dict[str, str | None],
    ) -> Event:
        title = str(component.get("SUMMARY", "")).strip()
        start_dt = component.get("DTSTART").dt
        end = component.get("DTEND")
        end_dt = end.dt if end is not None else None
        ics_desc = str(component.get("DESCRIPTION", "")).strip() or None

        block, exact_date_match = _match_block(blocks, title, start_dt)

        # A Luma slug is occurrence-specific: only trust it when the homepage
        # block's date matches this occurrence (recurring events share titles).
        luma_slug = block.get("luma_slug") if exact_date_match else None

        # The feed regenerates random UIDs on every fetch, so derive a stable
        # identity: the Luma slug when present, else title + date.
        if luma_slug:
            source_id = f"luma-{luma_slug}"
        else:
            day = start_dt.astimezone(LONDON).date().isoformat()
            digest = hashlib.sha1(
                f"{_norm_title(title)}|{day}".encode()
            ).hexdigest()[:16]
            source_id = f"td-{digest}"

        image_url = None
        if luma_slug:
            if luma_slug not in cover_cache:
                cover_cache[luma_slug] = self._fetch_luma_cover(luma_slug)
            image_url = cover_cache[luma_slug]

        room = block.get("room")
        venue = f"{VENUE_NAME} ({room})" if room else VENUE_NAME

        source_url = (
            f"https://newspeak.house/events?id={block['id']}"
            if block.get("id") and exact_date_match
            else f"{HOME_URL}#events"
        )

        organizer = None
        if block.get("host"):
            organizer = Organizer(name=block["host"], url=block.get("host_url"))

        return Event(
            source="newspeak",
            source_id=source_id,
            source_url=source_url,
            external_ref=f"luma:{luma_slug}" if luma_slug else None,
            title=title,
            description=block.get("description") or ics_desc,
            start_datetime=start_dt,
            end_datetime=end_dt if isinstance(end_dt, datetime) else None,
            location=Location(
                venue_name=venue,
                address=ADDRESS,
                city="London",
                country="United Kingdom",
                latitude=51.5253,
                longitude=-0.0698,
            ),
            image_url=image_url,
            organizer=organizer,
            scraped_at=datetime.now(timezone.utc),
        )

    def _parse_homepage(self) -> dict[str, list[dict]]:
        """Map normalised title -> homepage event blocks (recurring events
        appear once per occurrence, each with its own date and links)."""
        response = self.get(HOME_URL)
        soup = BeautifulSoup(response.text, "html.parser")

        blocks: dict[str, list[dict]] = {}
        for div in soup.find_all("div", class_="event"):
            title_el = div.find("div", class_="event-title")
            if not title_el:
                continue
            title_link = title_el.find("a")
            title = title_el.get_text(strip=True)

            block: dict = {"title": title}

            div_id = div.get("id", "")  # "event-1710" or "event-ration-club"
            event_id = div_id.removeprefix("event-")
            if event_id.isdigit():
                block["id"] = event_id

            details_el = div.find("div", class_="event-details")
            if details_el:
                details = details_el.get_text(strip=True)
                parts = [p.strip() for p in details.split("•")]
                # "Thu 11 JUN 2026 • 6:30pm – 9:30pm • Classroom"
                if len(parts) >= 3 and not re.search(r"\d", parts[-1]):
                    block["room"] = parts[-1]
                block["date"] = _parse_block_date(details)

            host_el = div.find("div", class_="event-host")
            if host_el:
                block["host"] = host_el.get_text(strip=True)
                host_link = host_el.find("a")
                if host_link and host_link.get("href"):
                    block["host_url"] = host_link["href"]

            paragraphs = [
                p.get_text(" ", strip=True).replace("​", "")
                for p in div.find_all("p")
            ]
            description = "\n\n".join(p for p in paragraphs if p).strip()
            if description:
                block["description"] = description

            # Luma slug from the title link or any register link
            for a in [title_link, *div.find_all("a", class_="section-link")]:
                if a is None or not a.get("href"):
                    continue
                m = LUMA_LINK_RE.match(a["href"])
                if m:
                    block["luma_slug"] = m.group(1)
                    break

            register = div.find("a", class_="section-link")
            if register and register.get("href"):
                block["register_url"] = register["href"]

            blocks.setdefault(_norm_title(title), []).append(block)

        return blocks

    def _fetch_luma_cover(self, slug: str) -> str | None:
        try:
            data = self.get(LUMA_EVENT_API.format(slug=slug)).json()
            event = (data.get("data") or {}).get("event") or {}
            return event.get("cover_url") or event.get("social_image_url")
        except Exception:
            logger.warning("Could not fetch Luma cover for %s", slug)
            return None


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

BLOCK_DATE_RE = re.compile(r"\b(\d{1,2}) ([A-Z]{3}) (\d{4})\b")


def _parse_block_date(details: str) -> date | None:
    m = BLOCK_DATE_RE.search(details)
    if not m:
        return None
    day, mon, year = m.groups()
    month = MONTHS.get(mon)
    if not month:
        return None
    return date(int(year), month, int(day))


def _match_block(
    blocks: dict[str, list[dict]], title: str, start_dt: datetime
) -> tuple[dict, bool]:
    """Return (block, exact_date_match) for an iCal occurrence.

    Prefers the block whose printed date matches the occurrence; otherwise
    any same-title block still provides description/room/host (those are
    shared across occurrences of a recurring event), but not links.
    """
    candidates = blocks.get(_norm_title(title), [])
    if not candidates:
        return {}, False
    event_date = start_dt.astimezone(LONDON).date()
    for block in candidates:
        if block.get("date") == event_date:
            return block, True
    return candidates[0], False
