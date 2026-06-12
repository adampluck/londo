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

  -- enrichment (LLM classification + deterministic area pass)
  category      text,            -- move | connect | expand | think | make
  traits        text[] not null default '{}',
  hook          text,            -- one-line editorial hook
  quality_score smallint,        -- 0-100
  area          text,            -- central | east | north | south | west
  enriched_at   timestamptz,

  first_seen_at timestamptz not null default now(),
  last_seen_at  timestamptz not null default now(),

  primary key (source, source_id)
);

create index if not exists events_start_at_idx on public.events (start_at);
create index if not exists events_dedupe_key_idx on public.events (dedupe_key);
create index if not exists events_last_seen_idx on public.events (last_seen_at);
create index if not exists events_category_idx on public.events (category);
create index if not exists events_area_idx on public.events (area);

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

-- Public "know an event? paste a link" submissions. Anon may only insert
-- pending rows; nobody but the service role can read them. The scrape
-- pipeline validates each URL and promotes good ones to the seeds table.
create table if not exists public.submissions (
  id          uuid primary key default gen_random_uuid(),
  url         text not null,
  note        text,
  status      text not null default 'pending',  -- pending | accepted | rejected
  reason      text,
  created_at  timestamptz not null default now(),
  reviewed_at timestamptz
);

alter table public.submissions enable row level security;

drop policy if exists "anon submit" on public.submissions;
create policy "anon submit" on public.submissions
  for insert to anon, authenticated
  with check (
    status = 'pending'
    and url ~* '^https?://'
    and char_length(url) <= 500
    and (note is null or char_length(note) <= 500)
  );
