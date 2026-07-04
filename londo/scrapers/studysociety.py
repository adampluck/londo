from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from decimal import Decimal

from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper
from londo.scrapers.eventbrite import BROWSER_UA

logger = logging.getLogger(__name__)

WHATS_ON_URL = "https://www.studysociety.org/whats-on/"
BOOT_URL = "https://core.service.elfsight.com/p/boot/"

# recurring classes repeat forever; expand a little past the frontend's
# 30-day window so it never runs dry between scrapes
HORIZON_DAYS = 35

WIDGET_ID_RE = re.compile(r"elfsight-app-([0-9a-f-]{36})")

PRICE_RE = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
# descriptions here use "free" loosely ("free movement", a host surnamed
# Free), so only an explicit no-charge phrasing counts
FREE_RE = re.compile(
    r"\bfree\s+(?:entry|event|admission|to\s+attend)\b"
    r"|\bentry\s+is\s+free\b|\bfree\s+of\s+charge\b",
    re.I,
)

COLET_HOUSE = Location(
    venue_name="Colet House",
    address="151 Talgarth Rd, London W14 9DA",
    city="London",
    country="GB",
)


class StudySocietyScraper(BaseScraper):
    """Scrapes The Study Society (Colet House, Barons Court) events.

    Their Ticket Tailor box office is behind a Cloudflare challenge, but
    the What's On page on their own site renders an Elfsight event-calendar
    widget whose boot endpoint serves the full event list as JSON: titles,
    start/end times, HTML descriptions, cover images, locations and the
    Ticket Tailor booking link per event. The widget id is read from the
    page on each run so a re-embedded widget doesn't break the scraper.

    Recurring entries (weekly classes, session series) are expanded into
    per-date occurrences up to HORIZON_DAYS out, matching what the widget
    itself renders. Online-only sessions are dropped; the booking link is
    used as source_url.
    """

    source_name = "studysociety"

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self.session.headers.update({"User-Agent": BROWSER_UA})

    def scrape(self) -> list[Event]:
        page = self.get(WHATS_ON_URL).text
        match = WIDGET_ID_RE.search(page)
        if not match:
            raise RuntimeError("No Elfsight widget found on What's On page")
        widget_id = match.group(1)

        boot_url = f"{BOOT_URL}?page={quote(WHATS_ON_URL, safe='')}&w={widget_id}"
        self.session.headers["Referer"] = WHATS_ON_URL
        data = self.get(boot_url).json()

        settings = data["data"]["widgets"][widget_id]["data"]["settings"]
        locations = {loc["id"]: loc for loc in settings.get("locations") or []}
        types = {t["id"]: t["name"] for t in settings.get("eventTypes") or []}

        items = settings.get("events") or []
        logger.info("Widget lists %d events", len(items))

        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        horizon = datetime.now(timezone.utc) + timedelta(days=HORIZON_DAYS)
        events: list[Event] = []
        for item in items:
            base = _build_event(item, locations, types)
            if base is None:
                continue
            if item.get("repeatPeriod", "noRepeat") == "noRepeat":
                if base.start_datetime and base.start_datetime < cutoff:
                    continue
                if base.start_datetime is None and (
                    base.start_date is None or base.start_date < cutoff.date()
                ):
                    continue
                events.append(base)
                logger.info("Scraped: %s", base.title)
                continue
            occurrences = [
                (start, end, day)
                for start, end, day in _occurrences(item, horizon)
                if cutoff <= start <= horizon
            ]
            for start, end, day in occurrences:
                events.append(
                    base.model_copy(
                        deep=True,
                        update={
                            # per-date row: occurrences appear/expire
                            # independently as the horizon slides
                            "source_id": f"{base.source_id}:{day.isoformat()}",
                            "start_datetime": start,
                            "end_datetime": end,
                            "start_date": day,
                        },
                    )
                )
            if occurrences:
                logger.info(
                    "Scraped: %s (%d occurrence(s))", base.title, len(occurrences)
                )

        logger.info("Kept %d upcoming events", len(events))
        return events


