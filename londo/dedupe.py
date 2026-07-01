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

    groups = _group(events)

    n_dupes = 0
    for group in groups:
        if len(group) < 2:
            continue
        group.sort(key=_priority)
        canonical, rest = group[0], group[1:]
        for dup in rest:
            # same DB row reached twice (e.g. lu.ma/x and luma.com/x):
            # merge fields but never mark a row a duplicate of itself
            same_row = (dup.source, dup.source_id) == (
                canonical.source,
                canonical.source_id,
            )
            if not same_row:
                dup.duplicate_of = f"{canonical.source}:{canonical.source_id}"
                n_dupes += 1
            _merge_missing(canonical, dup)

    if n_dupes:
        logger.info("Marked %d cross-source duplicates", n_dupes)
    return events


def _group(events: list[Event]) -> list[list[Event]]:
    """Cluster events that share ANY match signal. external_ref is only set by
    some sources (luma/eventbrite/meetup/newspeak), so a Dandelion copy of a
    Luma event carries none; keying on external_ref alone would split the two
    even though their title+time matches. Union-find over the union of both keys
    means events need to agree on just one signal to be judged duplicates."""
    parent: dict[int, int] = {i: i for i in range(len(events))}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    seen: dict[str, int] = {}
    for idx, event in enumerate(events):
        for key in _match_keys(event):
            if key in seen:
                union(seen[key], idx)
            else:
                seen[key] = idx

    clusters: dict[int, list[Event]] = {}
    for idx, event in enumerate(events):
        clusters.setdefault(find(idx), []).append(event)
    return list(clusters.values())


def _match_keys(event: Event) -> list[str]:
    keys = [_title_time_key(event)]
    if event.external_ref:
        keys.append(event.external_ref)
    return keys


def _dedupe_key(event: Event) -> str:
    if event.external_ref:
        return event.external_ref
    return _title_time_key(event)


def _title_time_key(event: Event) -> str:
    # Keyed to the minute, not the day: some venues run the same event several
    # times a day (different sessions), and those must stay separate. Genuine
    # cross-source duplicates of a single event share an exact start time.
    if event.start_datetime:
        when = event.start_datetime.astimezone(LONDON).isoformat(timespec="minutes")
    elif event.start_date:
        when = event.start_date.isoformat()
    else:
        when = "unknown"

    slug = re.sub(r"[^a-z0-9]+", "", event.title.lower())
    return f"{slug}|{when}"


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
