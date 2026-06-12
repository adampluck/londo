# Londo

The hub for in-person London gatherings that connect and inspire — ecstatic
dance to AI salons, breathwork to philosophy nights. Scrapes multiple sources
into one Supabase database, enriches every event with an LLM (intent
category, one-line hook, quality score), and serves a static frontend plus
per-event SEO pages.

## Sources

| Source | Method |
|---|---|
| [Dandelion](https://dandelion.events) | iCal feed (London, in-person) + per-event JSON-LD pages |
| [Luma](https://luma.com/london) | Discover API (images, geo, tickets) merged with the iCal feed (descriptions) |
| [Newspeak House](https://newspeak.house/#events) | iCal feed + homepage enrichment (descriptions, rooms, hosts, Luma cover images) |
| [Numinity](https://www.eventbrite.co.uk/o/numinity-33797188771) | Eventbrite organizer listing + destination API (series expanded into occurrences) |
| Eventbrite organizers | Same mechanism, aggregated as source `eventbrite` — configured in `EVENTBRITE_ORGANIZERS` (`londo/scrapers/eventbrite.py`); currently Robyn Wilford, The London School of Tantra, London Night Cafe, The School of Sufi Teaching, Ecstatic Dance London & URUBU |
| [PsyCalendar](https://www.psycalendar.com/other-psy-events) | Squarespace collection JSON; in-person London listings only, each resolved via its ticket link (Eventbrite/Dandelion/Luma/JSON-LD) and kept only with full details (date+time, location, description, image, cost). Emitted as source `other` ("elsewhere"); dedupe prefers native scrapers' copies |
| WhatsApp groups | `londo ingest-whatsapp export.txt` — extracts event links from a chat export; Luma/Eventbrite/Dandelion links fetch from their platforms, other links via schema.org JSON-LD (source `other`, only when date, time and location are present) |
| Visitor submissions | "Know a gathering we don't?" box on the site inserts into a `submissions` table (anon, insert-only RLS); the scrape validates each URL with the same completeness gate and promotes good ones to seeds |

Chat-ingested and submitted URLs are remembered in a `seeds` table and
re-fetched by the daily scrape until their events pass, so they stay as
fresh as everything else.

Events are deduplicated across sources: a shared Luma registration link, or
matching normalised title + date, marks the lower-priority copy as
`duplicate_of` the canonical one (priority: Newspeak > Dandelion > Luma),
and any missing fields (image, description, price) are merged into the
canonical record.

## Enrichment

After dedupe, each new canonical event gets one Claude Haiku call
(`londo/enrich.py`) assigning:

- **category** — the event's *form*: `move` (dance, movement), `connect`
  (relating, socials), `expand` (breathwork, psychedelics, ceremony),
  `think` (AI, talks, salons), `make` (workshops)
- **topics** — 1-3 subject/scene labels from a fixed vocabulary
  (psychedelics, consciousness, connection & intimacy, tech & ai,
  startups & work, …): what the event is *about*. Powers the topic chips
  and the one-tap tech / non-tech lens
- **traits** — fixed vocabulary (beginner-friendly, sober, outdoors, …)
- **hook** — a one-line editorial sell shown on cards and shared pages
- **quality_score** — 0-100 listing completeness; ≥75 gets a "✦ pick" mark

Already-enriched events are reused from the database, so the nightly cost is
a handful of calls. A deterministic pass (`londo/geo.py`) maps postcodes and
lat/lng to a London **area** (central/east/north/south/west); the LLM fills
in the area when the address has neither.

## Frontend

Three views: **browse** (category pills, topic chips, tech/non-tech lens,
area chips, a Mon–Sun week strip, search), **tonight** (what's still to
come today and a "surprise me" dice roll), and **map** (Leaflet,
category-coloured markers, obeying the same filters). The page palette
shifts with the time of day. Source is a small badge; the way in is
intent and subject, not plumbing.

## Static pages & SEO

The site is client-rendered, so `scripts/build_site.py` (stdlib only) also
emits a static page per event (`/e/<source>-<id>.html`), per category
(`/c/<category>.html`) and per topic (`/t/<topic>.html`) with OG tags,
Twitter cards, schema.org JSON-LD and a sitemap — WhatsApp unfurls and
Google both see real content. Both workflows
deploy this build; the scrape regenerates it every 6 hours.

## Setup

1. **Supabase**: create a free project at [supabase.com](https://supabase.com),
   open the SQL editor, and run `schema.sql` (existing projects: run the
   files in `migrations/` instead).
2. **Local**: `pip install -e .`, copy `.env.example` to `.env`, fill in
   `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (Settings → API) and
   `ANTHROPIC_API_KEY` (for enrichment; skipped gracefully without it).
3. **Frontend**: put the project URL and **anon** key in `web/config.js`.
   Serve locally with `python3 -m http.server -d web 8080`.
4. **GitHub Actions**: add `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` and
   `ANTHROPIC_API_KEY` as repository secrets. The scrape runs every 6 hours
   (`.github/workflows/scrape.yml`) and redeploys the site after each run;
   frontend pushes deploy via `.github/workflows/pages.yml` (enable Pages →
   Source: GitHub Actions in repo settings).

## Usage

```sh
londo scrape                      # all sources -> data/*.json (debug)
londo scrape --store supabase     # all sources -> Supabase
londo scrape -s luma -v           # one source, verbose
```

## How freshness works

Every upsert stamps `last_seen_at`. The frontend only shows events seen in
the last 3 days, so events removed from a source disappear automatically
without hard deletes.

## Not yet implemented

- WhatsApp group scraping (deliberately deferred — needs a different
  approach, likely WhatsApp Web automation or manual export parsing).
