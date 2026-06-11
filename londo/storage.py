from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

from londo.models import Event

logger = logging.getLogger(__name__)

CHUNK_SIZE = 100


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader — existing environment variables win."""
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class SupabaseStore:
    """Writes events to a Supabase `events` table via the PostgREST API."""

    def __init__(self, url: str | None = None, service_key: str | None = None):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.url or not self.key:
            raise ValueError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set "
                "(in the environment or a .env file)"
            )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            }
        )

    def upsert_events(self, events: list[Event]) -> int:
        rows = [_event_to_row(e) for e in events]
        endpoint = f"{self.url}/rest/v1/events?on_conflict=source,source_id"

        written = 0
        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i : i + CHUNK_SIZE]
            response = self.session.post(
                endpoint,
                json=chunk,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=60,
            )
            if response.status_code >= 400:
                logger.error(
                    "Upsert failed (%d): %s", response.status_code, response.text
                )
                response.raise_for_status()
            written += len(chunk)

        logger.info("Upserted %d events to Supabase", written)
        return written

    def upsert_seeds(self, seeds: list[dict]) -> None:
        if not seeds:
            return
        response = self.session.post(
            f"{self.url}/rest/v1/seeds?on_conflict=url",
            json=seeds,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            timeout=30,
        )
        response.raise_for_status()
        logger.info("Upserted %d seeds", len(seeds))

    def fetch_active_seeds(self) -> list[dict]:
        response = self.session.get(
            f"{self.url}/rest/v1/seeds?active=is.true&select=*", timeout=30
        )
        response.raise_for_status()
        return response.json()


def _event_to_row(event: Event) -> dict:
    loc = event.location
    prices = [float(t.amount) for t in event.price_tiers]

    start_at = event.start_datetime
    if start_at is None and event.start_date is not None:
        start_at = datetime(
            event.start_date.year,
            event.start_date.month,
            event.start_date.day,
            tzinfo=timezone.utc,
        )

    return {
        "source": event.source,
        "source_id": event.source_id,
        "source_url": event.source_url,
        "external_ref": event.external_ref,
        "dedupe_key": event.dedupe_key,
        "duplicate_of": event.duplicate_of,
        "title": event.title,
        "description": event.description,
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": event.end_datetime.isoformat() if event.end_datetime else None,
        "is_all_day": event.is_all_day,
        "venue_name": loc.venue_name if loc else None,
        "address": loc.address if loc else None,
        "city": (loc.city if loc else None) or "London",
        "latitude": loc.latitude if loc else None,
        "longitude": loc.longitude if loc else None,
        "is_online": event.is_online,
        "image_url": event.image_url,
        "tags": event.tags,
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "is_free": event.is_free or (bool(prices) and max(prices) == 0),
        "organizer_name": event.organizer.name if event.organizer else None,
        "organizer_url": event.organizer.url if event.organizer else None,
        "last_seen_at": event.scraped_at.isoformat(),
    }
