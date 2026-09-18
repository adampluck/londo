from __future__ import annotations

import logging
import re
from urllib.parse import quote
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from londo.geo import is_london
from londo.models import Event, Location, Organizer, PriceTier
from londo.scrapers.base import BaseScraper
from londo.scrapers.tickettailor import EVENT_URL_RE as TICKETTAILOR_RE
from londo.scrapers.tickettailor import external_ref as tickettailor_ref

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

SITE_URL = "https://soullinkhub.com"
EVENT_PAGE = SITE_URL + "/event/{id}"

# SoulLinkHub is a client-rendered Lovable app with no feed and no event
# URLs in its sitemap: the browser reads the listings straight from the
# site's Supabase project with this anon key (public by design — it ships
# in the site's JS bundle and row-level security limits it to reads of
# published events, fees and tags).
SUPABASE_URL = "https://rhsilfaimkqlvzrxqjbq.supabase.co"
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InJoc2lsZmFpbWtxbHZ6cnhxamJxIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3NTI2NTUxNDgsImV4cCI6MjA2ODIzMTE0OH0."
    "JRnSl8BlFnlM5-tXfA0QBo-ogIYic34ZrPyijj7fR-c"
)
EVENTS_URL = (
    SUPABASE_URL + "/rest/v1/events"
    "?select=id,title,description,location,geolocation,start_datetime,"
    "end_datetime,image,external_link,facilitated_by"
    "&status=eq.published&end_datetime=gte.{now}"
    "&order=start_datetime.asc,id.asc&limit={limit}&offset={offset}"
)
FEES_URL = (
    SUPABASE_URL + "/rest/v1/event_fees"
    "?select=event_id,fee_category,fee_amount,currency,display_order"
    "&is_active=eq.true&event_id=in.({ids})"
)
PAGE_SIZE = 500
FEE_BATCH = 100

# Greater London bounding box: rescues venues whose address names neither
# London nor a recognisable district ("Central School of Ballet, Waterloo").
LAT_RANGE = (51.28, 51.70)
LNG_RANGE = (-0.52, 0.34)

ONLINE_RE = re.compile(r"^\s*online\b", re.I)



class SoulLinkHubScraper(BaseScraper):
    """Scrapes SoulLinkHub's published events.

    A UK-wide listing site for conscious-community gatherings (5Rhythms,
    contact improv, men's and women's circles, tantra workshops). Each
    listing carries a full description, cover image, address, coordinates
    and facilitator, and the site's own event page is the way in: many
    listings link onward to a ticket page elsewhere, but not all.

    National coverage, so London is required positively — the address
    reads as London, or the coordinates fall inside Greater London — and
    the 'Online' listings are dropped first.
    """

    source_name = "soullinkhub"

    def __init__(self, rate_limit: float = 1.0):
        super().__init__(rate_limit=rate_limit)
        self.session.headers.update(
            {
                "apikey": ANON_KEY,
                "Authorization": f"Bearer {ANON_KEY}",
                "Prefer": "count=exact",  # fills in the Content-Range total
            }
        )

    def scrape(self) -> list[Event]:
        rows = self._fetch_rows()
        fees = self._fetch_fees([row["id"] for row in rows])

        events: list[Event] = []
        dropped = 0
        for row in rows:
            try:
                event = _build_event(row, fees.get(row["id"], []))
            except Exception:
                logger.exception("Failed to parse event %s", row.get("id"))
                continue
            if event is None:
                dropped += 1
                continue
            events.append(event)
            logger.info("Scraped: %s", event.title)

        logger.info(
            "Scraped %d London events from SoulLinkHub (%d elsewhere/online)",
            len(events),
            dropped,
        )
        return events

    def _fetch_rows(self) -> list[dict]:
        now = quote(datetime.now(timezone.utc).isoformat(timespec="seconds"))
        rows: list[dict] = []
        offset = 0
        while True:
            url = EVENTS_URL.format(now=now, limit=PAGE_SIZE, offset=offset)
            response = self.get(url)
            page = response.json()
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
        # PostgREST caps a page at its max-rows setting, which a short page
        # would hide; the response says how many rows the query matched.
        total = _content_range_total(response.headers.get("Content-Range", ""))
        if total is not None and total != len(rows):
            logger.warning("SoulLinkHub matched %d events, fetched %d", total, len(rows))
        return rows

    def _fetch_fees(self, ids: list[str]) -> dict[str, list[dict]]:
        fees: dict[str, list[dict]] = {}
        for i in range(0, len(ids), FEE_BATCH):
            batch = ",".join(ids[i : i + FEE_BATCH])
            try:
                items = self.get(FEES_URL.format(ids=batch)).json()
            except Exception:
                logger.exception("Could not fetch fees; listing without prices")
                return fees
            for item in items:
                fees.setdefault(item["event_id"], []).append(item)
        return fees


