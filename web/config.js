// Supabase project credentials (the anon key is safe to publish:
// the database is read-only for anonymous users via RLS).
// `self` rather than `window`: sw.js also imports this file, and workers
// have no `window`.
self.LONDO_CONFIG = {
  SUPABASE_URL: "https://nhjovwymgfsukpdgvajd.supabase.co",
  SUPABASE_ANON_KEY:
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5oam92d3ltZ2ZzdWtwZGd2YWpkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODExNjg0NTAsImV4cCI6MjA5Njc0NDQ1MH0.ZcZvGZs0Uhqw4-ZR-s97Y7ktC5YZw-V6XkVzZ43XFFc",
  // GoatCounter (open source, cookieless) — register the "londo" code at
  // goatcounter.com; leave empty ("") to disable analytics entirely.
  GOATCOUNTER: "https://londo.goatcounter.com/count",
  // CARTO basemap key (map tiles). Public like the anon key above —
  // restrict it to the site's domains at https://carto.com/basemaps/apikey/
  CARTO_API_KEY: "cb1_3kru_1_c22ef6590f5bf44aa5dd60d9",
};
