from __future__ import annotations

import logging
import os

import click

from londo.dedupe import dedupe
from londo.models import Event
from londo.output import write_events
from londo.scrapers.dandelion import DandelionScraper
from londo.scrapers.eventbrite import NuminityScraper
from londo.scrapers.luma import LumaScraper
from londo.scrapers.newspeak import NewspeakScraper
from londo.scrapers.seeds import SeedsScraper
from londo.storage import SupabaseStore, load_dotenv

SCRAPERS = {
    "dandelion": DandelionScraper,
    "luma": LumaScraper,
    "newspeak": NewspeakScraper,
    "numinity": NuminityScraper,
    "seeds": SeedsScraper,  # chat-ingested URLs; needs Supabase credentials
}


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
        if src == "seeds" and not os.environ.get("SUPABASE_URL"):
            click.echo("Skipping seeds (no Supabase credentials)")
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

    if not all_events:
        click.echo("No events scraped.")
        raise SystemExit(1)

    dedupe(all_events)
    n_dupes = sum(1 for e in all_events if e.duplicate_of)
    click.echo(f"Total: {len(all_events)} events ({n_dupes} cross-source duplicates)")

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
