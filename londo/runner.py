from __future__ import annotations

import logging
import os
import re

import click

from londo.dedupe import dedupe
from londo.models import Event
from londo.output import write_events
from londo.scrapers.consciouscafe import ConsciousCafeScraper
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
from londo.scrapers.whatsapp import WhatsAppScraper
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
    "consciouscafe": ConsciousCafeScraper,
    "seeds": SeedsScraper,  # chat-ingested URLs; needs Supabase credentials
    "submissions": SubmissionsScraper,  # community links; needs Supabase creds
    "whatsapp": WhatsAppScraper,  # carries forward chat-ingested flyers; Supabase only
}

SUPABASE_ONLY_SOURCES = ("seeds", "submissions", "whatsapp")

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
@click.argument("export_path", type=click.Path(exists=True))
@click.option(
    "--store",
    type=click.Choice(["json", "supabase", "both"]),
    default="supabase",
    help="Where to write the results.",
)
@click.option("--output-dir", "-o", default="data")
@click.option("--rate-limit", "-r", default=1.0, type=float)
@click.option(
    "--since-days",
    default=30,
    type=int,
    help="Only read posts sent in the last N days (photos older than the "
    "chat's disappearing-message window are gone anyway).",
)
@click.option("--verbose", "-v", is_flag=True)
def ingest_whatsapp(
    export_path: str,
    store: str,
    output_dir: str,
    rate_limit: float,
    since_days: int,
    verbose: bool,
) -> None:
    """Ingest events from a WhatsApp chat export (the .txt, or the folder
    holding it and its photos).

    Every post is tried by its links first: Luma/Eventbrite/Dandelion links
    are fetched from their platforms, anything else via schema.org metadata
    (source 'other'). A post whose links yield nothing but which carries a
    photo is read by Claude instead — flyer plus caption — and listed under
    source 'whatsapp' when it has a title, description, date and time, a
    London venue and the photo. A fetched page's own image always wins
    over the uploaded photo, which only fills in when the page has none.
    """
    from datetime import datetime, timedelta, timezone
    from pathlib import Path

    from londo.chat_events import build_event, extract_event, photo_digest
    from londo.links import LinkFetcher, classify_url
    from londo.whatsapp import group_posts, locate_export, parse_messages

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    load_dotenv()

    if store in ("supabase", "both") and not os.environ.get("SUPABASE_URL"):
        click.echo("SUPABASE_URL not set. Copy .env.example to .env and fill credentials.", err=True)
        raise SystemExit(1)

    chat_file, media_dir = locate_export(export_path)
    text = chat_file.read_text(encoding="utf-8", errors="replace")
    posts = group_posts(parse_messages(text))
    since = datetime.now() - timedelta(days=since_days)
    posts = [p for p in posts if p.sent_at >= since]
    click.echo(f"{len(posts)} posts in the last {since_days} days, {sum(1 for p in posts if p.photos)} with photos")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=1)
    fetcher = LinkFetcher(rate_limit=rate_limit)
    supabase = SupabaseStore() if store in ("supabase", "both") else None
    client = None  # Anthropic client, created on first flyer

    all_events: list[Event] = []
    seeds: list[dict] = []
    fetched_keys: set[tuple[str, str]] = set()  # every link tried
    listed_keys: set[tuple[str, str]] = set()  # links that produced events
    uploaded: dict[str, str] = {}  # photo filename -> hosted URL

    def hosted(photo: str) -> str | None:
        """Public URL for a flyer photo (uploaded once per run)."""
        if photo in uploaded:
            return uploaded[photo]
        path = media_dir / photo
        if not path.exists():
            return None
        if supabase is None:
            url = path.resolve().as_uri()  # json runs: point at the local file
        else:
            url = supabase.upload_image(
                path.read_bytes(), f"whatsapp/{photo_digest(path)}{path.suffix.lower()}"
            )
        uploaded[photo] = url
        return url

    for post in posts:
        links = [(u, classify_url(u)) for u in post.urls]
        links = [(u, c) for u, c in links if c is not None]
        if any(c in listed_keys for _, c in links):
            continue  # a repost of a link already listed this run

        link_events: list[Event] = []
        for url, (kind, key) in links:
            if (kind, key) in fetched_keys:
                continue
            fetched_keys.add((kind, key))
            events = [
                e for e in fetcher.fetch(url)
                if e.start_datetime is None or e.start_datetime >= cutoff
            ]
            if not events:
                continue
            listed_keys.add((kind, key))
            link_events.extend(events)
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

        if link_events:
            # the page's own image wins; the flyer only fills a gap
            for event in link_events:
                if not event.image_url and post.photos:
                    event.image_url = hosted(post.photos[0])
            all_events.extend(link_events)
            continue

        if not post.photos or not any((media_dir / p).exists() for p in post.photos):
            continue
        if client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                click.echo("ANTHROPIC_API_KEY not set - skipping flyer-only posts", err=True)
                break
            import anthropic

            client = anthropic.Anthropic()
        try:
            extracted = extract_event(client, post, media_dir)
        except Exception as exc:
            logging.getLogger(__name__).exception("Extraction failed for a post by %s", post.sender)
            click.echo(f"  FAILED extraction: {exc}", err=True)
            continue
        if extracted is None:
            continue
        event = build_event(extracted, post)
        if event is None or (event.start_datetime and event.start_datetime < cutoff):
            continue
        event.image_url = hosted(post.photos[0])
        if not event.image_url:
            continue
        all_events.append(event)
        click.echo(f"  [whatsapp] {event.title} ({event.start_datetime:%a %d %b %H:%M})")

    all_events = drop_unwanted(all_events)

    if not all_events:
        click.echo("No usable events found in this export.")
        return

    # Dedupe against what's already listed too, so a flyer for an event the
    # scrape has from its ticket page is marked a copy now rather than at
    # the next scrape. Those rows are context only — not written back.
    listed: list[Event] = []
    if supabase is not None:
        try:
            listed = supabase.fetch_upcoming_events()
        except Exception:
            logging.getLogger(__name__).exception("Could not fetch listed events")
    ours = {(e.source, e.source_id) for e in all_events}
    dedupe(all_events + [e for e in listed if (e.source, e.source_id) not in ours])
    n_dupes = sum(1 for e in all_events if e.duplicate_of)
    click.echo(f"Total: {len(all_events)} events ({len(seeds)} from links, {n_dupes} already listed)")

    from londo.enrich import enrich_events

    existing = {}
    if supabase is not None:
        try:
            existing = supabase.fetch_enrichment()
        except Exception:
            logging.getLogger(__name__).exception("Could not fetch enrichment")
    calls = enrich_events(all_events, existing=existing)
    click.echo(f"Enriched: {calls} new LLM classifications")

    if store in ("json", "both"):
        filepath = write_events(all_events, "whatsapp", output_dir)
        click.echo(f"Wrote JSON -> {filepath}")

    if supabase is not None:
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


