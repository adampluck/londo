from __future__ import annotations

import logging
import os
import re

import click

from londo.dedupe import dedupe
from londo.models import Event
from londo.output import write_events
from londo.scrapers.dandelion import DandelionScraper
from londo.scrapers.eventbrite import EventbriteListingsScraper, NuminityScraper
from londo.scrapers.luma import LumaScraper
from londo.scrapers.meetup import MeetupScraper
from londo.scrapers.momence import MomenceScraper
from londo.scrapers.newspeak import NewspeakScraper
from londo.scrapers.psycalendar import PsyCalendarScraper
from londo.scrapers.seeds import SeedsScraper
from londo.scrapers.studysociety import StudySocietyScraper
from londo.scrapers.submissions import SubmissionsScraper
from londo.storage import SupabaseStore, load_dotenv

SCRAPERS = {
    "dandelion": DandelionScraper,
    "luma": LumaScraper,
    "meetup": MeetupScraper,
    "momence": MomenceScraper,  # host workshops only; classes are skipped
    "newspeak": NewspeakScraper,
    "numinity": NuminityScraper,
    "eventbrite": EventbriteListingsScraper,
    "psycalendar": PsyCalendarScraper,  # aggregator; events land under 'other'
    "studysociety": StudySocietyScraper,
    "seeds": SeedsScraper,  # chat-ingested URLs; needs Supabase credentials
    "submissions": SubmissionsScraper,  # community links; needs Supabase creds
}

SUPABASE_ONLY_SOURCES = ("seeds", "submissions")

# Event types we never list, whatever the source (matched against title).
UNWANTED_TITLE_RE = re.compile(r"book[\s-]*signing", re.IGNORECASE)


def drop_unwanted(events: list[Event]) -> list[Event]:
    kept = [e for e in events if not UNWANTED_TITLE_RE.search(e.title or "")]
    dropped = len(events) - len(kept)
    if dropped:
        click.echo(f"Dropped {dropped} unwanted event(s) (book signings)")
    return kept


@click.group()
def cli() -> None:
    """Londo - London non-mainstream event aggregator."""


