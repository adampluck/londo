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
