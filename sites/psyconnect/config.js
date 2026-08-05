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
  // "channel": the footer's "follow us" link (SPA + static pages). Paste
  // the WhatsApp channel invite URL to switch it on; empty = hidden.
  // "joinCommunity": the unlisted /join-community/ page. Both the Worker URL
  // (workers/join-community) and the Turnstile *site* key must be set or the
  // page isn't built. The invite link itself is a Worker secret, never here.
  // The passphrase layer is off: passphraseHint is empty, so no field is
  // rendered, and the Worker has no JOIN_PASSPHRASE secret, so it doesn't
  // check one. To turn it back on, do both — `wrangler secret put
  // JOIN_PASSPHRASE` first, then set the hint here (say where to find the
  // word, never the word itself: this text is public). Secret first, or the
  // live page asks for nothing while the Worker still demands it.
  SITE: /*SITE-JSON*/ {
    "id": "psyconnect",
    "name": "psyconnect",
    "displayName": "PsyConnect",
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
        "pitch night", "pitch competition", "y combinator",
        "alo regent", "alo runners",
        "watkins bookshop",
        "wonder workshop - for little ones",
        "lfg summer party",
        "live music:",
        "run club", "running club", "parkrun", "5km", "10km", "bootcamp", "hyrox",
        "crossfit", "spin class", "pilates", "reformer", "kettlebell",
        "personal training", "strength training", "fitness class",
        "red light therapy", "high performers", "performance studio",
        "biohacking", "bon charge", "ydun",
        "aperitivo", "bottomless brunch", "coworking", "investor"
      ],
      "excludeText": ["shibari"]
    },
    "topics": [
      "psychedelics", "consciousness", "spirituality & ritual",
      "connection & intimacy", "healing & wellbeing"
    ],
    "features": {
      "lens": false,
      "categoryPills": false,
      "views": false,
      "map": false,
      "compass": false,
      "topics": true,
      "animateDayChange": false
    },
    "mapTiles": "light_all",
    "featured": {
      "organizers": ["PsyConnect London", "PsyConnect"],
      "label": "Our next event",
      "accent": "next event"
    },
    "curated": {
      "organizers": [
        "The Psychedelic Society", "Numinity", "Adventures in Awareness",
        "Unseen", "Unseen London", "Creating Meaning"
      ],
      "titleMatches": ["bohm"],
      "exclude": ["running club"],
      "maxTotal": 4,
      "maxMobile": 5,
      "windowStages": [7, 14, 30],
      "label": "Our upcoming top picks",
      "accent": "top picks"
    },
    "channel": {
      "url": "",
      "label": "follow psyconnect on WhatsApp"
    },
    "joinCommunity": {
      "workerUrl": "https://psyconnect-join.plucky101.workers.dev",
      "turnstileSiteKey": "0x4AAAAAAEG-S32kYG1X6qMl",
      "passphraseHint": ""
    },
    "logo": "logo.png",
    "shellExtras": ["theme.css", "bg.jpg", "logo.png"]
  } /*END-SITE-JSON*/,
};
