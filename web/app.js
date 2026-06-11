(function () {
  "use strict";

  const { SUPABASE_URL, SUPABASE_ANON_KEY } = window.LONDO_CONFIG;

  const state = {
    events: [],
    source: "all",
    freeOnly: false,
    range: "all",
    query: "",
  };

  const SOURCE_LABELS = {
    dandelion: "Dandelion",
    luma: "Luma",
    newspeak: "Newspeak House",
    numinity: "Numinity",
  };

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
      if (q) {
        const haystack = [e.title, e.description, e.venue_name, e.organizer_name]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }

  function render() {
    const container = document.getElementById("events");
    const events = applyFilters();

    if (!events.length) {
      container.innerHTML = '<p class="status">No events match.</p>';
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
      const h2 = document.createElement("h2");
      h2.className = "day-heading";
      h2.textContent = day;
      frag.appendChild(h2);

      const grid = document.createElement("div");
      grid.className = "grid";
      for (const e of dayEvents) grid.appendChild(card(e));
      frag.appendChild(grid);
    }
    container.replaceChildren(frag);
  }

  function card(e) {
    const a = document.createElement("a");
    a.className = "card";
    a.href = e.source_url;
    a.target = "_blank";
    a.rel = "noopener";

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
    time.textContent = formatTime(e);
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
    const bits = [];
    if (e.is_free) bits.push("Free");
    else if (e.price_min != null) {
      bits.push(
        e.price_min === e.price_max || e.price_max == null
          ? `£${e.price_min}`
          : `£${e.price_min}–£${e.price_max}`
      );
    }
    if (e.organizer_name) bits.push(e.organizer_name);
    meta.textContent = bits.join(" · ");
    if (bits.length) body.appendChild(meta);

    a.append(banner, body);
    return a;
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
    bindControls();
    if (SUPABASE_URL.startsWith("YOUR_")) {
      document.getElementById("events").innerHTML =
        '<p class="status">Set SUPABASE_URL and SUPABASE_ANON_KEY in web/config.js</p>';
      return;
    }
    try {
      state.events = await fetchEvents();
      render();
    } catch (err) {
      document.getElementById("events").innerHTML =
        `<p class="status">Failed to load events: ${err.message}</p>`;
    }
  }

  init();
})();
