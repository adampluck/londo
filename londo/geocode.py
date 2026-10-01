"""Coordinates for in-person events whose source gave only an address.

Eventbrite, Luma and a few others hand over lat/lng; Dandelion, Momence,
Meetup's iCal and the Study Society give an address and nothing else, so
those events were missing from the map. This fills them in, best first:

1. a place we already know — the same venue + address on any row that has
   coordinates (another source's copy, or an earlier run's lookup), so a
   weekly class is looked up once, not every run;
2. a full UK postcode, via postcodes.io (free, no key, bulk);
3. the venue and address, via OpenStreetMap's Nominatim, held to Greater
   London — a street or a venue name usually lands within a few hundred
   metres;
4. the postcode district alone ("London E5"), at its centroid — rough, but
   on the right side of town.

An address that says no more than "London" is left unplaced: a pin at
Charing Cross would be a guess dressed as a fact. Lookups never fail the
run — a network error just leaves the event as it was.
"""

from __future__ import annotations

import logging
import math
import re
import time

import requests

from londo.geo import LONDON_BBOX, OUTER_POSTCODE_RE, POSTCODE_RE
from londo.models import Event, Location

logger = logging.getLogger(__name__)

POSTCODES_IO = "https://api.postcodes.io"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy: an identifying User-Agent, at most one request
# a second, and cache what you get (the places map does that).
USER_AGENT = "londo-events/1.0 (+https://psyconnect.london)"
NOMINATIM_INTERVAL = 1.1
# keeps a run's worst case (a source suddenly listing dozens of new venues)
# to a few minutes; the rest are picked up by the next run
MAX_NOMINATIM = 120

FULL_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)

# Words that add nothing to a lookup (and Dandelion's localised countries).
_FILLER = {
    "london", "uk", "united kingdom", "england", "gb", "great britain",
    "regno unito", "royaume-uni", "reino unido", "vereinigtes königreich",
}
# ...and placeholders that look like places to a geocoder but aren't —
# "East London" comes back as a point in Notting Hill, "TBC" near Heathrow.
_VAGUE_RE = re.compile(
    r"^(?:(?:central|north|south|east|west|greater)\s+london|tbc|tba|tbd|"
    r"to be (?:confirmed|announced)|secret location|venue tbc|london tbc)$"
)
# A query's matches must chain together, each within this of another, or it
# named more than one place (London has a dozen Elm Groves) — better no pin
# than the wrong one. Chaining, not a radius, so a long road's segments
# still count as one road.
CHAIN_KM = 1.5


def place_key(venue: str | None, address: str | None) -> str:
    """Normalised venue + address, the key places are remembered by."""
    text = f"{venue or ''} | {address or ''}".lower()
    return re.sub(r"\s+", " ", text).strip()


def known_places(rows: list[dict]) -> dict[str, tuple[float, float]]:
    """Build the places map from stored rows carrying venue_name, address,
    latitude and longitude."""
    out: dict[str, tuple[float, float]] = {}
    for r in rows:
        if r.get("latitude") is None or r.get("longitude") is None:
            continue
        out.setdefault(
            place_key(r.get("venue_name"), r.get("address")),
            (float(r["latitude"]), float(r["longitude"])),
        )
    return out


def geocode_events(
    events: list[Event],
    places: dict[str, tuple[float, float]] | None = None,
    session: requests.Session | None = None,
) -> int:
    """Fill in latitude/longitude on in-person events that lack them.
    Returns how many were placed."""
    places = dict(places or {})
    # every event that already has coordinates teaches us its place too
    for e in events:
        loc = e.location
        if loc and loc.latitude is not None and loc.longitude is not None:
            places.setdefault(
                place_key(loc.venue_name, loc.address), (loc.latitude, loc.longitude)
            )

    todo = [
        e
        for e in events
        if not e.is_online
        and e.location is not None
        and (e.location.latitude is None or e.location.longitude is None)
        and _is_specific(e.location)
    ]
    if not todo:
        return 0

    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT  # requests sends its own by default
    by_key: dict[str, list[Event]] = {}
    for e in todo:
        by_key.setdefault(place_key(e.location.venue_name, e.location.address), []).append(e)

    found: dict[str, tuple[float, float]] = {}
    how: dict[str, int] = {"known": 0, "postcode": 0, "nominatim": 0, "district": 0}

    pending = []
    for key in by_key:
        if key in places:
            found[key] = places[key]
            how["known"] += 1
        else:
            pending.append(key)

    # full postcodes, a hundred to a request
    postcode_of = {
        key: _full_postcode(by_key[key][0].location) for key in pending
    }
    wanted = sorted({p for p in postcode_of.values() if p})
    coords = _lookup_postcodes(session, wanted)
    for key in list(pending):
        hit = coords.get(postcode_of[key] or "")
        if hit:
            found[key] = hit
            how["postcode"] += 1
            pending.remove(key)

    # venue and street names
    budget = MAX_NOMINATIM
    for key in list(pending):
        if budget <= 0:
            logger.info("Nominatim budget spent; %d places left for next run", len(pending))
            break
        loc = by_key[key][0].location
        for query in _queries(loc):
            if budget <= 0:
                break
            budget -= 1
            try:
                hit = _nominatim(session, query)
            except NominatimRefused as refused:
                # retrying a service that has turned us away is how a block
                # becomes a ban; the next run tries again
                logger.warning("Nominatim refused us (%s); skipping it this run", refused)
                budget = 0
                break
            if hit:
                found[key] = hit
                how["nominatim"] += 1
                pending.remove(key)
                break

    # the district centroid, last
    districts = {key: _district(by_key[key][0].location) for key in pending}
    for d in sorted({d for d in districts.values() if d}):
        hit = _lookup_district(session, d)
        if not hit:
            continue
        for key, dk in districts.items():
            if dk == d and key not in found:
                found[key] = hit
                how["district"] += 1

    placed = 0
    for key, (lat, lng) in found.items():
        if not _in_london(lat, lng):
            continue
        for e in by_key[key]:
            e.location.latitude, e.location.longitude = lat, lng
            placed += 1
    if placed:
        logger.info(
            "Geocoded %d events (%s places: %s)",
            placed,
            len(found),
            ", ".join(f"{n} {k}" for k, n in how.items() if n),
        )
    return placed


