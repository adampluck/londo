(function () {
  "use strict";

  const { SUPABASE_URL, SUPABASE_ANON_KEY } = window.LONDO_CONFIG;

  const state = {
    events: [],
    source: "all",
    freeOnly: false,
    range: "week", // default window: next 7 days
    query: "",
  };

  const SOURCE_LABELS = {
    dandelion: "Dandelion",
    luma: "Luma",
    newspeak: "Newspeak House",
    numinity: "Numinity",
    eventbrite: "Eventbrite",
    other: "elsewhere",
  };

  // Candidate vocabulary for the "popular" tag cloud, curated from what
  // actually recurs in the data. Tags are search shortcuts, not labels:
  // only those matching enough currently-loaded events are shown.
  const TAG_VOCAB = [
    "community", "connection", "ai", "art", "healing", "meditation",
    "breathwork", "nature", "ritual", "dance", "ecstatic dance", "ceremony",
    "tech", "movement", "cacao", "embodiment", "music", "sound healing",
    "sound bath", "founders", "spiritual", "retreat", "wellbeing", "somatic",
    "networking", "running", "kundalini", "yoga", "tantra", "politics",
    "intimacy", "men's", "women's", "authentic relating", "hackathon",
    "book club", "singing", "comedy", "poetry", "climate",
  ];
  const TAG_CLOUD_SIZE = 14;
  const TAG_MIN_COUNT = 3;
  // Always-shown tags with a fixed position: {tag, before}. The tag is
  // included regardless of its count and placed before the named tag
  // (or appended if that tag isn't currently in the cloud).
  const PINNED_TAGS = [{ tag: "psychedelics", before: "dance" }];

  // Deterministic placeholder gradient for events without an image.
  const GRADIENTS = [
    ["#4f46e5", "#9333ea"], ["#0891b2", "#2563eb"], ["#059669", "#0d9488"],
    ["#d97706", "#dc2626"], ["#db2777", "#9333ea"], ["#475569", "#1e293b"],
  ];

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
    const url =
      `${SUPABASE_URL}/rest/v1/events?${params}` +
      `&start_at=gte.${today.toISOString()}` +
      `&duplicate_of=is.null` +
      `&is_online=eq.false` + // in-person only
      `&last_seen_at=gte.${staleCutoff}`;

    const res = await fetch(url, {
      headers: {
        apikey: SUPABASE_ANON_KEY,
        Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
      },
    });
    if (!res.ok) throw new Error(`Supabase request failed (${res.status})`);
    return res.json();
  }

  function applyFilters() {
    const q = state.query.trim().toLowerCase();
    const now = new Date();
    let until = null;
    if (state.range === "today") {
      until = new Date(now);
      until.setHours(23, 59, 59, 999);
    } else if (state.range === "week") {
      until = new Date(now.getTime() + 7 * 864e5);
    } else if (state.range === "month") {
      until = new Date(now.getTime() + 30 * 864e5);
    }

    return state.events.filter((e) => {
      if (state.source !== "all" && e.source !== state.source) return false;
      if (state.freeOnly && !e.is_free) return false;
      if (until && new Date(e.start_at) > until) return false;
      if (q && !matchesQuery(e, q)) return false;
      return true;
    });
  }

  function haystack(e) {
    return [
      e.title,
      e.description,
      e.venue_name,
      e.organizer_name,
      (e.tags || []).join(" "),
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
  }

  function matchesQuery(e, q) {
    const text = haystack(e);
    // short queries match whole words only ("ai" shouldn't match "air")
    if (q.length <= 3) {
      return new RegExp(`(^|[^a-z0-9])${escapeRe(q)}([^a-z0-9]|$)`, "i").test(text);
    }
    if (text.includes(q)) return true;
    // plural-insensitive: "psychedelics" should match "psychedelic"
    return q.endsWith("s") && text.includes(q.slice(0, -1));
  }

  function escapeRe(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function renderTagCloud() {
    const row = document.getElementById("tag-cloud");
    const countFor = (tag) =>
      state.events.filter((e) => matchesQuery(e, tag)).length;

    const pinned = PINNED_TAGS.map((p) => p.tag);
    const counts = TAG_VOCAB.filter((t) => !pinned.includes(t))
      .map((tag) => [tag, countFor(tag)])
      .filter(([, n]) => n >= TAG_MIN_COUNT)
      .sort((a, b) => b[1] - a[1])
      .slice(0, TAG_CLOUD_SIZE - PINNED_TAGS.length);

    for (const { tag, before } of PINNED_TAGS) {
      const at = counts.findIndex(([t]) => t === before);
      counts.splice(at === -1 ? counts.length : at, 0, [tag, countFor(tag)]);
    }

    if (!counts.length) return;
    const max = Math.max(...counts.map(([, n]) => n), 1);

    for (const [tag, n] of counts) {
      const btn = document.createElement("button");
      const size = n > max * 0.6 ? 3 : n > max * 0.25 ? 2 : 1;
      btn.className = `tag size-${size}`;
      btn.textContent = tag;
      btn.title = `${n} events`;
      btn.addEventListener("click", () => {
        const search = document.getElementById("search");
        const active = state.query.trim().toLowerCase() === tag;
        search.value = active ? "" : tag;
        state.query = search.value;
        syncTagHighlight();
        render();
      });
      row.appendChild(btn);
    }
    row.hidden = false;
  }

  function syncTagHighlight() {
    const q = state.query.trim().toLowerCase();
    document
      .querySelectorAll("#tag-cloud .tag")
      .forEach((t) => t.classList.toggle("active", t.textContent === q));
  }

  function render() {
    const container = document.getElementById("events");
    const events = applyFilters();

    if (!events.length) {
      container.innerHTML =
        '<p class="status">nothing here — the city is resting. try a wider window.</p>';
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

    const frag = document.createDocumentFragment();
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
      name.textContent = day;
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
    container.replaceChildren(frag);
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

  function card(e, index) {
    const a = document.createElement("a");
    a.className = "card";
    a.href = e.source_url;
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
        paintPlaceholder(banner, e.title);
      };
      banner.appendChild(img);
    } else {
      paintPlaceholder(banner, e.title);
    }

    const badge = document.createElement("span");
    badge.className = `badge badge-${e.source}`;
    badge.textContent = SOURCE_LABELS[e.source] || e.source;
    banner.appendChild(badge);

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
    if (meta.childNodes.length) body.appendChild(meta);

    if (e.description) {
      const blurb = document.createElement("p");
      blurb.className = "blurb";
      blurb.textContent = e.description.replace(/\s+/g, " ").slice(0, 220);
      body.appendChild(blurb);
    }

    a.append(banner, body);
    return a;
  }

  function timeOfDayClass(e) {
    const hour = Number(
      new Date(e.start_at).toLocaleTimeString("en-GB", {
        hour: "numeric",
        hour12: false,
        timeZone: "Europe/London",
      })
    );
    if (hour < 12) return "dot-morning";
    if (hour < 17) return "dot-afternoon";
    return "dot-evening";
  }

  function paintPlaceholder(banner, title) {
    const [c1, c2] = GRADIENTS[hash(title) % GRADIENTS.length];
    banner.style.background = `linear-gradient(135deg, ${c1}, ${c2})`;
    const span = document.createElement("span");
    span.className = "placeholder-initial";
    span.textContent = (title || "?").trim().charAt(0).toUpperCase();
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

  function bindControls() {
    document.getElementById("search").addEventListener("input", (ev) => {
      state.query = ev.target.value;
      syncTagHighlight();
      render();
    });

    document.getElementById("source-pills").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-source]");
      if (!btn) return;
      state.source = btn.dataset.source;
      document
        .querySelectorAll("#source-pills .pill")
        .forEach((p) => p.classList.toggle("active", p === btn));
      render();
    });

    document.getElementById("free-toggle").addEventListener("click", (ev) => {
      state.freeOnly = !state.freeOnly;
      ev.target.classList.toggle("active", state.freeOnly);
      render();
    });

    document.getElementById("range").addEventListener("change", (ev) => {
      state.range = ev.target.value;
      render();
    });
  }

  async function init() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => {});
    }
    bindControls();
    if (SUPABASE_URL.startsWith("YOUR_")) {
      document.getElementById("events").innerHTML =
        '<p class="status">set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js</p>';
      return;
    }
    try {
      state.events = await fetchEvents();
      renderTagCloud();
      render();
    } catch (err) {
      document.getElementById("events").innerHTML =
        `<p class="status">the window is fogged up — events wouldn't load (${err.message})</p>`;
    }
  }

  init();
})();
