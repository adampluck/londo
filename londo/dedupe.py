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
    "tickettailor",
    "momence",
    "jimeaton",
    "soullinkhub",  # aggregator copy: any native ticket-page copy is better
    "other",
    "whatsapp",  # flyer + blurb from a chat: any page-backed copy is better
]

# Tiny words that don't carry event identity across sources. Keep
# prepositions like "in"/"on" — "Yoga in the Park" needs them so a
# "… Beginners" twin still has three content tokens for containment.
_STOP = frozenset({"a", "an", "and", "or", "of", "the", "to", "for", "with", "by"})

# Starts within this window count as the same slot when titles only fuzzy-match.
_SAME_SLOT = timedelta(minutes=90)

# UK postcode: the one address token every source spells the same way.
_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?) ?(\d[A-Z]{2})\b", re.I)


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


# A chat-extracted title is the flyer's headline, often shorter than the
# ticket page's ("The Sovereign Woman" vs "The Sovereign Woman: Tantric
# Women's Circle"), so with too few tokens for the shared-token rules
# above. Containment of one slug in the other is enough when one side is a
# chat listing — as long as the shorter is not a generic word or two.
_CONTAIN_MIN = 10


def _chat_title_contained(a: Event, b: Event) -> bool:
    if "whatsapp" not in (a.source, b.source):
        return False
    sa, sb = _title_slug(a.title), _title_slug(b.title)
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(shorter) >= _CONTAIN_MIN and shorter in longer


def _postcode(event: Event) -> str | None:
    if not event.location or not event.location.address:
        return None
    m = _POSTCODE_RE.search(event.location.address)
    return f"{m.group(1)}{m.group(2)}".upper() if m else None


def _place_slug(event: Event) -> str:
    loc = event.location
    if not loc:
        return ""
    return re.sub(r"[^a-z0-9]+", "", f"{loc.venue_name or ''} {loc.address or ''}".lower())


def _same_place(a: Event, b: Event) -> bool:
    """Same postcode, or one side's venue name sits inside the other's
    venue + address (SoulLinkHub folds the venue into its address)."""
    pa, pb = _postcode(a), _postcode(b)
    if pa and pb:
        return pa == pb
    for x, y in ((a, b), (b, a)):
        vx = _venue_slug(x)
        if vx and len(vx) >= 8 and vx in _place_slug(y):
            return True
    return False


def _same_instant(a: Event, b: Event) -> bool:
    """Identical start, and identical end when both give one."""
    sa, sb = _start_utc(a), _start_utc(b)
    if sa is None or sb is None or sa != sb:
        return False
    ea, eb = a.end_datetime, b.end_datetime
    if ea is None or eb is None:
        return True
    return ea.astimezone(timezone.utc) == eb.astimezone(timezone.utc)


def _text_tokens(event: Event) -> set[str]:
    return _title_tokens(f"{event.title} {event.description or ''}")


def _cross_referenced(a: Event, b: Event) -> bool:
    """One listing's whole title, or its named facilitator, appears in the
    other's title + description ("5Rhythms with Flow" inside "SACR(w)ED –
    5Rhythms® Midweek Ritual with Flow Vulk")."""
    for x, y in ((a, b), (b, a)):
        text = _text_tokens(y)
        title = _title_tokens(x.title)
        if len(title) >= 2 and title <= text:
            return True
        host = _title_tokens(x.organizer.name) if x.organizer else set()
        if len(host) >= 2 and host <= text:
            return True
    return False


def _co_listed(a: Event, b: Event) -> bool:
    """Two sources naming the same gathering differently — a facilitator's
    own event name on an aggregator vs the venue's series name on its ticket
    page. With no title overlap to go on, all of: different sources, the
    same instant, the same place, and one listing's title or host named in
    full by the other."""
    return (
        a.source != b.source
        and _same_instant(a, b)
        and _same_place(a, b)
        and _cross_referenced(a, b)
    )


# Ends further apart than this are two sessions, not one night relisted.
_END_DRIFT = timedelta(minutes=30)


def _same_host(a: Event, b: Event) -> bool:
    """One organiser name inside the other ("Ecstatic Dance London" vs
    "Ecstatic Dance London & URUBU Wellbeing Events")."""
    if not a.organizer or not b.organizer:
        return False
    sa, sb = _title_slug(a.organizer.name or ""), _title_slug(b.organizer.name or "")
    shorter, longer = (sa, sb) if len(sa) <= len(sb) else (sb, sa)
    return len(shorter) >= 8 and shorter in longer


# "Holiday workshops: Kitchen Lab (Year 1 & 2)" — a programme name, then
# the session.
_PROGRAMME_RE = re.compile(r"^(.{6,}?)\s*(?::|\s[-–—]\s)")


def _programme_siblings(a: Event, b: Event) -> bool:
    """Two named sessions of one programme, run side by side in different
    rooms (the Royal Institution's holiday workshops by year group). Only
    within one source: across sources a shared programme name is the same
    night cross-posted ("System Reset - …" on Dandelion and Eventbrite)."""
    if a.source != b.source:
        return False
    ma, mb = _PROGRAMME_RE.match(a.title), _PROGRAMME_RE.match(b.title)
    return bool(ma and mb and _title_slug(ma.group(1)) == _title_slug(mb.group(1)))


def _host_relisted(a: Event, b: Event) -> bool:
    """One host selling the same night twice under different names — a
    Dandelion listing and a themed Eventbrite one, or two Eventbrite pages
    for one party. Same host, place and start, and both give an end within
    half an hour of each other: a host running parallel rooms (the Study
    Society at Colet House) differs on the end, or leaves one out."""
    sa, sb = _start_utc(a), _start_utc(b)
    if sa is None or sa != sb:
        return False
    ea, eb = a.end_datetime, b.end_datetime
    if ea is None or eb is None:
        return False
    if abs(ea.astimezone(timezone.utc) - eb.astimezone(timezone.utc)) > _END_DRIFT:
        return False
    return _same_host(a, b) and _same_place(a, b) and not _programme_siblings(a, b)


def _near_duplicate(a: Event, b: Event) -> bool:
    """Same-day near-duplicates across sources with slightly different titles."""
    if not _titles_similar(a.title, b.title) and not _chat_title_contained(a, b):
        return _co_listed(a, b) or _host_relisted(a, b)
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
