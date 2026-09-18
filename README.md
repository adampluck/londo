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
| Eventbrite organizers | Same mechanism, aggregated as source `eventbrite` — configured in `EVENTBRITE_ORGANIZERS` (`londo/scrapers/eventbrite.py`); currently Robyn Wilford, The London School of Tantra, London Night Cafe, The School of Sufi Teaching, Ecstatic Dance London & URUBU, The Royal Institution, Seed Talks, London Psychedelic Community, The Maudsley Psychedelic Society, YOUnited Breath Space, Moon Haven & Gaia Wellbeing Collective |
| Ticket Tailor box offices | Box office listing page (one card per date with times and venue) plus each event's page for description, cover image and address, aggregated as source `tickettailor` — configured in `TICKET_TAILOR_BOX_OFFICES` (`londo/scrapers/tickettailor.py`). Ticket Tailor sits behind a Cloudflare bot check keyed on the client's TLS fingerprint, so pages are fetched with `curl_cffi` impersonating a browser, rotating hosts (`tickettailor.com` / `buytickets.at`) and browser profiles until one passes. Ticket Tailor links shared in chats are fetched the same way (recurring events expanded from their select-date page) and dedupe onto any organiser-site copy by Ticket Tailor event id + date |
| Meetup groups | Public iCal feed per group, listed in `MEETUP_GROUPS` (`londo/scrapers/meetup.py`); the feeds no longer carry a venue, so each event page's schema.org blob supplies the address and cover image, and drops the online (`VirtualLocation`) and out-of-town listings these groups occasionally post |
| [ConsciousCafe](https://consciouscafe.org/events/category/group-events/) | The Events Calendar REST API, `group-events` category (the in-person track; `online-events` is left alone). A national network, so London is required positively — a London venue record, a London "Venue:" line in the description, or the listing naming London for the roving lunches and dinners |
| [SoulLinkHub](https://soullinkhub.com) | A client-rendered Lovable app with no feed, so the listings are read the way the site's own browser code reads them: its public Supabase anon key against the `events` table (`status=published`, not yet ended) plus `event_fees` for sliding-scale prices. UK-wide, so London is required positively — the address reads as London or the coordinates fall inside Greater London — and 'Online' rows are dropped. The site's own event page is the way in; a listing that links to Ticket Tailor takes that event id + date as its `external_ref` so it dedupes onto the Ticket Tailor copy, and Dandelion/Eventbrite copies collapse by title + day (their links are per-series, not per-date, so they can't serve as refs) |
| [PsyCalendar](https://www.psycalendar.com/other-psy-events) | Squarespace collection JSON; in-person London listings only, each resolved via its ticket link (Eventbrite/Dandelion/Luma/JSON-LD) and kept only with full details (date+time, location, description, image, cost). Emitted as source `other` ("elsewhere"); dedupe prefers native scrapers' copies |
| WhatsApp groups | `londo ingest-whatsapp chat_export/` — reads a chat export (the folder with `_chat.txt` and its photos). Each post is tried by its links first: Luma/Eventbrite/Dandelion/Ticket Tailor links fetch from their platforms, other links via schema.org JSON-LD (source `other`). A post whose links yield nothing but which carries a photo is read by Claude (flyer + caption, `londo/chat_events.py`) and listed as source `whatsapp` when it has a title, description, date *and* time, a London venue and the photo — which is uploaded to the public `event-images` Storage bucket (created on first use). A fetched page's own image always beats the uploaded photo. Chat listings have no page to re-fetch, so the daily scrape carries them forward from the database (`londo/scrapers/whatsapp.py`) until they've passed; a ticket-page copy of the same event that turns up later becomes canonical |
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
londo help                        # cheat-sheet: WhatsApp import, seeds, deploy
londo scrape                      # all sources -> data/*.json (debug)
londo scrape --store supabase     # all sources -> Supabase
londo scrape -s luma -v           # one source, verbose
```

## How freshness works

Every upsert stamps `last_seen_at`. The frontend only shows events seen in
the last 3 days, so events removed from a source disappear automatically
without hard deletes.

## Chat-shared listings

Flyers posted to the group without a ticket page are listed with an
empty `source_url`: cards link to the event's own static page (`/e/<slug>/`,
reserved for them by `scripts/build_site.py`) and that page says the
details are as posted. Sender handles are never used as the organiser —
only a brand named on the flyer. A bad extraction is pulled from both
sites by setting `hidden` on the row in the Supabase dashboard.

Keep `chat_export/` out of git (it is ignored): it is a private chat with
phone numbers and photos of real people.
