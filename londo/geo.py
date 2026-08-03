from __future__ import annotations

import math
import re

from londo.models import Event

# Central London reference point (Charing Cross).
CENTER_LAT, CENTER_LNG = 51.5072, -0.1276
CENTRAL_RADIUS_KM = 3.0

# London postcode district, e.g. E2, SE15, NW1, EC2A, WC1.
POSTCODE_RE = re.compile(r"\b(EC|WC|E|N|NW|SE|SW|W)(\d{1,2})[A-Z]?\b", re.I)

AREAS = ("central", "east", "north", "south", "west")

# Outer-London postcode areas. A few (EN, KT, DA, WD) straddle the boundary
# into the home counties and are left out: sources that reach them are
# generally leaving London altogether.
OUTER_POSTCODE_RE = re.compile(r"\b(BR|CR|HA|IG|RM|SM|TW|UB)(\d{1,2})[A-Z]?\b", re.I)

# Greater London boroughs and the districts that stand in for them in
# addresses, for venues that name a place but never "London" ("Richmond
# Station", "Lauderdale House, Highgate"). Names shared with towns outside
# London (Richmond, Sutton) are a deliberate trade: the sources these
# match are London listings to begin with.
LONDON_PLACES = frozenset(
    """
    barking dagenham barnet bexley brent bromley camden croydon ealing
    enfield greenwich hackney hammersmith fulham haringey harrow havering
    hillingdon hounslow islington kensington chelsea kingston lambeth
    lewisham merton newham redbridge richmond southwark sutton
    tower-hamlets walthamstow wandsworth westminster shoreditch hoxton
    dalston peckham brixton clapham camberwell deptford bermondsey
    highgate hampstead holloway kilburn stratford twickenham wimbledon
    soho dulwich hackney-wick
    """.split()
)

_WORD_RE = re.compile(r"[a-z]+(?:-[a-z]+)?")


def is_london(text: str) -> bool:
    """Whether a free-text address plausibly sits in Greater London.

    Used by sources that cover more than London (national networks,
    Meetup groups that occasionally run a retreat elsewhere) to keep only
    what belongs here. Answers on positive evidence — callers decide what
    an unrecognised address means.
    """
    if not text:
        return False
    lowered = text.lower()
    if "london" in lowered:
        return True
    if POSTCODE_RE.search(text) or OUTER_POSTCODE_RE.search(text):
        return True
    return any(word in LONDON_PLACES for word in _WORD_RE.findall(lowered))


def assign_area(event: Event) -> str | None:
    """Deterministic London area from postcode (preferred) or lat/lng."""
    loc = event.location
    if loc is None:
        return None

    text = " ".join(p for p in (loc.address, loc.venue_name) if p)
    m = POSTCODE_RE.search(text)
    if m:
        prefix, district = m.group(1).upper(), int(m.group(2))
        if prefix in ("EC", "WC"):
            return "central"
        # The "1" districts ring the centre (E1, SE1, SW1, W1, N1, NW1)
        if district == 1:
            return "central"
        if prefix == "E":
            return "east"
        if prefix in ("N", "NW"):
            return "north"
        if prefix in ("SE", "SW"):
            return "south"
        if prefix == "W":
            return "west"

    if loc.latitude is None or loc.longitude is None:
        return None
    return _area_from_geo(loc.latitude, loc.longitude)


def _area_from_geo(lat: float, lng: float) -> str:
    dy_km = (lat - CENTER_LAT) * 111.0
    dx_km = (lng - CENTER_LNG) * 111.0 * math.cos(math.radians(CENTER_LAT))
    if math.hypot(dx_km, dy_km) <= CENTRAL_RADIUS_KM:
        return "central"
    angle = math.degrees(math.atan2(dy_km, dx_km)) % 360
    if angle < 45 or angle >= 315:
        return "east"
    if angle < 135:
        return "north"
    if angle < 225:
        return "west"
    return "south"
