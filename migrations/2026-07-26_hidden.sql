-- Manual moderation flag. This column was added by hand in the production
-- dashboard (commit bc9d859 started filtering on it) but never captured in
-- schema.sql/migrations — a fresh database would 400 on every read, since
-- both the SPA and build_site.py query hidden=is.false. Idempotent, so it
-- is safe to run against production too.
alter table public.events
  add column if not exists hidden boolean not null default false;
