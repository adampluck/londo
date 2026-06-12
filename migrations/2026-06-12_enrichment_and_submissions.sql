-- Migration: LLM enrichment columns + public event submissions.
-- Paste into the Supabase SQL editor (Dashboard -> SQL) and run once.

-- Enrichment fields written by the scrape pipeline.
alter table public.events add column if not exists category      text;
alter table public.events add column if not exists traits        text[] not null default '{}';
alter table public.events add column if not exists hook          text;
alter table public.events add column if not exists quality_score smallint;
alter table public.events add column if not exists area          text;
alter table public.events add column if not exists enriched_at   timestamptz;

create index if not exists events_category_idx on public.events (category);
create index if not exists events_area_idx on public.events (area);

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
