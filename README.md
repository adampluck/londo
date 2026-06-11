# Londo

Aggregates non-mainstream London events from multiple sources into one
Supabase database, with a static frontend to browse them all in one place.

## Sources

| Source | Method |
|---|---|
| [Dandelion](https://dandelion.events) | iCal feed (London, in-person) + per-event JSON-LD pages |
| [Luma](https://luma.com/london) | Discover API (images, geo, tickets) merged with the iCal feed (descriptions) |
| [Newspeak House](https://newspeak.house/#events) | iCal feed + homepage enrichment (descriptions, rooms, hosts, Luma cover images) |
| [Numinity](https://www.eventbrite.co.uk/o/numinity-33797188771) | Eventbrite organizer listing + destination API (series expanded into occurrences) |
| WhatsApp groups | `londo ingest-whatsapp export.txt` — extracts event links from a chat export; Luma/Eventbrite/Dandelion links fetch from their platforms, other links via schema.org JSON-LD (source `other`, only when date, time and location are present) |

Chat-ingested URLs are remembered in a `seeds` table and re-fetched by the
daily scrape until their events pass, so they stay as fresh as everything else.

Events are deduplicated across sources: a shared Luma registration link, or
matching normalised title + date, marks the lower-priority copy as
`duplicate_of` the canonical one (priority: Newspeak > Dandelion > Luma),
and any missing fields (image, description, price) are merged into the
canonical record.

## Setup

1. **Supabase**: create a free project at [supabase.com](https://supabase.com),
   open the SQL editor, and run `schema.sql`.
2. **Local**: `pip install -e .`, copy `.env.example` to `.env`, fill in
   `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` (Settings → API).
3. **Frontend**: put the project URL and **anon** key in `web/config.js`.
   Serve locally with `python3 -m http.server -d web 8080`.
4. **GitHub Actions**: add `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` as
   repository secrets. The scrape runs daily at 05:17 UTC
   (`.github/workflows/scrape.yml`); the frontend deploys to GitHub Pages on
   push (`.github/workflows/pages.yml` — enable Pages → Source: GitHub Actions
   in repo settings).

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
