-- Londo events table. Run this once in the Supabase SQL editor.

create table if not exists public.events (
  source        text not null,
  source_id     text not null,
  source_url    text not null,
  external_ref  text,
  dedupe_key    text,
  duplicate_of  text,

  title         text not null,
  description   text,

  start_at      timestamptz,
  end_at        timestamptz,
  is_all_day    boolean not null default false,

  venue_name    text,
  address       text,
  city          text,
  latitude      double precision,
  longitude     double precision,
  is_online     boolean not null default false,

  image_url     text,
  tags          text[] not null default '{}',

  price_min     numeric,
  price_max     numeric,
  is_free       boolean not null default false,

  organizer_name text,
  organizer_url  text,

  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),

  primary key (source, source_id)
);

create index if not exists events_start_at_idx on public.events (start_at);
create index if not exists events_dedupe_key_idx on public.events (dedupe_key);
create index if not exists events_last_seen_idx on public.events (last_seen_at);

-- Anyone may read; only the service role (which bypasses RLS) may write.
alter table public.events enable row level security;

drop policy if exists "public read" on public.events;
create policy "public read" on public.events
  for select to anon, authenticated using (true);

-- Seed URLs ingested from chats (WhatsApp etc.). The daily scrape
-- re-fetches active seeds so their events stay fresh, and deactivates
-- them once the event has passed.
create table if not exists public.seeds (
  url             text primary key,
  kind            text,
  added_by        text not null default 'whatsapp',
  added_at        timestamptz not null default now(),
  last_fetched_at timestamptz,
  event_start_at  timestamptz,
  active          boolean not null default true
);

-- service-role access only: RLS on, no anon policies
alter table public.seeds enable row level security;
