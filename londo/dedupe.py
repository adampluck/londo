from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from londo.models import Event

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")

# When the same event appears in several sources (or as multiple same-day
# ticket times), the canonical copy is the earliest start; ties break by
# this source order. Luma sits above Dandelion so a dual-listed gathering
# keeps the Luma registration URL and host name (often more accurate for
# featured/own events) while still merging Dandelion's missing fields.
SOURCE_PRIORITY = [
    "newspeak",
    "luma",
    "dandelion",
    "numinity",
    "eventbrite",
    "studysociety",
    "momence",
    "other",
]

# Tiny words that don't carry event identity across sources. Keep
# prepositions like "in"/"on" — "Yoga in the Park" needs them so a
# "… Beginners" twin still has three content tokens for containment.
_STOP = frozenset({"a", "an", "and", "or", "of", "the", "to", "for", "with", "by"})

# Starts within this window count as the same slot when titles only fuzzy-match.
_SAME_SLOT = timedelta(minutes=90)


def dedupe(events: list[Event]) -> list[Event]:
    """Assign dedupe keys, mark cross-source / same-day duplicates, and
    enrich the canonical event with any fields its duplicates have but it
    lacks.

    Exact normalised title on the same London day collapses to one listing.
    Near-duplicate titles (e.g. "Overnight Sound Healing Journey" vs
    "Overnight Gong Bath Sound Healing Journey") also collapse when they
    share the day and a similar start slot / venue. Earliest start wins;
    the ticket page usually still offers every slot.
    """
    for event in events:
        # Recompute from scratch so a re-scrape can un-mark rows that no
        # longer group with anything.
        event.duplicate_of = None
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
        logger.info("Marked %d duplicates (cross-source or same-day slots)", n_dupes)
    return events


def _group(events: list[Event]) -> list[list[Event]]:
    """Cluster events that share ANY match signal. external_ref is only set by
    some sources (luma/eventbrite/meetup/newspeak), so a Dandelion copy of a
    Luma event carries none; keying on external_ref alone would split the two
    even though their title+day matches. Union-find over the union of both
    keys, plus a same-day fuzzy title pass, means events need to agree on
    just one signal to be judged duplicates."""
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

    # Near-duplicate titles on the same day (and compatible time/venue)
    # that exact slug matching misses — e.g. one source inserts "Gong Bath".
    by_day: dict[str, list[int]] = {}
    for idx, event in enumerate(events):
        by_day.setdefault(_event_day(event), []).append(idx)

    for day, idxs in by_day.items():
        if day == "unknown" or len(idxs) < 2:
            continue
        for a in range(len(idxs)):
            ia = idxs[a]
            for b in range(a + 1, len(idxs)):
                ib = idxs[b]
                if find(ia) == find(ib):
                    continue
                if _near_duplicate(events[ia], events[ib]):
                    union(ia, ib)

    clusters: dict[int, list[Event]] = {}
    for idx, event in enumerate(events):
        clusters.setdefault(find(idx), []).append(event)
    return list(clusters.values())


def _match_keys(event: Event) -> list[str]:
    keys = [_title_day_key(event)]
    if event.external_ref:
        keys.append(event.external_ref)
    return keys


def _dedupe_key(event: Event) -> str:
    if event.external_ref:
        return event.external_ref
    return _title_day_key(event)


def _event_day(event: Event) -> str:
    if event.start_datetime:
        return event.start_datetime.astimezone(LONDON).date().isoformat()
    if event.start_date:
        return event.start_date.isoformat()
    return "unknown"


def _title_day_key(event: Event) -> str:
    """Normalised title + London calendar day.

    Same-day slots of one listing (and cross-source copies of it) share this
    key so only the earliest start is shown. Distinct gatherings on different
    days stay separate.
    """
    when = _event_day(event)
    slug = re.sub(r"[^a-z0-9]+", "", event.title.lower())
    return f"{slug}|{when}"


def _title_tokens(title: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", title.lower())
        if w not in _STOP and len(w) > 1
    }


def _title_slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def _titles_similar(a: str, b: str) -> bool:
    """True when two titles are the same gathering with different wording.

    Catches one source adding a method ("Gong Bath") or subtitle that the
    other omits, without collapsing unrelated short titles on the same day.
    """
    sa, sb = _title_slug(a), _title_slug(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True

    ratio = SequenceMatcher(None, sa, sb).ratio()
    if ratio >= 0.88:
        return True

    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    shared = ta & tb
    shorter, longer = (ta, tb) if len(ta) <= len(tb) else (tb, ta)

    # Longer title is the shorter one plus extras ("Gong Bath", "Beginners").
    if len(shorter) >= 3 and len(shared) >= 3 and len(shared) / len(shorter) >= 0.85:
        return True

    # Similar-length rewrites of the same name.
    union = ta | tb
    if len(shared) >= 3 and len(shared) / len(union) >= 0.75:
        return True

    # High string similarity, but only when there is enough shared content
    # that short pairs like "AI Meetup" / "AI Art Meetup" stay distinct.
    if ratio >= 0.82 and len(shorter) >= 3 and len(shared) >= 3:
        return True

    return False


def _start_utc(event: Event) -> datetime | None:
    if event.start_datetime is None:
        return None
    start = event.start_datetime
    if start.tzinfo is None:
        return start.replace(tzinfo=timezone.utc)
    return start.astimezone(timezone.utc)


def _starts_compatible(a: Event, b: Event) -> bool:
    """Fuzzy title matches only stick when starts are the same slot.

    Exact title+day matches (handled by key equality) still collapse multi-
    slot tickets; this gate is only for near-duplicate titles.
    """
    sa, sb = _start_utc(a), _start_utc(b)
    if sa is None or sb is None:
        # One side is date-only — same London day already required by caller.
        return True
    return abs(sa - sb) <= _SAME_SLOT


def _venue_slug(event: Event) -> str | None:
    if not event.location or not event.location.venue_name:
        return None
    slug = re.sub(r"[^a-z0-9]+", "", event.location.venue_name.lower())
    return slug or None


def _near_duplicate(a: Event, b: Event) -> bool:
    """Same-day near-duplicates across sources with slightly different titles."""
    if not _titles_similar(a.title, b.title):
        return False
    if not _starts_compatible(a, b):
        return False
    # If both name a venue and they clearly disagree, keep them separate
    # even when titles look close (two "Cacao Ceremony"s in different rooms).
    va, vb = _venue_slug(a), _venue_slug(b)
    if va and vb and va != vb:
        # Allow soft venue variants ("Colet House" vs "Colet House, London")
        shorter, longer = (va, vb) if len(va) <= len(vb) else (vb, va)
        if not longer.startswith(shorter) and SequenceMatcher(None, va, vb).ratio() < 0.85:
            return False
    return True


def _priority(event: Event) -> tuple[datetime, int, str]:
    """Earliest start wins; source rank breaks ties; source_id is stable last."""
    if event.start_datetime is not None:
        start = event.start_datetime
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
    elif event.start_date is not None:
        start = datetime(
            event.start_date.year,
            event.start_date.month,
            event.start_date.day,
            tzinfo=timezone.utc,
        )
    else:
        start = datetime.max.replace(tzinfo=timezone.utc)

    try:
        rank = SOURCE_PRIORITY.index(event.source)
    except ValueError:
        rank = len(SOURCE_PRIORITY)
    return (start, rank, event.source_id)


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
