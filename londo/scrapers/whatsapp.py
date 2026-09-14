from __future__ import annotations

import logging

from londo.models import Event
from londo.scrapers.base import BaseScraper
from londo.storage import SupabaseStore

logger = logging.getLogger(__name__)


class WhatsAppScraper(BaseScraper):
    """Re-emits upcoming chat-ingested events (`londo ingest-whatsapp`).

    Those listings come from a flyer and a blurb, not a page that can be
    re-fetched, so the daily scrape reads them back from Supabase: the
    upsert keeps their last_seen_at fresh, dedupe marks them duplicate of
    any platform listing that appears later, and enrichment is reused.
    Requires Supabase credentials.
    """

    source_name = "whatsapp"

    def scrape(self) -> list[Event]:
        events = SupabaseStore().fetch_upcoming_events("whatsapp")
        logger.info("Carried forward %d upcoming chat events", len(events))
        return events