HELP_TEXT = """\
Londo — everyday commands

  `londo` is ~/.local/bin/londo -> .venv/bin/londo in the project
  (pip install -e . in the venv after changing dependencies).
  Credentials come from .env in the current dir or the project root
  (SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, ANTHROPIC_API_KEY).

IMPORT WHATSAPP POSTS
  1. In WhatsApp: open the group > group name > Export Chat > Attach Media.
  2. Unzip the export into chat_export/ (gitignored; holds _chat.txt +
     the photos). Photos older than the disappearing-message window
     are gone, so export soon after the posts you want.
  3. londo ingest-whatsapp chat_export/
       --since-days 7      only posts from the last N days (default 30)
       --store json        dry run: writes data/whatsapp.json, no upload
       -v                  show every link fetched / flyer read
  Links (Luma, Eventbrite, Dandelion, JSON-LD pages) are fetched and
  seeded; flyer-only posts are read by Claude (needs ANTHROPIC_API_KEY)
  and their photo uploaded to Storage. Writes to Supabase by default.
  Pull a bad extraction from the site by ticking `hidden` on its row
  in the Supabase dashboard.

ADD A SINGLE EVENT LINK
  londo seed https://luma.com/xyz [more urls...]
       --seed-only         just remember the URL; fetch on the next scrape

SCRAPE ALL SOURCES (what the 6-hourly GitHub Action runs)
  londo scrape                    debug run -> data/all.json
  londo scrape --store supabase   the real thing
  londo scrape -s luma -v         one source, verbose
{sources}
  (Supabase-only: {supabase_only})

BUILD / PREVIEW THE SITES
  python3 scripts/build_site.py build                          londo
  python3 scripts/build_site.py --site psyconnect build-psyconnect
  open build-psyconnect/index.html   (or build/index.html) in a browser
  python3 -m http.server -d web 8080          serve the live SPA locally
  python3 scripts/preview.py [rows.json] [port]  SPA + mock Supabase

DEPLOY / CI
  gh workflow run scrape.yml     scrape + rebuild + deploy both sites now
  gh workflow run pages.yml      redeploy the frontends only
  gh run list --limit 5          see how the last runs went
  Pushing to main auto-deploys when web/, sites/ or build_site.py change.

ONE-OFFS
  python3 scripts/backfill_topics.py   re-enrich events missing topics

For a command's full options: londo <command> --help
"""


@cli.command("help")
def help_command() -> None:
    """Cheat-sheet of the everyday workflows (WhatsApp import, scrape, deploy)."""
    import textwrap

    sources = textwrap.fill(
        "Sources: " + ", ".join(SCRAPERS),
        width=76,
        initial_indent="  ",
        subsequent_indent="           ",
    )
    click.echo(
        HELP_TEXT.format(
            sources=sources,
            supabase_only=", ".join(SUPABASE_ONLY_SOURCES),
        )
    )
