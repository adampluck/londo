/* psyconnect weekly email — takes a sign-up from the site's modal
   (web/signup.js) and hands it to Kit, which owns the list and sends the
   weekly emails. Single opt-in: the form's own confirmation email is off
   in Kit, and the sign-up on the site is the consent.

   The Kit API key can't live in a static page, so this Worker holds it.
   Same gates as psyconnect-join, cheapest first: origin, rate limit,
   honeypot, dwell, then Turnstile (the only check that leaves the
   datacentre before Kit does).

   Kit steps, all keyed by email address:
   1. create the subscriber as `active` (an upsert — an existing
      subscriber keeps their state, so someone who unsubscribed isn't
      quietly put back), recording the sign-up page;
   2. add them to the form, which records where they came from;
   3. tag them with the page's interests (tags are created on first use). */

const SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const KIT = "https://api.kit.com/v4";
const MIN_DWELL_MS = 1500;
const MAX_TOKEN_AGE_MS = 5 * 60 * 1000;
const MAX_TAGS = 4;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
// bare topic slugs, or practice:/host:/venue: plus a page slug
const TAG = /^(?:[a-z0-9-]{1,40}|(?:practice|host|venue):[a-z0-9-]{1,60})$/;
// One edition per subscriber decides which weekly version they get (see
// scripts/weekly_email.py): the page's first topic, the one it's mostly about.
const EDITIONS = new Set(["psychedelics", "consciousness", "spirituality", "connection", "healing"]);
const KINDS = new Set(["home", "event", "practice", "area", "topic", "organizer", "venue", "tonight"]);

export default {
  async fetch(request, env) {
    const origin = env.ALLOWED_ORIGIN || "https://psyconnect.london";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(origin) });
    }
    if (request.method !== "POST") return fail(origin, 405, "method");
    if (request.headers.get("Origin") !== origin) return fail(origin, 403, "origin");

    return handle(request, env, origin);
  },
};

async function handle(request, env, origin) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (env.SUBSCRIBE_RL) {
    const { success } = await env.SUBSCRIBE_RL.limit({ key: ip });
    if (!success) return fail(origin, 429, "rate");
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return fail(origin, 400, "body");
  }

  if (body.website) return fail(origin, 400, "bot");
  if (!(Number(body.dwellMs) >= MIN_DWELL_MS)) return fail(origin, 400, "fast");

  const email = String(body.email || "").trim().toLowerCase();
  if (email.length > 254 || !EMAIL.test(email)) return fail(origin, 400, "email");

  const kind = KINDS.has(body.kind) ? body.kind : "home";
  const page = String(body.page || "/").slice(0, 200);
  const tags = [...new Set((Array.isArray(body.tags) ? body.tags : [])
    .map(String).filter((t) => TAG.test(t)))].slice(0, MAX_TAGS);

  if (!env.TURNSTILE_SECRET || !env.KIT_API_KEY || !env.KIT_FORM_ID) {
    return fail(origin, 500, "config");
  }

  const token = String(body.token || "");
  if (!token) return fail(origin, 400, "token");
  const form = new FormData();
  form.append("secret", env.TURNSTILE_SECRET);
  form.append("response", token);
  if (ip) form.append("remoteip", ip);
  let verdict;
  try {
    const res = await fetch(SITEVERIFY, { method: "POST", body: form });
    verdict = await res.json();
  } catch {
    return fail(origin, 502, "verify");
  }
  if (!verdict.success) return fail(origin, 403, "token");
  const issued = Date.parse(verdict.challenge_ts || "");
  if (!Number.isFinite(issued) || Date.now() - issued > MAX_TOKEN_AGE_MS) {
    return fail(origin, 403, "stale");
  }

  const kit = (path, init = {}) =>
    fetch(KIT + path, {
      ...init,
      headers: {
        "X-Kit-Api-Key": env.KIT_API_KEY,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
    });

  try {
    // 1. the subscriber, active from the start (Kit only sets the state
    // when it creates them).
    // A 422 is Kit refusing a parameter: the address itself, or (if the
    // custom fields haven't been created in Kit yet) the fields — in which
    // case keep the sign-up and drop the fields.
    const create = (fields) => kit("/subscribers", {
      method: "POST",
      body: JSON.stringify({ email_address: email, state: "active", ...fields }),
    });
    let created = await create({ fields: { signup_page: page, signup_kind: kind } });
    if (created.status === 422) {
      const why = JSON.stringify(await created.json().catch(() => ({})));
      if (/email/i.test(why)) return fail(origin, 400, "email");
      console.error("kit fields", why);
      created = await create({});
      if (created.status === 422) return fail(origin, 400, "email");
    }
    if (!created.ok) throw new Error(`subscriber ${created.status}`);

    // 2. the form, for its record of the sign-up (double opt-in is off)
    const joined = await kit(`/forms/${env.KIT_FORM_ID}/subscribers`, {
      method: "POST",
      body: JSON.stringify({
        email_address: email,
        referrer: origin + page,
      }),
    });
    if (!joined.ok) throw new Error(`form ${joined.status}`);

    // 3. interests — best effort: a tag hiccup shouldn't lose the sign-up
    const edition = tags.find((t) => EDITIONS.has(t));
    const names = edition ? [...tags, `edition:${edition}`] : tags;
    if (names.length) {
      const ids = await tagIds(kit, names);
      await Promise.all(ids.map((id) =>
        kit(`/tags/${id}/subscribers`, {
          method: "POST",
          body: JSON.stringify({ email_address: email }),
        }).catch(() => null)
      ));
    }
  } catch (err) {
    console.error("kit", String(err));
    return fail(origin, 502, "kit");
  }

  return json(origin, 200, { ok: true });
}

// Tag name -> id, creating any that don't exist yet. One list call per
// request: a few dozen tags at most, and sign-ups are rare.
async function tagIds(kit, names) {
  const known = new Map();
  try {
    const res = await kit("/tags?per_page=1000");
    if (res.ok) {
      for (const t of (await res.json()).tags || []) known.set(t.name, t.id);
    }
  } catch {}
  const ids = [];
  for (const name of names) {
    if (known.has(name)) {
      ids.push(known.get(name));
      continue;
    }
    try {
      const res = await kit("/tags", { method: "POST", body: JSON.stringify({ name }) });
      if (res.ok) {
        const { tag } = await res.json();
        if (tag && tag.id) ids.push(tag.id);
      }
    } catch {}
  }
  return ids;
}

function cors(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

function json(origin, status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...cors(origin), "Content-Type": "application/json" },
  });
}

function fail(origin, status, code) {
  return json(origin, status, { error: code });
}
