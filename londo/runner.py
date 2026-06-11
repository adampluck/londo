from __future__ import annotations

import logging

import click

from londo.dedupe import dedupe
from londo.models import Event
from londo.output import write_events
from londo.scrapers.dandelion import DandelionScraper
from londo.scrapers.luma import LumaScraper
from londo.scrapers.newspeak import NewspeakScraper
from londo.storage import SupabaseStore, load_dotenv

SCRAPERS = {
    "dandelion": DandelionScraper,
    "luma": LumaScraper,
    "newspeak": NewspeakScraper,
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
