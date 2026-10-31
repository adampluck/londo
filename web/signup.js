/* Weekly-email sign-up modal.

   Built entirely in the browser, after the page has settled: the static
   pages carry nothing but this script tag, so titles, copy, JSON-LD and
   the sitemap are untouched. Loaded only where a site asks for it — the
   static pages get the tag from build_site.py (with the page's kind, name
   and interest tags as data-* attributes); the SPA shell has its own tag.

   When it shows:
   - never on the page a visitor lands on from outside — Google penalises
     pop-ups that cover content straight after a search click. Either they
     navigate within the site, or they stay 45s and scroll halfway;
   - never again within 30 days of a dismissal, and never after a sign-up;
   - never to visitors arriving from the email itself.
   #signup in the URL forces it (for previewing), and anything marked
   data-signup-open (the header's envelope, the footer link) opens it on a
   click, whatever the rules above say.

   Interests are never asked for: the tags come from the page, and the
   worker files the subscriber under them in Kit. */
(() => {
  const me = document.currentScript;
  const SITE = (window.LONDO_CONFIG && window.LONDO_CONFIG.SITE) || {};
  const conf = SITE.signup || {};
  const d = me ? me.dataset : {};
  const endpoint = d.endpoint || conf.workerUrl || "";
  const sitekey = d.sitekey || conf.turnstileSiteKey || "";
  const forced = location.hash === "#signup";
  const KEY = "pc-signup";
  const SNOOZE_MS = 30 * 864e5;
  const DWELL_MS = 45e3;

  // ---- what this page is about -------------------------------------------
  const params = new URLSearchParams(location.search);
  let kind = d.kind || "home";
  let name = d.name || "";
  let tags = (d.tags || "").split(",").filter(Boolean);
  if (kind === "home" && params.get("topic")) {
    // the SPA's topic landings (?topic=psychedelics)
    kind = "topic";
    name = params.get("topic");
  }
  if (kind === "topic" && !tags.length && name) tags = [topicSlug(name)];

  function topicSlug(t) {
    return String(t).split(/[\s&]/)[0].toLowerCase();
  }

  const TOPIC_TITLES = {
    psychedelics: "Where London’s psychedelic community meets",
    consciousness: "Where London’s consciousness explorers meet",
    spirituality: "London’s ceremonies & rituals, every week",
    connection: "Where London comes to connect",
    healing: "London’s healing gatherings, every week",
  };

  function copy() {
    switch (kind) {
      case "event":
        return ["Liked this? There’s plenty more",
          `Gatherings like ${name || "this one"}, in your inbox every week.`];
      case "practice":
      case "area":
        return [`Your weekly ${name} fix`,
          `London’s ${name} and kindred gatherings, in one weekly email.`];
      case "topic":
        return [TOPIC_TITLES[tags[0]] || "Your week of gatherings, picked for you",
          "Talks, circles and gatherings, picked for you every week."];
      case "organizer":
        return [`More nights like ${name}’s`,
          "Kindred hosts and gatherings across London, every week."];
      case "venue":
        return [`More nights like ${name}`,
          "London’s best-kept gatherings, picked for you every week."];
      case "tonight":
        return ["Don’t find out on the night",
          "Get the week ahead in your inbox, picked for you."];
      default:
        return ["Find your people, every week",
          "London’s ceremonies, circles and dance floors, picked for you."];
    }
  }

  // The callout's copy: calmer than the modal's, since it's found at the
  // foot of the page rather than put in front of anyone
  function calloutCopy() {
    switch (kind) {
      case "event":
        return ["Never miss the next one",
          "One email every Thursday with the week’s gatherings across London, picked for you."];
      case "practice":
      case "area":
        return [`The week’s ${name}, every Thursday`,
          `All of London’s ${name}, plus a few kindred gatherings, in one weekly email.`];
      case "organizer":
      case "venue":
        return ["More nights like these, every Thursday",
          "One email with the week’s gatherings across London, picked for you."];
      case "tonight":
        return ["Plan the week, not just tonight",
          "One email every Thursday with the week’s gatherings across London, picked for you."];
      default:
        return ["The week ahead, every Thursday",
          "Ceremonies, circles, talks and dance floors across London, in one weekly email."];
    }
  }

  // ---- whether to show at all ----------------------------------------------
  function store(v) {
    try {
      if (v === undefined) return JSON.parse(localStorage.getItem(KEY) || "{}");
      localStorage.setItem(KEY, JSON.stringify(v));
    } catch {
      return {};
    }
  }

  if (!endpoint && !forced) return;

  // The header's envelope and the footer link open it on a click, whatever
  // the automatic rules below say: someone who closed it on reflex can
  // still find their way back.
  document.addEventListener("click", (e) => {
    const t = e.target.closest && e.target.closest("[data-signup-open]");
    if (!t) return;
    e.preventDefault();
    show(t.dataset.signupOpen || "link");
  });

  // Where the callout goes: under the event itself on an event page; on a
  // listing, after the second day of events (or the last, when there are
  // fewer), so it's met while browsing rather than at the foot of the page.
  function calloutSpot() {
    const event = document.querySelector(".static-event");
    if (event) return [event, "after"];
    const days = document.querySelectorAll(".day-group:not(.static-related)");
    if (days.length) return [days[days.length > 2 ? 1 : days.length - 1], "after"];
    const explore = document.querySelector(".static-explore");
    return explore ? [explore, "before"] : [null];
  }

  // The callout on event and listing pages: always there (it's found, not
  // pushed), except for subscribers.
  function callout() {
    const [at, where] = calloutSpot();
    if (!at || store().joined || document.querySelector(".pcs-callout")) return;
    const [title, body] = calloutCopy();
    const box = document.createElement("section");
    box.className = "pcs-callout";
    box.setAttribute("aria-labelledby", "pcs-callout-title");
    const h = document.createElement("h2");
    h.id = "pcs-callout-title";
    h.textContent = title;
    const p = document.createElement("p");
    p.className = "pcs-callout-body";
    p.textContent = body;
    const go = document.createElement("button");
    go.type = "button";
    go.className = "pcs-callout-go";
    go.dataset.signupOpen = "callout";
    go.textContent = "Get the weekly email";
    const fine = document.createElement("p");
    fine.className = "pcs-callout-fine";
    fine.textContent = "Free · unsubscribe in one click";
    box.append(h, p, go, fine);
    at[where](box);
  }
  if (endpoint) {
    if (document.readyState === "loading") addEventListener("DOMContentLoaded", callout, { once: true });
    else callout();
  }

  function autoAllowed() {
    if (forced) return true;
    // crawlers and test robots never see it, even if they sit out the timer
    if (navigator.webdriver ||
        /bot|crawl|spider|slurp|lighthouse|inspection|headless/i.test(navigator.userAgent)) return false;
    const s = store();
    if (s.joined) return false;
    if (s.dismissed && Date.now() - s.dismissed < SNOOZE_MS) return false;
    if (/email/i.test(params.get("utm_source") || "") ||
        /email/i.test(params.get("utm_medium") || "")) return false;
    return true;
  }
  const auto = autoAllowed();

  let internal = false;
  try {
    internal = !!document.referrer &&
      new URL(document.referrer).host === location.host;
  } catch {}

  // ---- trigger ---------------------------------------------------------------
  const start = Date.now();
  let scrolled = false;
  let shown = false;

  // Only a person's own scroll counts. Renderers like Google's lay the page
  // out in a very tall window instead of scrolling, so "the page is short"
  // must never pass for "the page was read" — a page too short to scroll
  // only gets the modal on the next page view.
  function onScroll(e) {
    if (scrolled || !e.isTrusted || scrollY < 1) return;
    const room = document.documentElement.scrollHeight - innerHeight;
    if (room > 0 && scrollY / room >= 0.5) {
      scrolled = true;
      maybe();
    }
  }

  function maybe() {
    if (shown || !auto) return;
    if (forced || internal || (scrolled && Date.now() - start >= DWELL_MS)) {
      whenIdle(show);
    }
  }

  function whenIdle(fn) {
    const go = () => setTimeout(fn, forced ? 0 : 1200);
    if (document.readyState === "complete") go();
    else addEventListener("load", go, { once: true });
  }

  if (auto) {
    addEventListener("scroll", onScroll, { passive: true });
    setTimeout(maybe, DWELL_MS);
    maybe();
  }

  // ---- the dialog -----------------------------------------------------------
  let root, lastFocus, opened, token = "", tokenWait;

  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") n.className = v;
      else n.setAttribute(k, v);
    }
    n.append(...kids);
    return n;
  }

  function closeButton() {
    const x = el("button", { class: "pcs-x", type: "button", "aria-label": "Close", "data-close": "" });
    x.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l10 10M13 3L3 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>';
    return x;
  }

  function busyElsewhere() {
    // don't stack on top of the app's own dialogs or an open menu
    return !!document.querySelector(
      'dialog[open], [aria-modal="true"]:not(.pcs)'
    );
  }

  // GoatCounter events, one path per step and page type, so its dashboard
  // shows the funnel: signup/shown/event → signup/joined/event
  function track(step) {
    const gc = window.goatcounter;
    if (!gc || !gc.count) return;
    try {
      gc.count({ path: `signup/${step}/${kind}`, title: `Sign-up ${step}: ${kind}`, event: true });
    } catch {}
  }

  // how: undefined for the automatic trigger, else what was clicked
  function show(how) {
    if (root) return;
    if (!how && shown) return;
    if (!how && !forced && busyElsewhere()) {
      setTimeout(show, 5000);
      return;
    }
    shown = true;
    track(how ? `shown-${how}` : forced ? "shown-link" : "shown");
    // a fresh Turnstile widget for this card (a token is good for one go)
    token = "";
    tokenWait = null;
    lastFocus = document.activeElement;
    opened = Date.now();
    const [title, body] = copy();

    const email = el("input", {
      class: "pcs-email", id: "pcs-email", type: "email", inputmode: "email",
      autocomplete: "email", placeholder: "your email", required: "",
    });
    const go = el("button", { class: "pcs-go", type: "submit" }, "Count me in");
    const err = el("p", { class: "pcs-err", id: "pcs-err", role: "alert" });
    const privacy = el("a", { href: (d.privacy || "privacy/") }, "Privacy");
    const form = el("form", { novalidate: "" },
      el("div", { class: "pcs-row" },
        el("label", { for: "pcs-email", class: "pcs-sr" }, "Email address"),
        email, go),
      err,
      // honeypot: hidden from people, irresistible to form-filling bots
      el("input", { class: "pcs-hp", name: "website", tabindex: "-1", autocomplete: "off", "aria-hidden": "true" }),
      el("div", { class: "pcs-ts" }),
      el("p", { class: "pcs-fine" }, "Free · weekly · unsubscribe anytime · ", privacy));

    const card = el("div", { class: "pcs", tabindex: "-1", role: "dialog", "aria-modal": "true", "aria-labelledby": "pcs-title" },
      closeButton(),
      el("h2", { class: "pcs-title", id: "pcs-title" }, title),
      el("p", { class: "pcs-body" }, body),
      form,
      el("button", { class: "pcs-no", type: "button", "data-close": "" }, "No thanks"));

    root = el("div", { class: "pcs-root" }, el("div", { class: "pcs-scrim", "data-close": "" }), card);
    document.body.append(root);
    requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add("open")));
    // phones: focus the sheet, not the field, so the keyboard doesn't jump
    // up over it before anyone has asked for it
    const narrow = matchMedia("(max-width: 560px)").matches;
    setTimeout(() => (narrow ? card : email).focus({ preventScroll: true }), 50);

    root.addEventListener("click", (e) => {
      if (e.target.closest("[data-close]")) dismiss();
    });
    root.addEventListener("keydown", trap);
    form.addEventListener("submit", (e) => submit(e, form, email, go, err, card));
    email.addEventListener("focus", turnstile, { once: true });
    swipeToClose(card);
  }

  function trap(e) {
    if (e.key === "Escape") return dismiss();
    if (e.key !== "Tab") return;
    const f = [...root.querySelectorAll("button, input:not(.pcs-hp), a[href]")]
      .filter((n) => n.offsetParent !== null);
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  }

  function swipeToClose(card) {
    let y0 = null;
    card.addEventListener("touchstart", (e) => {
      y0 = card.scrollTop === 0 ? e.touches[0].clientY : null;
    }, { passive: true });
    card.addEventListener("touchend", (e) => {
      if (y0 !== null && e.changedTouches[0].clientY - y0 > 80) dismiss();
      y0 = null;
    }, { passive: true });
  }

  function close() {
    if (!root) return;
    root.classList.remove("open");
    const r = root;
    root = null;
    setTimeout(() => r.remove(), 300);
    if (lastFocus && lastFocus.focus) lastFocus.focus({ preventScroll: true });
  }

  function dismiss() {
    const s = store();
    if (!s.joined) {
      store({ ...s, dismissed: Date.now() });
      track("dismissed");
    }
    close();
  }

  // Turnstile loads only once the email field has focus — nothing
  // third-party touches a page that is merely being read.
  function turnstile() {
    if (!sitekey || tokenWait) return;
    tokenWait = new Promise((resolve) => {
      const render = () => {
        const box = root && root.querySelector(".pcs-ts");
        if (!box) return resolve("");
        window.turnstile.render(box, {
          sitekey,
          appearance: "interaction-only",
          callback: (t) => { token = t; resolve(t); },
          "error-callback": () => resolve(""),
        });
      };
      if (window.turnstile) return render();  // reopened: script's here
      window.__pcsTs = render;
      const s = document.createElement("script");
      s.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?onload=__pcsTs&render=explicit";
      s.async = true;
      s.onerror = () => resolve("");
      document.head.append(s);
    });
  }

  async function submit(e, form, email, go, err, card) {
    e.preventDefault();
    const value = email.value.trim();
    const say = (msg) => {
      err.textContent = msg;
      email.setAttribute("aria-invalid", msg ? "true" : "false");
      if (msg) email.setAttribute("aria-describedby", "pcs-err");
    };
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(value)) {
      say("Check that email for a typo?");
      email.focus();
      return;
    }
    say("");
    go.disabled = true;
    go.textContent = "Sending…";
    turnstile();
    if (!token && tokenWait) {
      await Promise.race([tokenWait, new Promise((r) => setTimeout(r, 10000))]);
    }
    let ok = false, code = "";
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: value,
          kind,
          page: location.pathname,
          tags,
          token,
          website: form.querySelector(".pcs-hp").value,
          dwellMs: Date.now() - opened,
        }),
      });
      ok = res.ok;
      if (!ok) code = ((await res.json().catch(() => ({}))).error) || "";
    } catch {}
    if (ok) {
      store({ joined: Date.now() });
      track("joined");
      document.querySelectorAll(".pcs-callout").forEach((n) => n.remove());
      done(card);
      return;
    }
    track(code === "email" ? "error-email" : "error");
    go.disabled = false;
    go.textContent = "Count me in";
    say(code === "email"
      ? "Check that email for a typo?"
      : "That didn’t go through. Please try again in a moment.");
  }

  function done(card) {
    card.replaceChildren(
      closeButton(),
      el("div", { class: "pcs-done-mark", "aria-hidden": "true" }, "✓"),
      el("h2", { class: "pcs-title", id: "pcs-title" }, "You’re in"),
      el("p", { class: "pcs-body" }, "Your first email arrives on Thursday morning."));
    card.querySelector(".pcs-x").focus();
  }
})();