def _content_range_total(header: str) -> int | None:
    """Total from a 'Content-Range: 0-49/112' header ('*' when unknown)."""
    _, _, total = header.partition("/")
    return int(total) if total.isdigit() else None


def _build_event(row: dict, fees: list[dict]) -> Event | None:
    title = (row.get("title") or "").strip()
    if not title:
        return None

    location = _location(row)
    if location is None:
        logger.debug("Skipping non-London event: %s", title)
        return None

    start = _parse_when(row.get("start_datetime"))
    end = _parse_when(row.get("end_datetime"))
    if start is None:
        return None

    price_tiers, is_free = _price(fees)
    facilitator = (row.get("facilitated_by") or "").strip()

    return Event(
        source="soullinkhub",
        source_id=row["id"],
        source_url=EVENT_PAGE.format(id=row["id"]),
        external_ref=_external_ref(row.get("external_link"), start),
        title=title,
        description=_html_text(row.get("description")),
        start_datetime=start,
        end_datetime=end,
        start_date=start.astimezone(LONDON).date(),
        location=location,
        image_url=row.get("image") or None,
        price_tiers=price_tiers,
        is_free=is_free,
        organizer=Organizer(name=facilitator) if facilitator else None,
        scraped_at=datetime.now(timezone.utc),
    )


def _location(row: dict) -> Location | None:
    """The listing's London location, or None if it isn't a London event."""
    address = (row.get("location") or "").strip()
    if not address or ONLINE_RE.match(address):
        return None
    geo = row.get("geolocation") or {}
    lat, lng = _to_float(geo.get("lat")), _to_float(geo.get("lng"))
    in_box = (
        lat is not None
        and lng is not None
        and LAT_RANGE[0] <= lat <= LAT_RANGE[1]
        and LNG_RANGE[0] <= lng <= LNG_RANGE[1]
    )
    if not is_london(address) and not in_box:
        return None
    # The whole string stays in `address`: the site doesn't separate venue
    # from street, and a guessed venue name would only trip dedupe's
    # venue-disagreement gate against the native copy.
    return Location(
        address=address,
        city="London",
        country="GB",
        latitude=lat,
        longitude=lng,
    )


def _external_ref(link: str | None, start: datetime) -> str | None:
    """Shared identity with the Ticket Tailor scraper's copy of the same
    occurrence. Only Ticket Tailor refs name the date; an Eventbrite or
    Luma link is one fixed URL for a whole series, and dedupe unions on
    bare ref equality, so such a ref could merge a listing onto another
    date's copy. Those rely on title + day, which matches each date."""
    if not link:
        return None
    m = TICKETTAILOR_RE.match(link)
    if m:
        return tickettailor_ref(m.group(2), start.astimezone(LONDON).date())
    return None


def _parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _price(fees: list[dict]) -> tuple[list[PriceTier], bool]:
    tiers = []
    for fee in sorted(fees, key=lambda f: (f.get("display_order") or 0, str(f.get("fee_amount")))):
        try:
            amount = Decimal(str(fee.get("fee_amount")))
        except (InvalidOperation, ValueError):
            continue
        tiers.append(
            PriceTier(
                name=(fee.get("fee_category") or "Ticket").strip() or "Ticket",
                amount=amount,
                currency=fee.get("currency") or "GBP",
            )
        )
    if tiers and all(tier.amount == 0 for tier in tiers):
        return [], True
    # No fee rows means the tickets are sold elsewhere, not that it's free.
    return tiers, False


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _html_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text(" ", strip=True) or None