@cli.command()
@click.option(
    "--source",
    "-s",
    type=click.Choice(list(SCRAPERS.keys())),
    help="Scrape a specific source (default: all).",
)
@click.option(
    "--store",
    type=click.Choice(["json", "supabase", "both"]),
    default="json",
    help="Where to write the results.",
)
@click.option(
    "--output-dir",
    "-o",
    default="data",
    help="Output directory for JSON files.",
)
@click.option(
    "--rate-limit",
    "-r",
    default=1.0,
    type=float,
    help="Seconds between requests.",
)
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging.")
def scrape(
    source: str | None,
    store: str,
    output_dir: str,
    rate_limit: float,
    verbose: bool,
) -> None:
    """Scrape events from registered sources, dedupe, and store."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()

    sources = [source] if source else list(SCRAPERS.keys())

    all_events: list[Event] = []
    for src in sources:
        if src in SUPABASE_ONLY_SOURCES and not os.environ.get("SUPABASE_URL"):
            click.echo(f"Skipping {src} (no Supabase credentials)")
            continue
        scraper = SCRAPERS[src](rate_limit=rate_limit)
        click.echo(f"Scraping {src}...")
        try:
            events = scraper.scrape()
        except Exception as exc:
            # one broken source shouldn't lose the others' results
            logging.getLogger(__name__).exception("Scraper %s failed", src)
            click.echo(f"  FAILED: {exc}", err=True)
            continue
        click.echo(f"  {len(events)} events")
        all_events.extend(events)

    all_events = drop_unwanted(all_events)

    if not all_events:
        click.echo("No events scraped.")
        raise SystemExit(1)

    dedupe(all_events)
    n_dupes = sum(1 for e in all_events if e.duplicate_of)
    click.echo(f"Total: {len(all_events)} events ({n_dupes} cross-source duplicates)")

    from londo.enrich import enrich_events

    existing = {}
    if os.environ.get("SUPABASE_URL"):
        try:
            existing = SupabaseStore().fetch_enrichment()
        except Exception:
            logging.getLogger(__name__).exception("Could not fetch enrichment")
    calls = enrich_events(all_events, existing=existing)
    click.echo(f"Enriched: {calls} new LLM classifications")

    if store in ("json", "both"):
        filepath = write_events(all_events, "all", output_dir)
        click.echo(f"Wrote JSON -> {filepath}")

    if store in ("supabase", "both"):
        supabase = SupabaseStore()
        written = supabase.upsert_events(all_events)
        click.echo(f"Upserted {written} events to Supabase")


@cli.command("ingest-whatsapp")
@click.argument("export_file", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--store",
    type=click.Choice(["json", "supabase", "both"]),
    default="supabase",
    help="Where to write the results.",
)
@click.option("--output-dir", "-o", default="data")
@click.option("--rate-limit", "-r", default=1.0, type=float)
@click.option("--verbose", "-v", is_flag=True)
def ingest_whatsapp(
    export_file: str,
    store: str,
    output_dir: str,
    rate_limit: float,
    verbose: bool,
) -> None:
    """Extract event links from a WhatsApp chat export (.txt) and ingest them.

    Luma/Eventbrite/Dandelion links are fetched from their platforms;
    anything else is tried via schema.org metadata and stored as 'other'
    when the page has full details (date, time, location).
    """
    from pathlib import Path

    from londo.links import LinkFetcher, classify_url
    from londo.whatsapp import extract_urls

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()

    text = Path(export_file).read_text(encoding="utf-8", errors="replace")
    urls = extract_urls(text)
    candidates = [u for u in urls if classify_url(u) is not None]
    click.echo(f"Found {len(urls)} links, {len(candidates)} possible event links")

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    fetcher = LinkFetcher(rate_limit=rate_limit)
    all_events: list[Event] = []
    seeds: list[dict] = []
    seen_keys: set[tuple[str, str]] = set()
    for url in candidates:
        kind, key = classify_url(url)
        if (kind, key) in seen_keys:
            continue
        seen_keys.add((kind, key))
        events = fetcher.fetch(url)
        events = [
            e
            for e in events
            if e.start_datetime is None or e.start_datetime >= cutoff
        ]
        if not events:
            continue
        all_events.extend(events)
        starts = [e.start_datetime for e in events if e.start_datetime]
        seeds.append(
            {
                "url": url,
                "kind": kind,
                "added_by": "whatsapp",
                "event_start_at": max(starts).isoformat() if starts else None,
            }
        )
        click.echo(f"  [{kind}] {events[0].title} ({len(events)} event(s))")

    all_events = drop_unwanted(all_events)

    if not all_events:
        click.echo("No usable events found in this export.")
        return

    dedupe(all_events)
    click.echo(f"Total: {len(all_events)} events from {len(seeds)} links")

    if store in ("json", "both"):
        filepath = write_events(all_events, "whatsapp", output_dir)
        click.echo(f"Wrote JSON -> {filepath}")

    if store in ("supabase", "both"):
        supabase = SupabaseStore()
        supabase.upsert_events(all_events)
        supabase.upsert_seeds(seeds)
        click.echo(
            f"Upserted {len(all_events)} events and {len(seeds)} seeds to Supabase"
        )


@cli.command("seed")
@click.argument("urls", nargs=-1, required=True)
@click.option(
    "--store",
    type=click.Choice(["json", "supabase", "both"]),
    default="supabase",
    help="Where to write the results (default: supabase).",
)
@click.option("--output-dir", "-o", default="data")
@click.option("--rate-limit", "-r", default=1.0, type=float)
@click.option(
    "--seed-only",
    is_flag=True,
    help="Only upsert the seed row(s); skip fetch/enrich for now.",
)
@click.option("--verbose", "-v", is_flag=True)
def seed(
    urls: tuple[str, ...],
    store: str,
    output_dir: str,
    rate_limit: float,
    seed_only: bool,
    verbose: bool,
) -> None:
    """Add event URL(s) to the seeds table so the daily scrape keeps them fresh.

    Fetches each link now (unless --seed-only), enriches, and upserts both the
    events and a seed row (added_by=cli). Same shared pool as WhatsApp ingest
    and web submissions — londo and psyconnect both see matching events.
    """
    from datetime import datetime, timedelta, timezone

    from londo.links import LinkFetcher, classify_url

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()

    if store in ("supabase", "both") and not os.environ.get("SUPABASE_URL"):
        click.echo(
            "SUPABASE_URL not set. Copy .env.example to .env and fill credentials.",
            err=True,
        )
        raise SystemExit(1)

    classified: list[tuple[str, str, str]] = []  # (url, kind, key)
    seen_keys: set[tuple[str, str]] = set()
    for raw in urls:
        url = raw.strip()
        result = classify_url(url)
        if result is None:
            click.echo(f"  skip (not an event link): {url}", err=True)
            continue
        kind, key = result
        if (kind, key) in seen_keys:
            click.echo(f"  skip (duplicate): {url}")
            continue
        seen_keys.add((kind, key))
        classified.append((url, kind, key))

    if not classified:
        click.echo("No usable event links.", err=True)
        raise SystemExit(1)

    if seed_only:
        seeds = [
            {
                "url": url,
                "kind": kind,
                "added_by": "cli",
                "active": True,
            }
            for url, kind, _ in classified
        ]
        if store in ("supabase", "both"):
            SupabaseStore().upsert_seeds(seeds)
            click.echo(f"Upserted {len(seeds)} seed(s) to Supabase (fetch skipped)")
        for url, kind, _ in classified:
            click.echo(f"  [{kind}] {url}")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    fetcher = LinkFetcher(rate_limit=rate_limit)
    all_events: list[Event] = []
    seeds: list[dict] = []

    for url, kind, _ in classified:
        events = fetcher.fetch(url)
        events = [
            e
            for e in events
            if e.start_datetime is None or e.start_datetime >= cutoff
        ]
        starts = [e.start_datetime for e in events if e.start_datetime]
        seeds.append(
            {
                "url": url,
                "kind": kind,
                "added_by": "cli",
                "active": True,
                "event_start_at": max(starts).isoformat() if starts else None,
                "last_fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        if not events:
            click.echo(f"  [{kind}] no upcoming event details yet — seed kept: {url}")
            continue
        all_events.extend(events)
        click.echo(f"  [{kind}] {events[0].title} ({len(events)} event(s))")

    all_events = drop_unwanted(all_events)

    if all_events:
        dedupe(all_events)
        from londo.enrich import enrich_events

        existing = {}
        if os.environ.get("SUPABASE_URL"):
            try:
                existing = SupabaseStore().fetch_enrichment()
            except Exception:
                logging.getLogger(__name__).exception("Could not fetch enrichment")
        calls = enrich_events(all_events, existing=existing)
        click.echo(
            f"Total: {len(all_events)} event(s) from {len(seeds)} link(s); "
            f"enriched {calls} new"
        )
    else:
        click.echo(
            "No events fetched yet; seed row(s) will be retried on the next scrape."
        )

    if store in ("json", "both") and all_events:
        filepath = write_events(all_events, "seed", output_dir)
        click.echo(f"Wrote JSON -> {filepath}")

    if store in ("supabase", "both"):
        supabase = SupabaseStore()
        if all_events:
            written = supabase.upsert_events(all_events)
            click.echo(f"Upserted {written} event(s) to Supabase")
        supabase.upsert_seeds(seeds)
        click.echo(f"Upserted {len(seeds)} seed(s) to Supabase")
