-- Migration: topics — LLM-assigned subject/scene labels (psychedelics,
-- consciousness, tech & ai, ...) for browsing by topic and the
-- tech / non-tech lens. Paste into the Supabase SQL editor and run once.

alter table public.events
  add column if not exists topics text[] not null default '{}';
