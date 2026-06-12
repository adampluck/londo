-- Migration: one row per submitted URL — blunts repeat-flooding and makes
-- duplicate submissions a clean 409 instead of a new row. Old resolved
-- rows are purged after 30 days by the scrape, freeing URLs again.
-- Paste into the Supabase SQL editor and run once.

create unique index if not exists submissions_url_key
  on public.submissions (url);