def _build_event(item: dict, locations: dict, types: dict) -> Event | None:
    title = (item.get("name") or "").strip()
    if not title:
        return None

    loc_names = [
        (locations.get(lid) or {}).get("name", "").strip()
        for lid in item.get("location") or []
    ]
    is_online = bool(loc_names) and all(n == "Online" for n in loc_names)
    if is_online:
        return None
    # Colet House is the society's only venue; "Hybrid" events run there
    # with an online option, so both map to the house address.
    location = (
        COLET_HOUSE.model_copy()
        if any(n in ("Colet House", "Hybrid") for n in loc_names)
        else None
    )

    tz = ZoneInfo(item.get("timeZone") or "Europe/London")
    start_dt, start_d = _parse_when(item.get("start"), tz)
    end_dt, _ = _parse_when(item.get("end"), tz)

    description = _html_text(item.get("description"))
    price_tiers, is_free = _price_from_text(description)

    ticket_url = _ticket_url(item)
    tags = sorted(
        {types[tid].lower() for tid in item.get("eventType") or [] if tid in types}
        | {str(t).lower() for t in item.get("tags") or []}
    )

    cover = item.get("coverImage") or {}

    return Event(
        source="studysociety",
        source_id=str(item.get("id")),
        source_url=ticket_url or WHATS_ON_URL,
        title=title,
        description=description,
        start_datetime=start_dt,
        end_datetime=end_dt,
        start_date=start_d,
        is_all_day=start_dt is None and start_d is not None,
        location=location,
        image_url=cover.get("url") or None,
        tags=tags,
        price_tiers=price_tiers,
        is_free=is_free,
        organizer=Organizer(
            name="The Study Society", url="https://www.studysociety.org/"
        ),
        scraped_at=datetime.now(timezone.utc),
    )