def _is_specific(loc: Location) -> bool:
    """Whether the address says anything beyond "London"."""
    text = " ".join(p for p in (loc.venue_name, loc.address) if p)
    parts = [p.strip().lower() for p in re.split(r"[,\n]", text)]
    return any(p and p not in _FILLER and not _VAGUE_RE.match(p) for p in parts)


def _full_postcode(loc: Location) -> str | None:
    text = " ".join(p for p in (loc.address, loc.venue_name) if p)
    m = FULL_POSTCODE_RE.search(text)
    return f"{m.group(1)} {m.group(2)}".upper() if m else None


def _district(loc: Location) -> str | None:
    text = " ".join(p for p in (loc.address, loc.venue_name) if p)
    m = POSTCODE_RE.search(text) or OUTER_POSTCODE_RE.search(text)
    return m.group(0).upper() if m else None


def _queries(loc: Location) -> list[str]:
    """Nominatim queries, most specific first: venue with address, then the
    address alone (venue names OSM doesn't know sink the whole query)."""
    address = _tidy(loc.address)
    venue = (loc.venue_name or "").strip()
    out = []
    if venue and venue.lower() not in (loc.address or "").lower():
        out.append(f"{venue}, {address}" if address else venue)
    if address:
        out.append(address)
    return [q for q in dict.fromkeys(out) if _is_specific(Location(address=q))]


def _tidy(address: str | None) -> str:
    parts = [p.strip() for p in (address or "").split(",")]
    kept = [p for p in parts if p and p.lower() not in _FILLER - {"london"}]
    return ", ".join(kept)


def _in_london(lat: float, lng: float) -> bool:
    s, n, w, e = LONDON_BBOX
    return s <= lat <= n and w <= lng <= e


def _lookup_postcodes(
    session: requests.Session, postcodes: list[str]
) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for i in range(0, len(postcodes), 100):
        batch = postcodes[i : i + 100]
        try:
            r = session.post(f"{POSTCODES_IO}/postcodes", json={"postcodes": batch}, timeout=30)
            r.raise_for_status()
            results = r.json().get("result") or []
        except Exception:
            logger.warning("postcodes.io lookup failed", exc_info=True)
            continue
        for item in results:
            res = item.get("result")
            if res and res.get("latitude") is not None:
                out[item["query"]] = (res["latitude"], res["longitude"])
    return out


def _lookup_district(session: requests.Session, district: str) -> tuple[float, float] | None:
    try:
        r = session.get(f"{POSTCODES_IO}/outcodes/{district}", timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        res = r.json().get("result") or {}
    except Exception:
        logger.warning("postcodes.io district lookup failed for %s", district, exc_info=True)
        return None
    if res.get("latitude") is None:
        return None
    return (res["latitude"], res["longitude"])


_last_nominatim = 0.0


class NominatimRefused(Exception):
    """403/429: we're blocked or too fast — stop asking for this run."""


def _nominatim(session: requests.Session, query: str) -> tuple[float, float] | None:
    global _last_nominatim
    wait = _last_nominatim + NOMINATIM_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    s, n, w, e = LONDON_BBOX
    try:
        r = session.get(
            NOMINATIM,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 10,
                "countrycodes": "gb",
                "viewbox": f"{w},{n},{e},{s}",
                "bounded": 1,
            },
            timeout=30,
        )
        _last_nominatim = time.monotonic()
        if r.status_code in (403, 429):
            raise NominatimRefused(r.status_code)
        r.raise_for_status()
        hits = r.json()
    except NominatimRefused:
        raise
    except Exception:
        _last_nominatim = time.monotonic()
        logger.warning("Nominatim lookup failed for %r", query, exc_info=True)
        return None
    if not hits:
        return None
    points = [(float(h["lat"]), float(h["lon"])) for h in hits]
    if not _chained(points):
        logger.info("Nominatim: %r names more than one place, skipping", query)
        return None
    return points[0]


def _chained(points: list[tuple[float, float]]) -> bool:
    """Whether every point links to the first through hops of CHAIN_KM."""
    reached, frontier = {0}, [0]
    while frontier:
        i = frontier.pop()
        for j, p in enumerate(points):
            if j not in reached and _km(points[i], p) <= CHAIN_KM:
                reached.add(j)
                frontier.append(j)
    return len(reached) == len(points)


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    dy = (a[0] - b[0]) * 111.0
    dx = (a[1] - b[1]) * 111.0 * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)
