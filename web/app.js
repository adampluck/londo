(function () {
  "use strict";

  const { SUPABASE_URL, SUPABASE_ANON_KEY } = window.LONDO_CONFIG;
  // Per-site block (sites/<name>/config.js): dataset filter, topic nav,
  // feature flags. Absent on londo — every fallback below is londo's
  // current behavior.
  const SITE = window.LONDO_CONFIG.SITE || {};
  const FEATURES = SITE.features || {};

  // --- PWA install / launch helpers -------------------------------------
  const UA = navigator.userAgent || "";
  function isStandalone() {
    return (
      window.matchMedia("(display-mode: standalone)").matches ||
      window.navigator.standalone === true // iOS Safari home-screen apps
    );
  }
  function isIOS() {
    return (
      /iphone|ipad|ipod/i.test(UA) ||
      // iPadOS 13+ masquerades as desktop Safari; touch points give it away
      (/Macintosh/.test(UA) && navigator.maxTouchPoints > 1)
    );
  }
  // iOS install must go through Safari's Share sheet; other iOS browsers and
  // in-app webviews (FB/IG/etc.) can't add to the home screen, so don't nudge.
  function isIOSSafari() {
    return (
      isIOS() &&
      !/CriOS|FxiOS|EdgiOS|GSA|FBAN|FBAV|Instagram|Line\//.test(UA)
    );
  }
  function prefersReducedMotion() {
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }
  // londo and psyconnect share the github.io origin, so localStorage keys
  // must be site-prefixed (same reasoning as the SW cache name in sw.js).
  function storeKey(suffix) {
    return `${SITE.id || "londo"}:${suffix}`;
  }

  // Tag outbound event links with UTM params so organisers can see
  // psyconnect referral traffic in their own analytics — londo has no
  // SITE.id and stays unchanged.
  function withUtm(url) {
    if (SITE.id !== "psyconnect" || !url) return url;
    try {
      const u = new URL(url);
      u.searchParams.set("utm_source", "psyconnect.london");
      u.searchParams.set("utm_medium", "referral");
      return u.toString();
    } catch {
      return url;
    }
  }

  const state = {
    events: [],
    view: "browse", // browse | tonight | map
    category: "all",
    topics: new Set(), // multi-select; empty = anything
    lens: "all", // all | tech | beyond
    area: "all",
    day: "7", // "7", "30", or a London YYYY-MM-DD
    freeOnly: false,
    query: "",
    surprise: null, // event shown by "surprise me"
    // Header-link landing: { kind: "topic"|"category", key } — intro + filter.
    // Not set when using chips/pills so landings stay a special entry path.
    landing: null,
    map: null,
    mapLoaded: false,
  };

  // Intent categories — the primary way in. Colors tint badges, pills
  // and placeholder gradients.
  const CATEGORIES = {
    move:    { label: "move",    color: "#e8836f", colorB: "#d96a9e" },
    connect: { label: "connect", color: "#e3c08d", colorB: "#e8836f" },
    expand:  { label: "expand",  color: "#9d7fd1", colorB: "#6f5bb5" },
    think:   { label: "think",   color: "#5fb5a2", colorB: "#3d8fa8" },
    make:    { label: "make",    color: "#d96a9e", colorB: "#9d7fd1" },
  };

  // Subject/scene labels assigned by enrichment (londo/enrich.py
  // TOPIC_VOCAB) — what an event is about, independent of its form.
  const TOPICS = SITE.topics || [
    "psychedelics", "consciousness", "connection & intimacy", "tech & ai",
    "startups & work", "arts & creativity", "music & sound",
    "nature & outdoors", "healing & wellbeing", "spirituality & ritual",
    "society & politics", "science & ideas",
  ];
  // static /t/<slug>/ pages + ?topic=<slug> landings — keep in step with
  // scripts/build_site.py TOPICS
  const TOPIC_SLUGS = {
    "psychedelics": "psychedelics",
    "consciousness": "consciousness",
    "connection & intimacy": "connection",
    "tech & ai": "tech-ai",
    "startups & work": "startups",
    "arts & creativity": "arts",
    "music & sound": "music",
    "nature & outdoors": "nature",
    "healing & wellbeing": "healing",
    "spirituality & ritual": "spirituality",
    "society & politics": "society",
    "science & ideas": "ideas",
  };
  // the tech / non-tech lens: which side of the tech divide to see
  const TECH_TOPICS = ["tech & ai", "startups & work"];

  // Warm intros for header-link landings (?topic= / ?category=).
  // Mirror tone of scripts/build_site.py CATEGORY_INTROS / TOPIC_INTROS.
  const LANDING_INTROS = {
    category: {
      move: {
        title: "move — body first",
        paras: [
          "Bodies first. These are the nights and mornings when London moves — ecstatic dance floors, 5Rhythms waves, yoga that feels like play, contact improv and everything in between.",
          "No performance required. Show up as you are, follow what feels good, and leave a little more awake than you arrived.",
        ],
      },
      connect: {
        title: "connect — real conversation",
        paras: [
          "For the people who miss real conversation. Circles, authentic relating, shared tables and soft socials where the point is each other — not networking, not small talk that goes nowhere.",
          "Come curious. Leave with a face you recognise next time.",
        ],
      },
      expand: {
        title: "expand — breath, ceremony, stillness",
        paras: [
          "Quiet rooms, deep breath, altered edges. Breathwork, meditation, sound baths, ceremony and the soft practices that open something wider than the usual week.",
          "In person, in London — chosen for presence, not spectacle.",
        ],
      },
      think: {
        title: "think — ideas in the room",
        paras: [
          "Salons, talks and long-form evenings for people who like their ideas with other humans in the room. Philosophy, AI, science, civic chat — without the webinar energy.",
          "Bring a question. Stay for the conversation after.",
        ],
      },
      make: {
        title: "make — hands busy",
        paras: [
          "Hands busy, mind quieter. Workshops, craft, song and making things together — the kind of evening where you leave with something you built, not just a ticket stub.",
          "No portfolio needed. Just show up ready to try.",
        ],
      },
    },
    topic: {
      psychedelics: {
        title: "psychedelics in london",
        paras: [
          "Talks, integration circles, community nights and careful conversations about plant medicine and psychedelic culture — education and connection, in person.",
          "A gentle way in if you're curious, and a place to land if you've already been out there.",
        ],
      },
      consciousness: {
        title: "consciousness",
        paras: [
          "Explorations of mind, awareness and the odd miracle of being awake. From contemplative evenings to lively salons — always with other people in the room.",
        ],
      },
      "connection & intimacy": {
        title: "connection & intimacy",
        paras: [
          "Spaces for relating with a bit more honesty. Circles, workshops and gatherings about friendship, intimacy and the courage to be seen.",
          "Come as you are. Leave a little less alone in the city.",
        ],
      },
      "tech & ai": {
        title: "tech & ai",
        paras: [
          "Builders, thinkers and the quietly obsessed — in-person nights about AI, tools and the future, without another Zoom grid.",
        ],
      },
      "startups & work": {
        title: "startups & work",
        paras: [
          "Founders, side projects and the people building things in London. Meetups and evenings that feel human, not like a pitch deck.",
        ],
      },
      "arts & creativity": {
        title: "arts & creativity",
        paras: [
          "Making, looking, listening. Creative gatherings for anyone who wants art in their week, not only on a gallery wall.",
        ],
      },
      "music & sound": {
        title: "music & sound",
        paras: [
          "Sound baths, live rooms, shared listening and the evenings where music is the medicine. Ears open, phones down if you can.",
        ],
      },
      "nature & outdoors": {
        title: "nature & outdoors",
        paras: [
          "Parks, walks and outdoor rituals — London still has green edges if you know where to look. Come for the sky and the company.",
        ],
      },
      "healing & wellbeing": {
        title: "healing & wellbeing",
        paras: [
          "Gentle practices for nervous systems that live in a loud city. Bodywork, breath, rest and care — in person, at a human pace.",
        ],
      },
      "spirituality & ritual": {
        title: "ceremony & spirituality",
        paras: [
          "Ceremony, ritual and the sacred ordinary. Cacao, prayer, seasonal gatherings and rooms held with intention.",
          "You don't need a fixed belief — only a little openness.",
        ],
      },
      "society & politics": {
        title: "society & politics",
        paras: [
          "Civic conversation without the shouty timeline. Evenings about how we live together, face to face.",
        ],
      },
      "science & ideas": {
        title: "science & ideas",
        paras: [
          "Curiosity as a social sport. Talks and salons where science and big ideas get a pint and a good audience.",
        ],
      },
    },
  };

  // Deterministic placeholder gradient for events without an image.
  const GRADIENTS = [
    ["#4f46e5", "#9333ea"], ["#0891b2", "#2563eb"], ["#059669", "#0d9488"],
    ["#d97706", "#dc2626"], ["#db2777", "#9333ea"], ["#475569", "#1e293b"],
  ];

  const PICK_THRESHOLD = 75; // quality_score at or above ⇒ "✦ pick"
  // everything works within a 30-day window: the fetch, the date strip,
  // and the widest day-range tick
  const HORIZON_DAYS = 30;

  // ---------- data ----------

  async function fetchEvents() {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    // keep events visible for sources whose scrape succeeded recently;
    // drop rows not re-confirmed within 3 days (likely cancelled/removed)
    const staleCutoff = new Date(Date.now() - 3 * 864e5).toISOString();

    const params = new URLSearchParams({
      select: "*",
      order: "start_at.asc",
      limit: "1000",
    });
    const horizon = new Date(today.getTime() + HORIZON_DAYS * 864e5);
    const url =
      `${SUPABASE_URL}/rest/v1/events?${params}` +
      `&start_at=gte.${today.toISOString()}` +
      `&start_at=lte.${horizon.toISOString()}` +
      `&duplicate_of=is.null` +
      `&is_online=eq.false` + // in-person only
      `&last_seen_at=gte.${staleCutoff}` +
      `&hidden=is.false`;

    const res = await fetch(url, {
      headers: {
        apikey: SUPABASE_ANON_KEY,
        Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
      },
    });
    if (!res.ok) throw new Error(`Supabase request failed (${res.status})`);
    return res.json();
  }

  // ---------- time helpers ----------

  function londonDate(d) {
    // YYYY-MM-DD in Europe/London
    return new Date(d).toLocaleDateString("en-CA", { timeZone: "Europe/London" });
  }

  function londonHour(d) {
    return Number(
      new Date(d).toLocaleTimeString("en-GB", {
        hour: "numeric",
        hour12: false,
        timeZone: "Europe/London",
      })
    );
  }

  // ---------- filtering ----------

  function isTech(e) {
    return (e.topics || []).some((t) => TECH_TOPICS.includes(t));
  }

  // All-day events stay visible all day (no single "start" to have
  // passed); everything else disappears once its start time is behind us.
  function hasStarted(e) {
    if (e.is_all_day) return false;
    return new Date(e.start_at).getTime() < Date.now();
  }

  // Our own events (SITE.featured.organizers) are always in, whatever
  // the filter or enrichment says — it's our site.
  function isOurs(e) {
    const org = (e.organizer_name || "").toLowerCase();
    return ((SITE.featured || {}).organizers || []).some(
      (o) => org === o.toLowerCase()
    );
  }

  // Hand-picked trusted third-party organisers/series (SITE.curated) —
  // like isOurs(), these bypass SITE.filter entirely: they're chosen by
  // source, not by topic/category heuristics.
  function isCurated(e) {
    if (!SITE.curated) return false;
    const org = (e.organizer_name || "").toLowerCase();
    const title = (e.title || "").toLowerCase();
    if (
      (SITE.curated.organizers || []).some((o) => org === o.toLowerCase())
    ) {
      return true;
    }
    return (SITE.curated.titleMatches || []).some((t) =>
      title.includes(t.toLowerCase())
    );
  }

  // A filtered site (SITE.filter) only ever sees its slice of the table:
  // category in the list, or any topic overlapping — minus anything whose
  // title/organizer/hook/description hits an exclude term (sports mis-tags,
  // tech hackathons that pick up "healing & wellbeing", etc.).
  // Tech/startup topics alone never admit an event unless a stronger scene
  // topic is also present (or category is expand).
  function siteMatch(e) {
    if (isOurs(e)) return true;
    if (isCurated(e)) return true;
    if (!SITE.filter) return true;
    const hay = [
      e.title,
      e.organizer_name,
      e.hook,
      e.description,
      (e.tags || []).join(" "),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if ((SITE.filter.exclude || []).some((term) => hay.includes(term)))
      return false;

    const topics = e.topics || [];
    const techish = ["tech & ai", "startups & work"];
    const strongScene = [
      "psychedelics",
      "consciousness",
      "spirituality & ritual",
      "connection & intimacy",
    ];
    // "healing & wellbeing" + tech is how health hackathons leak in
    if (
      topics.some((t) => techish.includes(t)) &&
      !topics.some((t) => strongScene.includes(t)) &&
      e.category !== "expand"
    ) {
      return false;
    }

    if ((SITE.filter.categories || []).includes(e.category)) return true;
    return topics.some((t) => (SITE.filter.topics || []).includes(t));
  }

  function baseFilter(e) {
    if (state.category !== "all" && e.category !== state.category) return false;
    if (
      state.topics.size &&
      !(e.topics || []).some((t) => state.topics.has(t))
    )
      return false;
    if (state.lens === "tech" && !isTech(e)) return false;
    if (state.lens === "beyond" && isTech(e)) return false;
    if (state.area !== "all" && e.area !== state.area) return false;
    if (state.freeOnly && !e.is_free) return false;
    const q = state.query.trim().toLowerCase();
    if (q && !matchesQuery(e, q)) return false;
    return true;
  }

  function browseEvents() {
    // when searching, scan all loaded events (up to 30 days) — day filter hides too much
    if (state.query.trim()) {
      return state.events.filter((e) => baseFilter(e) && !hasStarted(e));
    }
    const until =
      state.day === "7" || state.day === "30"
        ? Date.now() + Number(state.day) * 864e5
        : null;
    return state.events.filter((e) => {
      if (!baseFilter(e) || hasStarted(e)) return false;
      if (until) return new Date(e.start_at).getTime() <= until;
      return londonDate(e.start_at) === state.day;
    });
  }

  function tonightEvents() {
    const now = Date.now();
    const today = londonDate(now);
    return state.events.filter((e) => {
      if (!baseFilter(e)) return false;
      if (londonDate(e.start_at) !== today) return false;
      if (e.is_all_day) return true;
      return new Date(e.start_at).getTime() >= now - 30 * 60000;
    });
  }

  function mapEvents() {
    // same filter set as browse (category, area, day, free, search) —
    // the map is just another way of looking at the current selection
    return browseEvents().filter(
      (e) => e.latitude != null && e.longitude != null
    );
  }

  function haystack(e) {
    return [
      e.title,
      e.description,
      e.hook,
      e.venue_name,
      e.organizer_name,
      (e.tags || []).join(" "),
      (e.topics || []).join(" "),
      (e.traits || []).join(" "),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function matchesQuery(e, q) {
    const text = haystack(e);
    // 1-2 char queries match whole words only ("ai" shouldn't match "air")
    if (q.length <= 2) {
      return new RegExp(`(^|[^a-z0-9])${escapeRe(q)}([^a-z0-9]|$)`, "i").test(text);
    }
    if (text.includes(q)) return true;
    // plural-insensitive: "psychedelics" should match "psychedelic"
    return q.endsWith("s") && text.includes(q.slice(0, -1));
  }

  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // ---------- controls ----------

  function renderWeekStrip() {
    const strip = document.getElementById("week-strip");
    const cells = [];
    const now = new Date();
    const fmt = (d, opts) =>
      d
        .toLocaleDateString("en-GB", { ...opts, timeZone: "Europe/London" })
        .toLowerCase();
    for (let i = 0; i < HORIZON_DAYS; i++) {
      const d = new Date(now.getTime() + i * 864e5);
      const key = londonDate(d);
      if (i === 0) cells.push(tick("today", fmt(d, { weekday: "short" }), key));
      else if (i === 1)
        cells.push(tick("tmrw", fmt(d, { weekday: "short" }), key));
      else
        cells.push(
          tick(fmt(d, { weekday: "short" }), fmt(d, { day: "numeric" }), key)
        );
    }
    strip.replaceChildren(...cells);
    syncDayTicks(); // include the pinned 7/30 ticks on first paint

    function tick(main, sub, key) {
      const btn = document.createElement("button");
      btn.className = "tick" + (key === state.day ? " cursor" : "");
      btn.dataset.day = key;
      btn.appendChild(document.createTextNode(main));
      const small = document.createElement("small");
      small.textContent = sub;
      btn.appendChild(small);
      btn.addEventListener("click", () => {
        state.day = key;
        state.surprise = null;
        syncDayTicks();
        render();
      });
      return btn;
    }
  }

  function syncDayTicks() {
    document
      .querySelectorAll(".tick")
      .forEach((t) => t.classList.toggle("cursor", t.dataset.day === state.day));
  }

  function maybeShowEnrichedControls() {
    // These instruments only make sense once events are classified.
    const withCategory = state.events.filter((e) => e.category).length;
    if (withCategory >= 5 && FEATURES.categoryPills !== false)
      document.getElementById("category-pills").hidden = false;
    const withArea = state.events.filter((e) => e.area).length;
    if (withArea >= 5 && FEATURES.compass !== false)
      document.getElementById("compass-unit").hidden = false;
    const withTopics = state.events.filter(
      (e) => (e.topics || []).length
    ).length;
    if (withTopics >= 5 && FEATURES.topics !== false) {
      renderTopicTokens();
      document.getElementById("topic-unit").hidden = false;
      if (FEATURES.lens !== false) {
        document.getElementById("lens-unit").hidden = false;
        setLens(state.lens);
      }
      startTapeDrift();
    }
  }

  function renderTopicTokens() {
    const tape = document.getElementById("topic-chips");
    const countFor = (topic) =>
      state.events.filter((e) => (e.topics || []).includes(topic)).length;

    // "anything" sits still beside the tape so it's always in reach
    document
      .getElementById("topic-anchor")
      .replaceChildren(token("anything", "all"));
    const tokens = [];
    for (const topic of TOPICS) {
      const n = countFor(topic);
      if (n < 2) continue; // don't offer near-empty doorways
      const btn = token(topic, topic);
      btn.title = `${n} events`;
      tokens.push(btn);
    }
    tape.replaceChildren(...tokens);
    syncTopicTokens();

    function token(label, key) {
      const btn = document.createElement("button");
      btn.className = "token";
      btn.dataset.topic = key;
      btn.textContent = label;
      btn.addEventListener("click", () => {
        clearLanding();
        if (key === "all") state.topics.clear();
        else if (state.topics.has(key)) state.topics.delete(key);
        else state.topics.add(key);
        state.surprise = null;
        syncTopicTokens();
        render();
      });
      return btn;
    }
  }

  function syncTopicTokens() {
    document.querySelectorAll("#topic-unit .token").forEach((t) => {
      const k = t.dataset.topic;
      t.classList.toggle(
        "lit",
        k === "all" ? state.topics.size === 0 : state.topics.has(k)
      );
    });
  }

  function setLens(lens) {
    state.lens = lens;
    document.querySelectorAll("#lens-toggle .lens-stop").forEach((b) => {
      const on = b.dataset.lens === lens;
      b.classList.toggle("lit", on);
      b.setAttribute("aria-checked", String(on));
    });
  }

  const AREA_LABELS = {
    all: "anywhere in london",
    north: "north london",
    east: "east london",
    south: "south london",
    west: "west london",
    central: "central london",
  };

  function syncCompass() {
    document
      .querySelectorAll("#compass .zone")
      .forEach((z) => z.classList.toggle("lit", z.dataset.area === state.area));
    document.getElementById("area-readout").textContent =
      AREA_LABELS[state.area] || AREA_LABELS.all;
  }

  // let mouse users drag (and flick) the horizontal tapes — touch
  // already scrolls natively
  function enableDragScroll(el) {
    let down = false, moved = false;
    let startX = 0, startLeft = 0, lastX = 0, lastT = 0, vel = 0, raf;
    el.addEventListener("pointerdown", (ev) => {
      if (ev.pointerType !== "mouse") return;
      down = true;
      moved = false;
      startX = lastX = ev.clientX;
      startLeft = el.scrollLeft;
      lastT = performance.now();
      vel = 0;
      cancelAnimationFrame(raf);
    });
    window.addEventListener("pointermove", (ev) => {
      if (!down) return;
      const dx = ev.clientX - startX;
      if (Math.abs(dx) > 4) moved = true;
      el.scrollLeft = startLeft - dx;
      const t = performance.now();
      vel = (ev.clientX - lastX) / Math.max(1, t - lastT);
      lastX = ev.clientX;
      lastT = t;
    });
    window.addEventListener("pointerup", () => {
      if (!down) return;
      down = false;
      let speed = -vel * 14; // carry the release velocity into a glide
      const glide = () => {
        if (Math.abs(speed) < 0.4) return;
        el.scrollLeft += speed;
        speed *= 0.92;
        raf = requestAnimationFrame(glide);
      };
      glide();
    });
    // a drag shouldn't also press whatever it started on
    el.addEventListener(
      "click",
      (ev) => {
        if (!moved) return;
        ev.stopPropagation();
        ev.preventDefault();
        moved = false;
      },
      true
    );
  }

  // drift the topic tape gently until first touch — a quiet hint that
  // there's more to the right
  function startTapeDrift() {
    const tape = document.getElementById("topic-chips");
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    // track position as a float: scrollLeft truncates on read, so a
    // sub-pixel increment would otherwise never accumulate
    let pos = tape.scrollLeft;
    let dir = 1;
    let raf;
    const step = () => {
      const max = tape.scrollWidth - tape.clientWidth;
      if (max > 4) {
        pos += 0.3 * dir;
        if (pos >= max - 1) dir = -1;
        else if (pos <= 0) dir = 1;
        tape.scrollLeft = pos;
      }
      raf = requestAnimationFrame(step);
    };
    const stop = () => cancelAnimationFrame(raf);
    for (const ev of ["pointerdown", "wheel", "touchstart"])
      tape.addEventListener(ev, stop, { once: true, passive: true });
    raf = requestAnimationFrame(step);
  }

  function topicKeyFromSlug(slug) {
    if (!slug) return null;
    const s = slug.toLowerCase();
    for (const [key, val] of Object.entries(TOPIC_SLUGS)) {
      if (val === s) return key;
    }
    // also accept the raw topic key (encoded)
    if (TOPICS.includes(slug)) return slug;
    return null;
  }

  function clearLanding({ keepUrl } = {}) {
    state.landing = null;
    if (!keepUrl && (location.search || "").length > 1) {
      const path = location.pathname + (location.hash || "");
      history.replaceState(null, "", path);
    }
  }

  // Header tagline landings: /?topic=psychedelics or /?category=expand
  function applyLandingFromUrl() {
    const params = new URLSearchParams(location.search);
    const topicSlug = params.get("topic");
    const cat = params.get("category");
    if (topicSlug) {
      const key = topicKeyFromSlug(topicSlug);
      if (key && TOPICS.includes(key)) {
        state.landing = { kind: "topic", key };
        state.topics = new Set([key]);
        state.category = "all";
        state.day = "30";
        return true;
      }
    }
    if (cat && CATEGORIES[cat]) {
      state.landing = { kind: "category", key: cat };
      state.category = cat;
      state.topics.clear();
      state.day = "30";
      return true;
    }
    return false;
  }

  function landingCopy() {
    if (!state.landing) return null;
    const { kind, key } = state.landing;
    const pack = (LANDING_INTROS[kind] || {})[key];
    if (pack) return pack;
    return {
      title: key,
      paras: [`In-person ${key} gatherings in London.`],
    };
  }

  function renderLandingIntro() {
    const copy = landingCopy();
    if (!copy) return null;
    const head = document.createElement("header");
    head.className = "landing-intro";

    const kicker = document.createElement("p");
    kicker.className = "landing-kicker";
    kicker.textContent = "in person · london";
    head.appendChild(kicker);

    const h2 = document.createElement("h2");
    h2.className = "landing-title";
    h2.textContent = copy.title;
    head.appendChild(h2);

    for (const text of copy.paras || []) {
      const p = document.createElement("p");
      p.className = "landing-lead";
      p.textContent = text;
      head.appendChild(p);
    }

    const clear = document.createElement("button");
    clear.type = "button";
    clear.className = "landing-clear";
    clear.textContent = "show everything";
    clear.addEventListener("click", () => {
      clearLanding();
      state.category = "all";
      state.topics.clear();
      state.day = "7";
      document
        .querySelectorAll("#category-pills .key")
        .forEach((k) =>
          k.classList.toggle("lit", k.dataset.category === "all")
        );
      syncDayTicks();
      syncTopicTokens();
      render();
    });
    head.appendChild(clear);
    return head;
  }

  function resetFilters() {
    clearLanding();
    state.category = "all";
    state.topics.clear();
    state.area = "all";
    state.day = "7";
    state.freeOnly = false;
    state.query = "";
    state.surprise = null;
    document.getElementById("search").value = "";
    document.getElementById("free-toggle").checked = false;
    document
      .querySelectorAll("#category-pills .key")
      .forEach((k) => k.classList.toggle("lit", k.dataset.category === "all"));
    syncDayTicks();
    setLens("all");
    syncCompass();
    syncTopicTokens();
    render();
  }

  function renderLastUpdated() {
    const latest = state.events.reduce(
      (max, e) => (e.last_seen_at > max ? e.last_seen_at : max),
      ""
    );
    if (!latest) return;
    const el = document.getElementById("last-updated");
    el.textContent = `events last updated ${relativeTime(new Date(latest))}`;
    el.title = new Date(latest).toLocaleString("en-GB", {
      timeZone: "Europe/London",
    });
    el.hidden = false;
  }

  function relativeTime(date) {
    const mins = Math.round((Date.now() - date.getTime()) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.round(hours / 24);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }

  // ---------- rendering ----------

  function setView(view) {
    // map can be hidden per-site (FEATURES.map: false) without killing
    // the tonight/browse tabs
    if (view === "map" && FEATURES.map === false) view = "browse";
    state.view = view;
    state.surprise = null;
    document
      .querySelectorAll(".view-key")
      .forEach((k) => k.classList.toggle("lit", k.dataset.view === view));
    countView(view);
    render();
  }

  function render() {
    const main = document.getElementById("events");
    const mapView = document.getElementById("map-view");

    mapView.hidden = state.view !== "map";
    main.hidden = state.view === "map";

    if (state.view === "map") {
      renderMap();
      return;
    }
    if (state.view === "tonight") {
      renderTonight(main);
      return;
    }
    renderBrowse(main);
  }

  // True when a wider day window (30 days) would surface more listings than
  // the current filter. Search already spans the full horizon; on "30" there
  // is nothing further to open.
  function canExpandTo30() {
    if (state.day === "30" || state.query.trim()) return false;
    const shown = browseEvents().length;
    const until30 = Date.now() + 30 * 864e5;
    const all30 = state.events.filter(
      (e) => baseFilter(e) && new Date(e.start_at).getTime() <= until30
    ).length;
    return all30 > shown;
  }

  function expandTo30() {
    if (state.day === "30") return;
    state.day = "30";
    state.surprise = null;
    syncDayTicks();
    render();
  }

  function showMoreButton() {
    const wrap = document.createElement("div");
    wrap.className = "show-more";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "key show-more-btn";
    btn.textContent = "show more";
    btn.title = "show the next 30 days";
    btn.addEventListener("click", expandTo30);
    wrap.appendChild(btn);
    return wrap;
  }

  function renderBrowse(container) {
    const events = browseEvents();
    const frag = document.createDocumentFragment();
    const intro = renderLandingIntro();
    if (intro) frag.appendChild(intro);

    if (!events.length) {
      if (canExpandTo30()) {
        const empty = document.createElement("p");
        empty.className = "status";
        empty.textContent =
          "nothing in this window — try a wider look.";
        frag.appendChild(empty);
        frag.appendChild(showMoreButton());
        container.replaceChildren(frag);
        return;
      }
      const empty = document.createElement("p");
      empty.className = "status";
      empty.textContent =
        "nothing here — the city is resting. try a wider window.";
      frag.appendChild(empty);
      container.replaceChildren(frag);
      return;
    }

    const byDay = new Map();
    for (const e of events) {
      const day = new Date(e.start_at).toLocaleDateString("en-GB", {
        weekday: "long",
        day: "numeric",
        month: "long",
        timeZone: "Europe/London",
      });
      if (!byDay.has(day)) byDay.set(day, []);
      byDay.get(day).push(e);
    }

    for (const [day, dayEvents] of byDay) {
      const section = document.createElement("section");
      section.className = "day-group";

      const h2 = document.createElement("h2");
      h2.className = "day-heading";

      const relative = relativeLabel(dayEvents[0].start_at);
      if (relative) {
        const when = document.createElement("span");
        when.className = "when";
        when.textContent = relative;
        h2.appendChild(when);
      }
      const name = document.createElement("span");
      // relative already says which day it is ("today —"/"tomorrow —"),
      // so drop the weekday here — "Friday 17 July" was long enough to
      // wrap the heading onto two lines on mobile.
      name.textContent = relative
        ? new Date(dayEvents[0].start_at).toLocaleDateString("en-GB", {
            day: "numeric",
            month: "long",
            timeZone: "Europe/London",
          })
        : day;
      h2.appendChild(name);

      const count = document.createElement("span");
      count.className = "count";
      count.textContent =
        dayEvents.length === 1 ? "one gathering" : `${dayEvents.length} gatherings`;
      h2.appendChild(count);

      section.appendChild(h2);

      const grid = document.createElement("div");
      grid.className = "grid";
      dayEvents.forEach((e, i) => grid.appendChild(card(e, i)));
      section.appendChild(grid);
      frag.appendChild(section);
    }
    if (canExpandTo30()) frag.appendChild(showMoreButton());
    container.replaceChildren(frag);
  }

  function renderTonight(container) {
    const events = tonightEvents();

    const frag = document.createDocumentFragment();
    const head = document.createElement("div");
    head.className = "tonight-head";
    const h2 = document.createElement("h2");
    h2.className = "tonight-title";
    h2.textContent = "tonight";
    const sub = document.createElement("p");
    sub.className = "tonight-sub";
    sub.textContent = events.length
      ? `${events.length} gathering${events.length === 1 ? "" : "s"} still to come in london`
      : "";
    const dice = document.createElement("button");
    dice.className = "key surprise";
    dice.textContent = "surprise me";
    dice.addEventListener("click", () => {
      const pool = events.length ? events : weekPool();
      if (!pool.length) return;
      let pick = pool[Math.floor(Math.random() * pool.length)];
      if (pool.length > 1 && state.surprise && pick === state.surprise) {
        pick = pool[(pool.indexOf(pick) + 1) % pool.length];
      }
      state.surprise = pick;
      render();
    });
    head.append(h2, sub, dice);
    frag.appendChild(head);

    if (state.surprise) {
      const note = document.createElement("p");
      note.className = "status surprise-note";
      note.textContent = "the dice say —";
      frag.appendChild(note);
      const grid = document.createElement("div");
      grid.className = "grid grid-solo";
      grid.appendChild(card(state.surprise, 0));
      frag.appendChild(grid);
      const back = document.createElement("button");
      back.className = "key show-all";
      back.textContent = "show everything tonight";
      back.addEventListener("click", () => {
        state.surprise = null;
        render();
      });
      frag.appendChild(back);
      container.replaceChildren(frag);
      return;
    }

    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "status";
      empty.textContent =
        "the city is quiet tonight — try surprise me for the days ahead.";
      frag.appendChild(empty);
      container.replaceChildren(frag);
      return;
    }

    const grid = document.createElement("div");
    grid.className = "grid";
    events.forEach((e, i) => grid.appendChild(card(e, i)));
    frag.appendChild(grid);
    container.replaceChildren(frag);
  }

  function weekPool() {
    const until = Date.now() + 7 * 864e5;
    return state.events.filter(
      (e) => baseFilter(e) && new Date(e.start_at).getTime() <= until
    );
  }

  function relativeLabel(startAt) {
    const fmt = (d) =>
      d.toLocaleDateString("en-GB", { timeZone: "Europe/London" });
    const eventDay = fmt(new Date(startAt));
    const now = new Date();
    if (eventDay === fmt(now)) return "today —";
    if (eventDay === fmt(new Date(now.getTime() + 864e5))) return "tomorrow —";
    return "";
  }

  // ---------- map ----------

  function renderMap() {
    if (state.mapLoaded) {
      drawMarkers();
      return;
    }
    state.mapLoaded = true;
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
    document.head.appendChild(css);
    const script = document.createElement("script");
    script.src = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
    script.onload = () => {
      state.map = L.map("map", { scrollWheelZoom: true }).setView(
        [51.5072, -0.1276],
        11
      );
      L.tileLayer(
        `https://{s}.basemaps.cartocdn.com/${SITE.mapTiles || "dark_all"}/{z}/{x}/{y}{r}.png`,
        {
          attribution:
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
          maxZoom: 19,
        }
      ).addTo(state.map);
      state.markerLayer = L.layerGroup().addTo(state.map);
      drawMarkers();
    };
    script.onerror = () => {
      document.getElementById("map").innerHTML =
        '<p class="status">the map wouldn\'t load — try again later.</p>';
    };
    document.head.appendChild(script);
  }

  function drawMarkers() {
    if (!state.map || !state.markerLayer) return;
    state.markerLayer.clearLayers();
    for (const e of mapEvents()) {
      const color = (CATEGORIES[e.category] || { color: "#8e7aa8" }).color;
      const marker = L.circleMarker([e.latitude, e.longitude], {
        radius: 7,
        color,
        weight: 1.5,
        fillColor: color,
        fillOpacity: 0.65,
      });
      const when = new Date(e.start_at).toLocaleDateString("en-GB", {
        weekday: "short",
        day: "numeric",
        month: "short",
        timeZone: "Europe/London",
      });
      marker.bindPopup(
        `<strong>${escapeHtml(e.title)}</strong><br>` +
          `${when} · ${escapeHtml(formatTime(e))}<br>` +
          `${escapeHtml(e.venue_name || e.address || "")}<br>` +
          `<a href="${escapeHtml(withUtm(e.source_url))}" target="_blank" rel="noopener">open ↗</a>`
      );
      state.markerLayer.addLayer(marker);
    }
    // Leaflet mis-sizes when initialized while hidden
    setTimeout(() => state.map.invalidateSize(), 60);
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
    );
  }

  // ---------- cards ----------

  function card(e, index) {
    const a = document.createElement("a");
    a.className = "card";
    a.href = withUtm(e.source_url);
    a.target = "_blank";
    a.rel = "noopener";
    a.style.animationDelay = `${Math.min(index * 45, 360)}ms`;

    const banner = document.createElement("div");
    banner.className = "banner";
    if (e.image_url) {
      const img = document.createElement("img");
      img.src = e.image_url;
      img.alt = "";
      img.loading = "lazy";
      img.onerror = () => {
        img.remove();
        paintPlaceholder(banner, e);
      };
      banner.appendChild(img);
    } else {
      paintPlaceholder(banner, e);
    }

    if (e.category && CATEGORIES[e.category]) {
      const cat = document.createElement("span");
      cat.className = `badge badge-cat badge-cat-${e.category}`;
      cat.textContent = CATEGORIES[e.category].label;
      banner.appendChild(cat);
    }

    if ((e.quality_score ?? 0) >= PICK_THRESHOLD) {
      const pick = document.createElement("span");
      pick.className = "pick-mark";
      pick.textContent = "✦ pick";
      pick.title = "one of the richer listings this week";
      banner.appendChild(pick);
    }

    const body = document.createElement("div");
    body.className = "card-body";

    const time = document.createElement("p");
    time.className = "time";
    const dot = document.createElement("span");
    dot.className = `dot ${timeOfDayClass(e)}`;
    time.appendChild(dot);
    time.appendChild(document.createTextNode(formatTime(e)));
    body.appendChild(time);

    const title = document.createElement("h3");
    title.textContent = e.title;
    body.appendChild(title);

    if (e.hook) {
      const hook = document.createElement("p");
      hook.className = "hook";
      hook.textContent = e.hook;
      body.appendChild(hook);
    }

    if (e.venue_name || e.address) {
      const venue = document.createElement("p");
      venue.className = "venue";
      venue.textContent = e.venue_name || e.address;
      body.appendChild(venue);
    }

    const meta = document.createElement("p");
    meta.className = "meta";
    if (e.is_free) {
      const free = document.createElement("span");
      free.className = "free-tag";
      free.textContent = "free";
      meta.appendChild(free);
    } else if (e.price_min != null) {
      meta.appendChild(
        document.createTextNode(
          e.price_min === e.price_max || e.price_max == null
            ? `£${e.price_min}`
            : `£${e.price_min}–£${e.price_max}`
        )
      );
    }
    if (e.organizer_name) {
      if (meta.childNodes.length)
        meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(document.createTextNode(e.organizer_name));
    }
    if (e.area) {
      if (meta.childNodes.length)
        meta.appendChild(document.createTextNode(" · "));
      meta.appendChild(document.createTextNode(e.area));
    }
    if (meta.childNodes.length) body.appendChild(meta);

    if (!e.hook && e.description) {
      const blurb = document.createElement("p");
      blurb.className = "blurb";
      blurb.textContent = e.description.replace(/\s+/g, " ").slice(0, 220);
      body.appendChild(blurb);
    }

    a.append(banner, body);
    return a;
  }

  function timeOfDayClass(e) {
    const hour = londonHour(e.start_at);
    if (hour < 12) return "dot-morning";
    if (hour < 17) return "dot-afternoon";
    return "dot-evening";
  }

  function paintPlaceholder(banner, e) {
    let c1, c2;
    if (e.category && CATEGORIES[e.category]) {
      ({ color: c1, colorB: c2 } = CATEGORIES[e.category]);
    } else {
      [c1, c2] = GRADIENTS[hash(e.title || "?") % GRADIENTS.length];
    }
    banner.style.background = `linear-gradient(135deg, ${c1}, ${c2})`;
    const span = document.createElement("span");
    span.className = "placeholder-initial";
    span.textContent = (e.title || "?").trim();
    banner.appendChild(span);
  }

  function hash(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return h;
  }

  function formatTime(e) {
    if (e.is_all_day) return "All day";
    const opts = {
      hour: "numeric",
      minute: "2-digit",
      timeZone: "Europe/London",
    };
    const start = new Date(e.start_at).toLocaleTimeString("en-GB", opts);
    if (!e.end_at) return start;
    const end = new Date(e.end_at).toLocaleTimeString("en-GB", opts);
    return `${start} – ${end}`;
  }

  // ---------- spotlight: our next event + this week's picks ----------
  //
  // One 3-column row on desktop: "Our next event" heads column 1 (if a
  // featured event is live) and "Our top picks this week" heads the
  // remaining columns (2 picks alongside a featured event, or all 3 if
  // there's no featured event). Mobile hides the picks entirely and shows
  // only the featured card, if any — a swipeable picks strip on top of a
  // hero card was too much scroll before the browse grid.

  // Selects up to `limit` events from SITE.curated.organizers/titleMatches,
  // within the next windowDays, dropping SITE.curated.exclude title matches
  // (e.g. Numinity's weekly running club). Spreads picks across distinct
  // organisers (round-robin, earliest event each) rather than stacking
  // repeats of one — priorityOrganizer is only ever the first pick when
  // there's just one candidate slot, and only contributes a 2nd/3rd pick
  // once every other organiser in the window has already had a turn.
  function pickCurated(excludeSourceUrl, limit) {
    const cfg = SITE.curated;
    if (!cfg) return [];
    const maxTotal = limit != null ? limit : cfg.maxTotal || 3;
    if (maxTotal <= 0) return [];
    const now = Date.now();
    const horizon = now + (cfg.windowDays || 7) * 86400000;
    const excludeTerms = (cfg.exclude || []).map((t) => t.toLowerCase());
    // state.events is start_at-ascending, so candidates stay in date order.
    const candidates = state.events.filter((e) => {
      if (!isCurated(e)) return false;
      if (excludeSourceUrl && e.source_url === excludeSourceUrl) return false;
      const t = new Date(e.start_at).getTime();
      if (!(t > now && t <= horizon)) return false;
      const title = (e.title || "").toLowerCase();
      return !excludeTerms.some((term) => title.includes(term));
    });
    if (!candidates.length) return [];

    const priority = (cfg.priorityOrganizer || "").toLowerCase();
    const orgKey = (e) => (e.organizer_name || "").toLowerCase();
    const byOrg = new Map();
    for (const e of candidates) {
      if (!byOrg.has(orgKey(e))) byOrg.set(orgKey(e), []);
      byOrg.get(orgKey(e)).push(e);
    }
    // organisers in date-of-earliest-event order, priority pushed last so
    // it's the last to get a look-in during the round-robin pass
    const orgOrder = [...byOrg.keys()].sort((a, b) => {
      if (a === priority && b !== priority) return 1;
      if (b === priority && a !== priority) return -1;
      return (
        new Date(byOrg.get(a)[0].start_at) - new Date(byOrg.get(b)[0].start_at)
      );
    });

    const picks = [];
    const used = new Set();
    for (const org of orgOrder) {
      if (picks.length >= maxTotal) break;
      const e = byOrg.get(org)[0];
      picks.push(e);
      used.add(e);
    }
    // still short (fewer distinct organisers than maxTotal) — backfill
    // from remaining candidates, in date order, repeats allowed
    for (const e of candidates) {
      if (picks.length >= maxTotal) break;
      if (used.has(e)) continue;
      picks.push(e);
      used.add(e);
    }

    return picks.sort((a, b) => new Date(a.start_at) - new Date(b.start_at));
  }

  // kind: "featured" (our own event) or "pick" (curated third party) —
  // only used for the card's border colour now; the group heading carries
  // the "what is this" label.
  function spotlightCard(e, kind) {
    const card = document.createElement("a");
    card.className = "spotlight-card spotlight-" + kind;
    card.href = withUtm(e.source_url);
    card.target = "_blank";
    card.rel = "noopener";

    if (e.image_url) {
      const media = document.createElement("div");
      media.className = "spotlight-media";
      const img = document.createElement("img");
      img.src = e.image_url;
      img.alt = "";
      img.onerror = () => media.remove();
      media.appendChild(img);
      card.appendChild(media);
    }

    const body = document.createElement("div");
    body.className = "spotlight-body";

    if (kind === "pick" && e.organizer_name) {
      const eyebrow = document.createElement("p");
      eyebrow.className = "spotlight-eyebrow";
      eyebrow.textContent = e.organizer_name;
      body.appendChild(eyebrow);
    }

    const title = document.createElement("h3");
    title.textContent = e.title;
    body.appendChild(title);

    const when = document.createElement("p");
    when.className = "spotlight-when";
    const day = new Date(e.start_at).toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
      timeZone: "Europe/London",
    });
    when.append([day, formatTime(e)].filter(Boolean).join(" · "));
    // venue is its own span so mobile can drop it (see .spotlight-venue)
    if (e.venue_name) {
      const venue = document.createElement("span");
      venue.className = "spotlight-venue";
      venue.textContent = " · " + e.venue_name;
      when.appendChild(venue);
    }
    body.appendChild(when);

    const blurb = (e.hook || e.description || "").trim();
    if (blurb) {
      const p = document.createElement("p");
      p.className = "spotlight-blurb";
      p.textContent =
        blurb.length > 200 ? blurb.slice(0, 197).trimEnd() + "…" : blurb;
      body.appendChild(p);
    }

    card.appendChild(body);
    return card;
  }

  // Day-heading-styled <h2> placed at a given grid column span, so it
  // heads exactly the columns its cards occupy below it. `accentPhrase`
  // (from SITE.featured.accent/SITE.curated.accent) gets the same accent
  // colour as a day-heading's "today —" (class "when" reuses that rule);
  // the rest of the label stays plain ink.
  function spotlightHeading(label, accentPhrase, className, colStart, colEnd) {
    const h2 = document.createElement("h2");
    h2.className = "day-heading spotlight-heading " + className;
    h2.style.gridColumn = colStart + " / " + colEnd;

    const idx = accentPhrase ? label.indexOf(accentPhrase) : -1;
    if (idx === -1) {
      h2.textContent = label;
      return h2;
    }
    const before = label.slice(0, idx);
    const after = label.slice(idx + accentPhrase.length);
    if (before) h2.appendChild(document.createTextNode(before));
    const when = document.createElement("span");
    when.className = "when";
    when.textContent = accentPhrase;
    h2.appendChild(when);
    if (after) h2.appendChild(document.createTextNode(after));

    return h2;
  }

  function renderSpotlight() {
    const section = document.getElementById("spotlight");
    // state.events is start_at-sorted, so the first match is the next one
    const featured =
      SITE.featured &&
      state.events.find(
        (ev) => isOurs(ev) && new Date(ev.start_at).getTime() > Date.now()
      );
    // Always fetch up to maxTotal (default 3). Desktop shows featured + 2
    // picks in one 3-column row (the 3rd pick is hidden with CSS); the mobile
    // swipe strip shows all three. Without a featured event, picks get the
    // full row on both.
    const pickLimit = SITE.curated ? SITE.curated.maxTotal || 3 : 0;
    const picks = pickLimit
      ? pickCurated(featured && featured.source_url, pickLimit)
      : [];

    if (!featured && !picks.length) {
      section.hidden = true;
      return;
    }

    const grid = document.createElement("div");
    grid.className = "spotlight-grid";
    section.classList.toggle("no-featured", !featured);

    const pickStart = featured ? 2 : 1;
    // Heading spans only the desktop-visible picks (featured caps the row at
    // 2) so the extra 3rd pick can't force an implicit 4th grid column.
    const desktopPicks = featured ? Math.min(picks.length, 2) : picks.length;
    const pickEnd = pickStart + Math.max(desktopPicks, featured ? 0 : 1);

    if (featured) {
      grid.appendChild(
        spotlightHeading(
          SITE.featured.label || "Our next event",
          SITE.featured.accent || "next event",
          "spotlight-heading-featured",
          1,
          2
        )
      );
    }
    if (picks.length) {
      grid.appendChild(
        spotlightHeading(
          (SITE.curated && SITE.curated.label) || "Our top picks this week",
          (SITE.curated && SITE.curated.accent) || "top picks",
          "spotlight-heading-picks",
          pickStart,
          pickEnd
        )
      );
    }

    if (featured) {
      const card = spotlightCard(featured, "featured");
      card.style.gridColumn = "1";
      grid.appendChild(card);
    }
    // Picks live in their own wrapper so mobile can turn them into a
    // horizontal swipe strip; on desktop the wrapper is display:contents, so
    // the cards rejoin the grid and their inline grid-column places them.
    if (picks.length) {
      const picksWrap = document.createElement("div");
      picksWrap.className = "spotlight-picks";
      // Desktop shows exactly the picks it showed before (2 alongside a
      // featured event); pickCurated date-sorts, so the 2-of-3 to keep is the
      // limit-2 selection, not "the first two by date". The extra pick is
      // strip-only. Non-extra picks take contiguous columns; the extra is
      // display:none on desktop so its column never matters.
      const desktopSet = featured
        ? new Set(pickCurated(featured.source_url, 2))
        : new Set(picks);
      let col = pickStart;
      picks.forEach((e) => {
        const card = spotlightCard(e, "pick");
        if (desktopSet.has(e)) {
          card.style.gridColumn = String(col++);
        } else {
          card.classList.add("spotlight-pick-extra");
        }
        picksWrap.appendChild(card);
      });
      grid.appendChild(picksWrap);
    }

    section.replaceChildren(grid);
    section.hidden = false;
  }

  // ---------- analytics (GoatCounter: open source, cookieless) ----------

  function initAnalytics() {
    const endpoint = window.LONDO_CONFIG.GOATCOUNTER;
    if (!endpoint) return;
    const s = document.createElement("script");
    s.async = true;
    s.src = "https://gc.zgo.at/count.js";
    s.dataset.goatcounter = endpoint;
    document.head.appendChild(s);
  }

  // count tab switches as virtual pageviews (/tonight, /map)
  function countView(view) {
    if (window.goatcounter && window.goatcounter.count) {
      window.goatcounter.count({
        path: "/" + view,
        title: (SITE.name || "londo") + " — " + view,
        event: false,
      });
    }
  }

  // ---------- theming ----------

  function applyTimeTheme() {
    const hour = new Date().getHours();
    const theme =
      hour >= 5 && hour < 11
        ? "dawn"
        : hour >= 11 && hour < 17
          ? "day"
          : hour >= 17 && hour < 22
            ? "dusk"
            : "night";
    document.body.classList.add(`theme-${theme}`);
  }

  // ---------- wiring ----------

  function bindControls() {
    document.getElementById("search").addEventListener("input", (ev) => {
      state.query = ev.target.value;
      state.surprise = null;
      render();
    });

    // lens toggle: pick a stop directly
    document.getElementById("lens-toggle").addEventListener("click", (ev) => {
      const stop = ev.target.closest(".lens-stop");
      if (!stop || stop.dataset.lens === state.lens) return;
      state.surprise = null;
      setLens(stop.dataset.lens);
      render();
    });

    // view keys live in the header (desktop) and bottom bar (mobile)
    for (const navId of ["view-tabs", "bottom-tabs"]) {
      document.getElementById(navId).addEventListener("click", (ev) => {
        const btn = ev.target.closest("button[data-view]");
        if (!btn) return;
        setView(btn.dataset.view);
      });
    }

    document.getElementById("category-pills").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-category]");
      if (!btn) return;
      clearLanding();
      state.category = btn.dataset.category;
      state.surprise = null;
      document
        .querySelectorAll("#category-pills .key")
        .forEach((p) => p.classList.toggle("lit", p === btn));
      render();
    });

    // area dial: press a zone to select; press the lit zone (or tap the
    // readout) to go back to anywhere
    document.getElementById("compass-unit").addEventListener("click", (ev) => {
      if (ev.target.closest("#area-readout")) {
        if (state.area === "all") return;
        state.area = "all";
      } else {
        const zone = ev.target.closest(".zone");
        if (!zone) return;
        state.area = state.area === zone.dataset.area ? "all" : zone.dataset.area;
      }
      state.surprise = null;
      syncCompass();
      render();
    });

    // pinned 7/30 day ranges next to the date ticker
    document.getElementById("range-ticks").addEventListener("click", (ev) => {
      const btn = ev.target.closest(".tick");
      if (!btn) return;
      if (state.day === btn.dataset.day) return;
      state.day = btn.dataset.day;
      state.surprise = null;
      syncDayTicks();
      render();
    });

    enableDragScroll(document.getElementById("week-strip"));
    enableDragScroll(document.getElementById("topic-chips"));

    document.getElementById("free-toggle").addEventListener("change", (ev) => {
      state.freeOnly = ev.target.checked;
      state.surprise = null;
      render();
    });

    document
      .getElementById("reset-filters")
      .addEventListener("click", resetFilters);
  }

  const LOADING_LINES = [
    "gathering events…",
    "consulting the city…",
    "finding the good stuff…",
    "asking around…",
    "scanning noticeboards…",
    "following the chalk arrows…",
    "checking what's on…",
    "listening for drums…",
    "something's happening tonight…",
    "the city never really sleeps…",
    "hold tight…",
  ];

  function startLoadingCycle() {
    const el = document.getElementById("loading-msg");
    if (!el) return;
    let i = 0;
    const reduceMotion = window.matchMedia(
      "(prefers-reduced-motion: reduce)"
    ).matches;
    return setInterval(() => {
      i = (i + 1) % LOADING_LINES.length;
      if (reduceMotion) {
        el.textContent = LOADING_LINES[i];
        return;
      }
      el.classList.add("is-fading");
      window.setTimeout(() => {
        el.textContent = LOADING_LINES[i];
        el.classList.remove("is-fading");
      }, 280);
    }, 2200);
  }

  // Footer topic links → SPA landings (?topic=), same as the header tagline.
  // Static /t/<slug>/ pages stay in the sitemap and on static-page footers for
  // crawl; in-app links must not depend on directory-index hosting (which can
  // show a raw folder listing instead of a page).
  function renderSeoNav() {
    const nav = document.getElementById("seo-nav");
    if (!nav) return;
    const frag = document.createDocumentFragment();
    let first = true;
    for (const topic of TOPICS) {
      const slug = TOPIC_SLUGS[topic];
      if (!slug) continue;
      if (!first) {
        const sep = document.createElement("span");
        sep.className = "seo-sep";
        sep.setAttribute("aria-hidden", "true");
        sep.textContent = "·";
        frag.appendChild(sep);
      }
      first = false;
      const a = document.createElement("a");
      a.href = `?topic=${encodeURIComponent(slug)}`;
      a.textContent = topic;
      frag.appendChild(a);
    }
    nav.replaceChildren(frag);
  }

  // --- Launch splash (installed app only) -------------------------------
  // A full-screen branded launch screen — the loader orb + site logo — shown
  // only when opened standalone (the installed app), so ordinary web visits
  // don't gain an extra splash step. Reuses the existing .loader markup/CSS.
  let splashEl = null;
  function showSplash() {
    if (!isStandalone() || document.getElementById("splash")) return;
    const el = document.createElement("div");
    el.id = "splash";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML =
      (SITE.logo ? `<img class="splash-logo" src="${SITE.logo}" alt="">` : "") +
      '<div class="loader">' +
      '<span class="loader-ring r1"></span>' +
      '<span class="loader-ring r2"></span>' +
      '<span class="loader-ring r3"></span>' +
      '<span class="loader-core"></span>' +
      '<span class="loader-spark s1"></span>' +
      '<span class="loader-spark s2"></span>' +
      '<span class="loader-spark s3"></span>' +
      '<span class="loader-spark s4"></span>' +
      "</div>";
    document.body.appendChild(el);
    splashEl = el;
  }
  function hideSplash() {
    if (!splashEl) return;
    const el = splashEl;
    splashEl = null;
    if (prefersReducedMotion()) {
      el.remove();
      return;
    }
    el.classList.add("is-hiding");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
    // belt-and-braces: if transitionend never fires, still clean up
    window.setTimeout(() => el.remove(), 700);
  }

  // --- Install hint banner ----------------------------------------------
  // Non-invasive, dismissible nudge. Platform-split: Android/Chromium gets a
  // real one-tap install (beforeinstallprompt); iOS Safari gets the manual
  // "Share -> Add to Home Screen" hint (iOS exposes no install API).
  const INSTALL_HINT_KEY = "install-hint-dismissed";
  const INSTALL_HINT_REDISPLAY_MS = 60 * 24 * 60 * 60 * 1000; // ~60 days
  const INSTALL_HINT_DELAY_MS = 4000;
  let deferredPrompt = null;

  function installHintDismissed() {
    try {
      const t = Number(localStorage.getItem(storeKey(INSTALL_HINT_KEY)));
      return t && Date.now() - t < INSTALL_HINT_REDISPLAY_MS;
    } catch (_) {
      return false;
    }
  }
  function markInstallHintDismissed() {
    try {
      localStorage.setItem(storeKey(INSTALL_HINT_KEY), String(Date.now()));
    } catch (_) {
      /* private mode / storage disabled — just skip persistence */
    }
  }

  // Register listeners early: beforeinstallprompt can fire before the banner's
  // delayed appearance, and appinstalled can arrive from the browser UI.
  function initInstallHint() {
    if (FEATURES.installHint === false || isStandalone()) return;
    window.addEventListener("beforeinstallprompt", (e) => {
      e.preventDefault();
      deferredPrompt = e;
    });
    window.addEventListener("appinstalled", () => {
      markInstallHintDismissed();
      removeInstallHint();
    });
  }

  function removeInstallHint() {
    const el = document.getElementById("install-hint");
    if (!el) return;
    if (prefersReducedMotion()) {
      el.remove();
      return;
    }
    el.classList.remove("is-in");
    el.addEventListener("transitionend", () => el.remove(), { once: true });
    window.setTimeout(() => el.remove(), 500);
  }

  const SHARE_SVG =
    '<svg class="install-hint-share" viewBox="0 0 24 24" aria-hidden="true" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><path d="M12 15V3"/><path d="m7 8 5-5 5 5"/>' +
    '<path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/></svg>';

  function renderInstallHint(mode) {
    if (!mode || document.getElementById("install-hint")) return;
    const name = SITE.displayName || SITE.name || "londo";

    const wrap = document.createElement("div");
    wrap.id = "install-hint";
    wrap.className = "install-hint";
    wrap.setAttribute("role", "dialog");
    wrap.setAttribute("aria-label", `install ${name}`);

    const icon = document.createElement("div");
    icon.className = "install-hint-icon";
    icon.setAttribute("aria-hidden", "true");
    // square app icon, not the wide wordmark logo (which squashes in a box)
    icon.innerHTML = '<img src="icons/apple-touch-icon.png" alt="">';

    const body = document.createElement("div");
    body.className = "install-hint-body";
    const title = document.createElement("p");
    title.className = "install-hint-title";
    title.textContent = `Install ${name}`;
    const text = document.createElement("p");
    text.className = "install-hint-text";
    if (mode === "ios") {
      text.innerHTML = `Tap ${SHARE_SVG} then <b>Add to Home Screen</b>.`;
    } else if (mode === "ios-other") {
      // iOS can only install from Safari; other iOS browsers can't
      text.innerHTML =
        `Open in <b>Safari</b>, then Share ${SHARE_SVG} → <b>Add to Home Screen</b>.`;
    } else {
      text.textContent = "Add it to your home screen for full-screen access.";
    }
    body.append(title, text);

    wrap.append(icon, body);

    if (mode === "android") {
      const install = document.createElement("button");
      install.className = "install-hint-install";
      install.type = "button";
      install.textContent = "Install";
      install.addEventListener("click", async () => {
        if (!deferredPrompt) {
          removeInstallHint();
          return;
        }
        deferredPrompt.prompt();
        try {
          await deferredPrompt.userChoice;
        } catch (_) {
          /* ignore */
        }
        deferredPrompt = null;
        markInstallHintDismissed();
        removeInstallHint();
      });
      wrap.append(install);
    }

    const close = document.createElement("button");
    close.className = "install-hint-close";
    close.type = "button";
    close.setAttribute("aria-label", "dismiss");
    close.innerHTML = "&times;";
    close.addEventListener("click", () => {
      markInstallHintDismissed();
      removeInstallHint();
    });
    wrap.append(close);

    document.body.appendChild(wrap);
    // next frame so the slide-up transition runs
    requestAnimationFrame(() => wrap.classList.add("is-in"));
  }

  // Called after the first successful render: wait a beat (non-invasive),
  // then show the hint if we can actually guide an install on this platform.
  function maybeShowInstallHint() {
    if (FEATURES.installHint === false) return;
    if (isStandalone() || installHintDismissed()) return;
    if (!deferredPrompt && !isIOS()) return;
    window.setTimeout(() => {
      if (isStandalone() || installHintDismissed()) return;
      // Android/Chromium: real one-tap install. iOS Safari: Share -> Add to
      // Home Screen. Other iOS browsers: can't install, so tell them to open
      // in Safari first.
      const mode = deferredPrompt
        ? "android"
        : isIOSSafari()
        ? "ios"
        : isIOS()
        ? "ios-other"
        : null;
      renderInstallHint(mode);
    }, INSTALL_HINT_DELAY_MS);
  }

  async function init() {
    showSplash();
    initInstallHint();
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => {});
    }
    applyTimeTheme();
    initAnalytics();
    renderSeoNav();
    bindControls();
    if (FEATURES.views === false) {
      // single-view site: browse only, no tab bars
      document.getElementById("view-tabs").hidden = true;
      document.getElementById("bottom-tabs").hidden = true;
    } else if (FEATURES.map === false) {
      // keep browse/tonight; hide map entry points only
      document
        .querySelectorAll('.view-key[data-view="map"]')
        .forEach((el) => {
          el.hidden = true;
        });
    }
    renderWeekStrip();
    setLens("all");
    const loadingTimer = startLoadingCycle();
    if (SUPABASE_URL.startsWith("YOUR_")) {
      clearInterval(loadingTimer);
      document.getElementById("events").innerHTML =
        '<p class="status">set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js</p>';
      return;
    }
    try {
      state.events = (await fetchEvents()).filter(siteMatch);
      clearInterval(loadingTimer);
      applyLandingFromUrl();
      // sync chrome to any landing filter (topic chips / category / day)
      document
        .querySelectorAll("#category-pills .key")
        .forEach((k) =>
          k.classList.toggle("lit", k.dataset.category === state.category)
        );
      syncDayTicks();
      renderSpotlight();
      maybeShowEnrichedControls();
      // topic chips only exist after maybeShowEnrichedControls
      syncTopicTokens();
      renderLastUpdated();
      render();
      hideSplash();
      maybeShowInstallHint();
    } catch (err) {
      clearInterval(loadingTimer);
      hideSplash();
      document.getElementById("events").innerHTML =
        `<p class="status">the window is fogged up — events wouldn't load (${err.message})</p>`;
    }
  }

  init();
})();