WEEKDAYS = {"mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6}


def _occurrences(
    item: dict, horizon: datetime
) -> list[tuple[datetime, datetime | None, date]]:
    """Expand a recurring entry into concrete (start, end, local date)
    occurrences, from the series start through the horizon.

    Semantics were reverse-engineered against the live widget's rendering:
    - 'weeklyOn' repeats weekly (times repeatInterval) on the START date's
      weekday; its repeatFrequency and repeatWeeklyOnDays fields lie.
    - 'custom' honors repeatFrequency/repeatInterval, with weekly ones
      running on repeatWeeklyOnDays and monthly ones on either the same
      day-of-month or the start's nth-weekday (repeatMonthlyOnDay).
    - 'nthDayInMonth' is monthly on the start's nth-weekday.
    - repeatEnds 'onDate' is inclusive but IGNORED when the date is not
      after the series start (stale editor defaults render forever);
      'afterOccurrences' caps the count from the series start.
    - exceptions skip or reschedule single occurrences, keyed by the
      epoch-ms of the occurrence's start.
    """
    tz = ZoneInfo(item.get("timeZone") or "Europe/London")
    start_dt, _ = _parse_when(item.get("start"), tz)
    end_dt, _ = _parse_when(item.get("end"), tz)
    if start_dt is None:
        return []
    local_start = start_dt.astimezone(tz)
    first = local_start.date()
    duration = end_dt - start_dt if end_dt and end_dt > start_dt else None

    limit = horizon.astimezone(tz).date()
    max_n: int | None = None
    if item.get("repeatEnds") == "onDate":
        _, end_day = _parse_when(item.get("repeatEndsDate"), tz)
        if end_day and end_day > first:
            limit = min(limit, end_day)
    elif item.get("repeatEnds") == "afterOccurrences":
        max_n = max(int(item.get("repeatEndsOccurrences") or 1), 1)

    days = _candidate_days(item, first, limit)
    if max_n is not None:
        days = days[:max_n]

    exceptions = {
        int(ex["originalDate"]): ex
        for ex in item.get("exceptions") or []
        if isinstance(ex, dict) and ex.get("originalDate")
    }

    out: list[tuple[datetime, datetime | None, date]] = []
    for day in days:
        start = datetime(
            day.year, day.month, day.day,
            local_start.hour, local_start.minute, tzinfo=tz,
        ).astimezone(timezone.utc)
        end = start + duration if duration else None
        ex = exceptions.get(int(start.timestamp() * 1000))
        if ex:
            if ex.get("type") == "skip":
                continue
            new_start, _ = _parse_when(ex.get("rescheduledStart"), tz)
            if new_start:
                new_end, _ = _parse_when(ex.get("rescheduledEnd"), tz)
                start, end = new_start, new_end
                day = start.astimezone(tz).date()
        out.append((start, end, day))
    return out


def _candidate_days(item: dict, first: date, limit: date) -> list[date]:
    period = item.get("repeatPeriod")
    freq = item.get("repeatFrequency")
    interval = max(int(item.get("repeatInterval") or 1), 1)

    if period == "weeklyOn":
        return _every_n_days(first, limit, 7 * interval)
    if period == "nthDayInMonth" or (
        freq == "monthly" and item.get("repeatMonthlyOnDay") == "nthDay"
    ):
        return _monthly_nth_weekday(first, limit, interval)
    if freq == "monthly":
        return _monthly_same_day(first, limit, interval)
    if freq == "daily":
        return _every_n_days(first, limit, interval)
    if freq == "yearly":
        return [
            d
            for year in range(first.year, limit.year + 1)
            if (d := _safe_date(year, first.month, first.day)) and first <= d <= limit
        ]
    # custom weekly: any of the listed weekdays, in weeks aligned to the start
    on_days = {
        WEEKDAYS[d] for d in item.get("repeatWeeklyOnDays") or [] if d in WEEKDAYS
    } or {first.weekday()}
    week0 = first - timedelta(days=first.weekday())
    out = []
    day = first
    while day <= limit:
        if day.weekday() in on_days:
            weeks = (day - week0).days // 7
            if weeks % interval == 0:
                out.append(day)
        day += timedelta(days=1)
    return out


def _every_n_days(first: date, limit: date, step: int) -> list[date]:
    out = []
    day = first
    while day <= limit:
        out.append(day)
        day += timedelta(days=step)
    return out


def _monthly_nth_weekday(first: date, limit: date, interval: int) -> list[date]:
    nth = (first.day - 1) // 7
    out = []
    year, month = first.year, first.month
    while (year, month) <= (limit.year, limit.month):
        anchor = date(year, month, 1)
        offset = (first.weekday() - anchor.weekday()) % 7 + nth * 7
        day = anchor + timedelta(days=offset)
        if day.month == month and first <= day <= limit:
            out.append(day)
        month += interval
        year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    return out


def _monthly_same_day(first: date, limit: date, interval: int) -> list[date]:
    out = []
    year, month = first.year, first.month
    while (year, month) <= (limit.year, limit.month):
        day = _safe_date(year, month, first.day)
        if day and first <= day <= limit:
            out.append(day)
        month += interval
        year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    return out


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_when(
    value, tz: ZoneInfo
) -> tuple[datetime | None, date | None]:
    if not isinstance(value, dict) or not value.get("date"):
        return None, None
    day = date.fromisoformat(value["date"])
    time_str = value.get("time")
    if not time_str:
        return None, day
    hour, minute = (int(p) for p in time_str.split(":")[:2])
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    return local.astimezone(timezone.utc), day


def _ticket_url(item: dict) -> str | None:
    for action in item.get("actions") or []:
        link = (action or {}).get("link") or {}
        url = link.get("rawValue") or ""
        if link.get("type") == "url" and url.startswith("http"):
            # links copied from the site's embedded widget carry modal
            # parameters that render a bare iframe view in a normal tab
            return url.replace("?modal_widget=true&widget=true", "")
    return None


def _price_from_text(text: str | None) -> tuple[list[PriceTier], bool]:
    if not text:
        return [], False
    amounts = sorted({Decimal(m) for m in PRICE_RE.findall(text)})
    tiers = [
        PriceTier(name=f"Tier {i + 1}", amount=amount)
        for i, amount in enumerate(amounts)
    ]
    is_free = not tiers and FREE_RE.search(text) is not None
    return tiers, is_free


def _html_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text(" ", strip=True) or None
