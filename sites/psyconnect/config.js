// psyconnect site config — overlays web/config.js at build time.
// Same Supabase project as londo (anon key is safe to publish: the
// database is read-only for anonymous users via RLS).
// `self` rather than `window`: sw.js also imports this file, and workers
// have no `window`.
self.LONDO_CONFIG = {
  SUPABASE_URL: "https://nhjovwymgfsukpdgvajd.supabase.co",
  SUPABASE_ANON_KEY:
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im5oam92d3ltZ2ZzdWtwZGd2YWpkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODExNjg0NTAsImV4cCI6MjA5Njc0NDQ1MH0.ZcZvGZs0Uhqw4-ZR-s97Y7ktC5YZw-V6XkVzZ43XFFc",
  // GoatCounter (open source, cookieless) — psyconnect code at goatcounter.com
  GOATCOUNTER: "https://psyconnect.goatcounter.com/count",
  // Everything between the SITE-JSON markers must stay strict JSON:
  // scripts/build_site.py extracts and json.loads it so the SPA and the
  // static pages share one filter definition.
  SITE: /*SITE-JSON*/ {
    "id": "psyconnect",
    "name": "psyconnect",
    "tagline": "consciousness, connection, ceremony & psychedelics — in person in london",
    "filter": {
      "categories": ["expand"],
      "topics": [
        "psychedelics", "consciousness", "spirituality & ritual",
        "connection & intimacy", "healing & wellbeing"
      ],
      "exclude": [
        "social sports mix", "football", "futsal", "5-a-side",
        "dodgeball", "rounders", "netball", "basketball", "volleyball",
        "rugby", "cricket", "softball", "badminton", "kickabout",
        "startup", "founder",
        "hackathon", "openai", "emed", "ai engine", "demo day",
        "pitch night", "pitch competition", "y combinator"
      ]
    },
    "topics": [
      "psychedelics", "consciousness", "spirituality & ritual",
      "connection & intimacy", "healing & wellbeing"
    ],
    "features": {
      "lens": false,
      "categoryPills": false,
      "views": true,
      "map": false,
      "compass": false,
      "topics": true
    },
    "mapTiles": "light_all",
    "featured": {
      "organizers": ["PsyConnect London", "PsyConnect"],
      "label": "our next event"
    },
    "logo": "logo.png",
    "shellExtras": ["theme.css", "bg.jpg", "logo.png"]
  } /*END-SITE-JSON*/,
};
