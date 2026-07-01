"""One-off: fetch events with missing topics, re-enrich, patch back."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from londo.enrich import enrich_events
from londo.models import Event, Location, Organizer, PriceTier
from londo.storage import SupabaseStore, load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def fetch_missing_topics(store: SupabaseStore) -> list[dict]:
    cols = (
        "source,source_id,source_url,title,description,tags,"
        "venue_name,address,organizer_name,organizer_url,"
        "price_min,price_max,is_free,start_at,end_at,"
        "category,traits,hook,quality_score,area,enriched_at"
    )
    # Topics null or empty array, and event hasn't passed yet
    response = store.session.get(
        f"{store.url}/rest/v1/events"
        f"?select={cols}"
        f"&or=(topics.is.null,topics.eq.{{}})"
        f"&start_at=gte.{datetime.now(timezone.utc).date().isoformat()}"
        f"&order=start_at.asc"
        f"&limit=500",
        timeout=60,
    )
    response.raise_for_status()
    rows = response.json()
    logger.info("Found %d events with missing topics", len(rows))
    return rows


def row_to_event(row: dict) -> Event:
    loc = None
    if row.get("address") or row.get("venue_name"):
        loc = Location(
            venue_name=row.get("venue_name"),
            address=row.get("address") or "",
        )
    org = None
    if row.get("organizer_name"):
        org = Organizer(name=row["organizer_name"], url=row.get("organizer_url"))

    price_tiers = []
    if row.get("price_min") is not None:
        price_tiers.append(PriceTier(name="ticket", amount=row["price_min"]))

    start_dt = None
    if row.get("start_at"):
        start_dt = datetime.fromisoformat(row["start_at"].replace("Z", "+00:00"))

    enriched_at = None
    if row.get("enriched_at"):
        enriched_at = datetime.fromisoformat(row["enriched_at"].replace("Z", "+00:00"))

    return Event(
        source=row["source"],
        source_id=row["source_id"],
        source_url=row["source_url"],
        title=row["title"],
        description=row.get("description"),
        tags=row.get("tags") or [],
        location=loc,
        organizer=org,
        price_tiers=price_tiers,
        is_free=row.get("is_free") or False,
        start_datetime=start_dt,
        # Carry over existing enrichment so we only re-do the topics
        category=row.get("category"),
        traits=row.get("traits") or [],
        hook=row.get("hook"),
        quality_score=row.get("quality_score"),
        area=row.get("area"),
        enriched_at=enriched_at,
        scraped_at=datetime.now(timezone.utc),
    )


def patch_enrichment(store: SupabaseStore, event: Event) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "topics": event.topics,
        "category": event.category,
        "traits": event.traits,
        "hook": event.hook,
        "quality_score": event.quality_score,
        "area": event.area,
        "enriched_at": (event.enriched_at or now).isoformat(),
    }
    response = store.session.patch(
        f"{store.url}/rest/v1/events"
        f"?source=eq.{event.source}&source_id=eq.{event.source_id}",
        json=payload,
        headers={"Prefer": "return=minimal"},
        timeout=30,
    )
    response.raise_for_status()


def main() -> None:
    load_dotenv()
    store = SupabaseStore()

    rows = fetch_missing_topics(store)
    if not rows:
        print("Nothing to do.")
        return

    events = [row_to_event(r) for r in rows]

    # Pass empty existing dict so enrich_events re-classifies everything
    calls = enrich_events(events, existing={})
    print(f"Made {calls} LLM calls")

    patched = 0
    for event in events:
        if event.topics:
            patch_enrichment(store, event)
            patched += 1
            logger.info("%s -> %s", event.title, event.topics)

    print(f"Patched {patched}/{len(events)} events with topics")


if __name__ == "__main__":
    main()
