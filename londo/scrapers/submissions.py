from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import re

from londo.geo import POSTCODE_RE
from londo.links import LinkFetcher, classify_url
from londo.models import Event
from londo.scrapers.base import BaseScraper
from londo.storage import SupabaseStore

logger = logging.getLogger(__name__)

LONDON_RE = re.compile(r"\blondon\b", re.I)
# generous Greater London bounding box
LONDON_BBOX = (51.25, 51.75, -0.6, 0.35)


def _in_london(event: Event) -> bool:
    """Anyone can submit a link, so unlike curated sources the result must
    prove it's actually in London before it gets published."""
    loc = event.location
    if loc is None:
        return False
    if loc.latitude is not None and loc.longitude is not None:
        s, n, w, e = LONDON_BBOX
        if s <= loc.latitude <= n and w <= loc.longitude <= e:
            return True
    text = " ".join(
        p for p in (loc.venue_name, loc.address, loc.city) if p
    )
    return bool(LONDON_RE.search(text) or POSTCODE_RE.search(text))


class SubmissionsScraper(BaseScraper):
    """Validates community-submitted event links ("know an event? paste a
    link") and promotes good ones to the seeds table.

    Anyone can insert a pending row via the website; nothing is published
    until this pass fetches the URL and the event passes the same
    completeness gate as every other source. Requires Supabase credentials.
    """

    source_name = "submissions"

    def scrape(self) -> list[Event]:
        store = SupabaseStore()
        store.purge_old_submissions(days=30)
        pending = store.fetch_pending_submissions()
        if not pending:
            return []
        logger.info("Reviewing %d pending submissions", len(pending))

        fetcher = LinkFetcher(rate_limit=self.rate_limit)
        cutoff = datetime.now(timezone.utc) - timedelta(days=1)
        events: list[Event] = []
        seeds: list[dict] = []
        seen_urls: set[str] = set()
        for sub in pending:
            url = (sub.get("url") or "").strip()
            classified = classify_url(url)
            if url in seen_urls or classified is None:
                store.resolve_submission(
                    sub["id"], "rejected",
                    "duplicate" if url in seen_urls else "not an event link",
                )
                continue
            seen_urls.add(url)

            fetched = [
                e for e in fetcher.fetch(url)
                if not e.is_online
                and _in_london(e)
                and (e.start_datetime is None or e.start_datetime >= cutoff)
            ]
            if not fetched:
                store.resolve_submission(
                    sub["id"], "rejected",
                    "no upcoming in-person London event with full details "
                    "at this link",
                )
                continue

            events.extend(fetched)
            starts = [e.start_datetime for e in fetched if e.start_datetime]
            seeds.append(
                {
                    "url": url,
                    "kind": classified[0],
                    "added_by": "web",
                    "event_start_at": max(starts).isoformat() if starts else None,
                }
            )
            store.resolve_submission(
                sub["id"], "accepted", f"added {fetched[0].title}"
            )
            logger.info("Accepted submission: %s", fetched[0].title)

        if seeds:
            store.upsert_seeds(seeds)
        logger.info(
            "Submissions: %d accepted, %d rejected",
            len(seeds), len(pending) - len(seeds),
        )
        return events
