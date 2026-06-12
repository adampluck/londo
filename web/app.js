(function () {
  "use strict";

  const { SUPABASE_URL, SUPABASE_ANON_KEY } = window.LONDO_CONFIG;

  const state = {
    events: [],
    view: "browse", // browse | tonight | map
    category: "all",
    area: "all",
    day: "all", // "all" or a London YYYY-MM-DD
    freeOnly: false,
    query: "",
    geo: null, // {lat, lng} once granted
    surprise: null, // event shown by "surprise me"
    map: null,
    mapLoaded: false,
  };

  const SOURCE_LABELS = {
    dandelion: "Dandelion",
    luma: "Luma",
    newspeak: "Newspeak House",
    numinity: "Numinity",
    eventbrite: "Eventbrite",
    other: "elsewhere",
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
  const PINNED_TAGS = [{ tag: "psychedelics", before: "dance" }];

  // Deterministic placeholder gradient for events without an image.
  const GRADIENTS = [
    ["#4f46e5", "#9333ea"], ["#0891b2", "#2563eb"], ["#059669", "#0d9488"],
    ["#d97706", "#dc2626"], ["#db2777", "#9333ea"], ["#475569", "#1e293b"],
  ];

  const PICK_THRESHOLD = 75; // quality_score at or above ⇒ "✦ pick"
  const WEEK_STRIP_DAYS = 14;

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

  function baseFilter(e) {
    if (state.category !== "all" && e.category !== state.category) return false;
    if (state.area !== "all" && e.area !== state.area) return false;
    if (state.freeOnly && !e.is_free) return false;
    const q = state.query.trim().toLowerCase();
    if (q && !matchesQuery(e, q)) return false;
    return true;
  }

  function browseEvents() {
    return state.events.filter((e) => {
      if (!baseFilter(e)) return false;
      if (state.day !== "all" && londonDate(e.start_at) !== state.day)
        return false;
      return true;
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
    const horizon = Date.now() + 30 * 864e5;
    return state.events.filter(
      (e) =>
        baseFilter(e) &&
        e.latitude != null &&
        e.longitude != null &&
        new Date(e.start_at).getTime() <= horizon
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
      (e.traits || []).join(" "),
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

  // ---------- controls ----------

  function renderWeekStrip() {
    const strip = document.getElementById("week-strip");
    const chips = [chipEl("all days", "all")];
    const now = new Date();
    for (let i = 0; i < WEEK_STRIP_DAYS; i++) {
      const d = new Date(now.getTime() + i * 864e5);
      const key = londonDate(d);
      let label;
      if (i === 0) label = "today";
      else if (i === 1) label = "tmrw";
      else
        label = d
          .toLocaleDateString("en-GB", {
            weekday: "short",
            day: "numeric",
            timeZone: "Europe/London",
          })
          .toLowerCase();
      chips.push(chipEl(label, key));
    }
    strip.replaceChildren(...chips);

    function chipEl(label, key) {
      const btn = document.createElement("button");
      btn.className = "chip day-chip" + (key === state.day ? " active" : "");
      btn.dataset.day = key;
      btn.textContent = label;
      btn.addEventListener("click", () => {
        state.day = key;
        state.surprise = null;
        strip
          .querySelectorAll(".day-chip")
          .forEach((c) => c.classList.toggle("active", c === btn));
        render();
      });
      return btn;
    }
  }

  function maybeShowEnrichedControls() {
    // These rows only make sense once the pipeline has classified events.
    const withCategory = state.events.filter((e) => e.category).length;
    if (withCategory >= 5)
      document.getElementById("category-pills").hidden = false;
    const withArea = state.events.filter((e) => e.area).length;
    if (withArea >= 5) document.getElementById("area-chips").hidden = false;
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

  function render() {
    const controls = document.getElementById("controls");
    const main = document.getElementById("events");
    const mapView = document.getElementById("map-view");

    controls.hidden = state.view === "map";
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

  function renderBrowse(container) {
    const events = browseEvents();
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

  function renderTonight(container) {
    requestGeo();
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
    dice.className = "pill surprise";
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
      back.className = "pill show-all";
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
        "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
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
          `<a href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener">open ↗</a>`
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

  // ---------- geolocation ----------

  function requestGeo() {
    if (state.geo !== null || !("geolocation" in navigator)) return;
    state.geo = false; // only ask once
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        state.geo = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        if (state.view === "tonight") render();
      },
      () => {},
      { maximumAge: 600000, timeout: 8000 }
    );
  }

  function distanceKm(e) {
    if (!state.geo || e.latitude == null || e.longitude == null) return null;
    const R = 6371;
    const dLat = ((e.latitude - state.geo.lat) * Math.PI) / 180;
    const dLng = ((e.longitude - state.geo.lng) * Math.PI) / 180;
    const a =
      Math.sin(dLat / 2) ** 2 +
      Math.cos((state.geo.lat * Math.PI) / 180) *
        Math.cos((e.latitude * Math.PI) / 180) *
        Math.sin(dLng / 2) ** 2;
    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
  }

  // ---------- cards ----------

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

    const badge = document.createElement("span");
    badge.className = `badge badge-src badge-${e.source}`;
    badge.textContent = SOURCE_LABELS[e.source] || e.source;
    banner.appendChild(badge);

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
      let where = e.venue_name || e.address;
      const km = distanceKm(e);
      if (km != null)
        where += ` · ${km < 10 ? km.toFixed(1) : Math.round(km)} km away`;
      venue.textContent = where;
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
    span.textContent = (e.title || "?").trim().charAt(0).toUpperCase();
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

  // ---------- submissions ----------

  function bindSubmitBox() {
    const form = document.getElementById("submit-form");
    const note = document.getElementById("submit-note");
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = document.getElementById("submit-url");
      const url = input.value.trim();
      if (!url) return;
      note.hidden = false;
      note.textContent = "sending…";
      try {
        const res = await fetch(`${SUPABASE_URL}/rest/v1/submissions`, {
          method: "POST",
          headers: {
            apikey: SUPABASE_ANON_KEY,
            Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
            "Content-Type": "application/json",
            Prefer: "return=minimal",
          },
          body: JSON.stringify({ url }),
        });
        if (res.status === 409) {
          input.value = "";
          note.textContent = "someone already sent that one — it's in the queue.";
          return;
        }
        if (!res.ok) throw new Error(String(res.status));
        input.value = "";
        note.textContent =
          "thank you — if it checks out, it appears after the next sweep.";
      } catch {
        note.textContent = "that didn't go through — try again in a bit.";
      }
    });
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
        title: "londo — " + view,
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
      syncTagHighlight();
      render();
    });

    document.getElementById("view-tabs").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-view]");
      if (!btn) return;
      state.view = btn.dataset.view;
      state.surprise = null;
      document
        .querySelectorAll("#view-tabs .view-tab")
        .forEach((t) => t.classList.toggle("active", t === btn));
      countView(state.view);
      render();
    });

    document.getElementById("category-pills").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-category]");
      if (!btn) return;
      state.category = btn.dataset.category;
      state.surprise = null;
      document
        .querySelectorAll("#category-pills .pill")
        .forEach((p) => p.classList.toggle("active", p === btn));
      render();
    });

    document.getElementById("area-chips").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-area]");
      if (!btn) return;
      state.area = btn.dataset.area;
      state.surprise = null;
      document
        .querySelectorAll("#area-chips .chip")
        .forEach((c) => c.classList.toggle("active", c === btn));
      render();
    });

    document.getElementById("free-toggle").addEventListener("click", (ev) => {
      state.freeOnly = !state.freeOnly;
      ev.target.classList.toggle("active", state.freeOnly);
      render();
    });
  }

  async function init() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("sw.js").catch(() => {});
    }
    applyTimeTheme();
    initAnalytics();
    bindControls();
    bindSubmitBox();
    renderWeekStrip();
    if (SUPABASE_URL.startsWith("YOUR_")) {
      document.getElementById("events").innerHTML =
        '<p class="status">set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js</p>';
      return;
    }
    try {
      state.events = await fetchEvents();
      maybeShowEnrichedControls();
      renderTagCloud();
      renderLastUpdated();
      render();
    } catch (err) {
      document.getElementById("events").innerHTML =
        `<p class="status">the window is fogged up — events wouldn't load (${err.message})</p>`;
    }
  }

  init();
})();
