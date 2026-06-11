from __future__ import annotations

import logging
import re
from zoneinfo import ZoneInfo

from londo.models import Event

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

# When the same event appears in several sources, the canonical copy comes
# from the earliest source in this list; the rest are marked duplicate_of it.
SOURCE_PRIORITY = ["newspeak", "dandelion", "numinity", "eventbrite", "luma", "other"]


def dedupe(events: list[Event]) -> list[Event]:
    """Assign dedupe keys, mark cross-source duplicates, and enrich the
    canonical event with any fields its duplicates have but it lacks."""
    for event in events:
        event.dedupe_key = _dedupe_key(event)

    groups: dict[str, list[Event]] = {}
    for event in events:
        groups.setdefault(event.dedupe_key, []).append(event)

    n_dupes = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=_priority)
        canonical, rest = group[0], group[1:]
        for dup in rest:
            dup.duplicate_of = f"{canonical.source}:{canonical.source_id}"
            _merge_missing(canonical, dup)
            n_dupes += 1

    if n_dupes:
        logger.info("Marked %d cross-source duplicates", n_dupes)
    return events


def _dedupe_key(event: Event) -> str:
    if event.external_ref:
        return event.external_ref

    if event.start_datetime:
        day = event.start_datetime.astimezone(LONDON).date().isoformat()
    elif event.start_date:
        day = event.start_date.isoformat()
    else:
        day = "unknown"

    slug = re.sub(r"[^a-z0-9]+", "", event.title.lower())
    return f"{slug}|{day}"


def _priority(event: Event) -> tuple[int, str]:
    try:
        rank = SOURCE_PRIORITY.index(event.source)
    except ValueError:
        rank = len(SOURCE_PRIORITY)
    return (rank, event.source_id)


def _merge_missing(canonical: Event, dup: Event) -> None:
    if not canonical.image_url and dup.image_url:
        canonical.image_url = dup.image_url
    if not canonical.description and dup.description:
        canonical.description = dup.description
    if not canonical.tags and dup.tags:
        canonical.tags = dup.tags
    if not canonical.price_tiers and dup.price_tiers:
        canonical.price_tiers = dup.price_tiers
        canonical.is_free = dup.is_free
    if not canonical.organizer and dup.organizer:
        canonical.organizer = dup.organizer
    if canonical.location and dup.location:
        if canonical.location.latitude is None and dup.location.latitude is not None:
            canonical.location.latitude = dup.location.latitude
            canonical.location.longitude = dup.location.longitude
    elif not canonical.location and dup.location:
        canonical.location = dup.location
