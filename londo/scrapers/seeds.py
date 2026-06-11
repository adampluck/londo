from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from londo.links import LinkFetcher
from londo.models import Event
from londo.scrapers.base import BaseScraper
from londo.storage import SupabaseStore

logger = logging.getLogger(__name__)


class SeedsScraper(BaseScraper):
    """Re-fetches chat-ingested URLs stored in the Supabase seeds table.

    Keeps their events fresh (last_seen_at) and updated, and deactivates
    seeds once every event behind them has passed. Requires Supabase
    credentials, so it only runs when storing to Supabase.
    """

    source_name = "seeds"

    def scrape(self) -> list[Event]:
        store = SupabaseStore()
        seeds = store.fetch_active_seeds()
        logger.info("Found %d active seeds", len(seeds))
        if not seeds:
            return []

        fetcher = LinkFetcher(rate_limit=self.rate_limit)
        now = datetime.now(timezone.utc)
        events: list[Event] = []
        updates: list[dict] = []
        for seed in seeds:
            fetched = fetcher.fetch(seed["url"])
            events.extend(fetched)

            starts = [e.start_datetime for e in fetched if e.start_datetime]
            latest = max(starts) if starts else None
            # No future event behind this URL any more -> stop refreshing.
            # Grace period covers events fetched while in progress.
            still_active = bool(latest and latest > now - timedelta(days=1))
            updates.append(
                {
                    "url": seed["url"],
                    "kind": seed.get("kind"),
                    "added_by": seed.get("added_by") or "whatsapp",
                    "last_fetched_at": now.isoformat(),
                    "event_start_at": latest.isoformat() if latest else seed.get("event_start_at"),
                    "active": still_active,
                }
            )

        store.upsert_seeds(updates)
        retired = sum(1 for u in updates if not u["active"])
        if retired:
            logger.info("Retired %d past seeds", retired)
        logger.info("Scraped %d events from %d seeds", len(events), len(seeds))
        return events
