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

    # Columns added by migrations/2026-06-12_enrichment_and_submissions.sql.
    # If that migration hasn't been applied yet, upserts retry without them.
    ENRICHMENT_COLUMNS = (
        "category", "topics", "traits", "hook", "quality_score", "area",
        "enriched_at",
    )

    def upsert_events(self, events: list[Event]) -> int:
        # Postgres rejects a batch that touches the same row twice
        # ("ON CONFLICT DO UPDATE cannot affect row a second time"), and the
        # same event can be reached via different URLs. First copy wins —
        # the dedupe pass puts the canonical (unmarked) copy first.
        unique: dict[tuple[str, str], dict] = {}
        for event in events:
            unique.setdefault((event.source, event.source_id), _event_to_row(event))
        rows = list(unique.values())
        if len(rows) < len(events):
            logger.info(
                "Collapsed %d same-row events in batch", len(events) - len(rows)
            )
        endpoint = f"{self.url}/rest/v1/events?on_conflict=source,source_id"

        strip_enrichment = False
        written = 0
        for i in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[i : i + CHUNK_SIZE]
            if strip_enrichment:
                chunk = [_without_enrichment(r) for r in chunk]
            response = self.session.post(
                endpoint,
                json=chunk,
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=60,
            )
            if response.status_code == 400 and not strip_enrichment and any(
                c in response.text for c in self.ENRICHMENT_COLUMNS
            ):
                logger.warning(
                    "Events table is missing enrichment columns - apply "
                    "migrations/2026-06-12_enrichment_and_submissions.sql. "
                    "Retrying without enrichment fields."
                )
                strip_enrichment = True
                response = self.session.post(
                    endpoint,
                    json=[_without_enrichment(r) for r in chunk],
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

    def fetch_enrichment(self) -> dict[tuple[str, str], dict]:
        """Existing enrichment keyed by (source, source_id), so the scrape
        only pays for LLM calls on events it hasn't classified before."""
        cols = "source,source_id," + ",".join(self.ENRICHMENT_COLUMNS)
        response = self.session.get(
            f"{self.url}/rest/v1/events?select={cols}&enriched_at=not.is.null",
            timeout=60,
        )
        if response.status_code == 400:  # migration not applied yet
            return {}
        response.raise_for_status()
        return {(r["source"], r["source_id"]): r for r in response.json()}

    def fetch_pending_submissions(self) -> list[dict]:
        response = self.session.get(
            f"{self.url}/rest/v1/submissions?status=eq.pending&select=*"
            "&order=created_at.asc&limit=100",
            timeout=30,
        )
        if response.status_code in (400, 404):  # table not created yet
            return []
        response.raise_for_status()
        return response.json()

    def purge_old_submissions(self, days: int = 30) -> None:
        """Delete resolved submissions after a while: keeps the table small
        under flooding, and frees their URLs for honest resubmission."""
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        response = self.session.delete(
            f"{self.url}/rest/v1/submissions"
            f"?status=neq.pending&reviewed_at=lt.{cutoff}",
            headers={"Prefer": "return=minimal"},
            timeout=30,
        )
        if response.status_code not in (400, 404):  # table may not exist yet
            response.raise_for_status()

    def resolve_submission(self, submission_id: str, status: str, reason: str) -> None:
        response = self.session.patch(
            f"{self.url}/rest/v1/submissions?id=eq.{submission_id}",
            json={
                "status": status,
                "reason": reason,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            },
            headers={"Prefer": "return=minimal"},
            timeout=30,
        )
        response.raise_for_status()

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
        "category": event.category,
        "topics": event.topics,
        "traits": event.traits,
        "hook": event.hook,
        "quality_score": event.quality_score,
        "area": event.area,
        "enriched_at": event.enriched_at.isoformat() if event.enriched_at else None,
        "last_seen_at": event.scraped_at.isoformat(),
    }


def _without_enrichment(row: dict) -> dict:
    return {
        k: v for k, v in row.items()
        if k not in SupabaseStore.ENRICHMENT_COLUMNS
    }
