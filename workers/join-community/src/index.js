/* psyconnect.london/join-community — reveals the WhatsApp invite link only
   after a visitor clears every gate.

   The page itself is static (GitHub Pages) and must never contain the invite
   URL, or the gate is decorative: the URL lives here, as a Worker secret, and
   is written into a response only at the very end of handle().

   Checks run cheapest-first so a crawler costs us nothing. Order matters:
   the Turnstile siteverify round-trip is last because it's the only one that
   leaves the datacentre. */

const SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify";
const MIN_DWELL_MS = 2000;
const MAX_TOKEN_AGE_MS = 5 * 60 * 1000;

export default {
  async fetch(request, env) {
    const origin = env.ALLOWED_ORIGIN || "https://psyconnect.london";

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(origin) });
    }
    if (request.method !== "POST") {
      return fail(origin, 405, "method");
    }
    // Named origin only, never "*": a token minted for our sitekey is useless
    // to a page on another host that can't read the response.
    if (request.headers.get("Origin") !== origin) {
      return fail(origin, 403, "origin");
    }

    return handle(request, env, origin);
  },
};

async function handle(request, env, origin) {
  const ip = request.headers.get("CF-Connecting-IP") || "";
  if (env.JOIN_RL) {
    const { success } = await env.JOIN_RL.limit({ key: ip });
    if (!success) return fail(origin, 429, "rate");
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return fail(origin, 400, "body");
  }

  // Honeypot: a hidden input real people never see, let alone fill in.
  if (body.website) return fail(origin, 400, "bot");

  // Dwell is client-reported and so forgeable — it only catches automation
  // naive enough to post instantly. The unforgeable freshness check is
  // challenge_ts below.
  if (!(Number(body.dwellMs) >= MIN_DWELL_MS)) return fail(origin, 400, "fast");

  if (env.JOIN_PASSPHRASE) {
    const given = String(body.passphrase || "").trim().toLowerCase();
    if (given !== env.JOIN_PASSPHRASE.trim().toLowerCase()) {
      return fail(origin, 403, "passphrase");
    }
  }

  const token = String(body.token || "");
  if (!token) return fail(origin, 400, "token");

  // A missing secret would make siteverify reject every token, which reads to
  // the visitor as "you failed the human check" — say "misconfigured" instead.
  if (!env.TURNSTILE_SECRET || !env.WHATSAPP_INVITE_URL) {
    return fail(origin, 500, "config");
  }

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

  // Tokens are single-use and expire after 300s, but check anyway: a replayed
  // or hoarded token shouldn't buy anything.
  const issued = Date.parse(verdict.challenge_ts || "");
  if (!Number.isFinite(issued) || Date.now() - issued > MAX_TOKEN_AGE_MS) {
    return fail(origin, 403, "stale");
  }

  return json(origin, 200, { url: env.WHATSAPP_INVITE_URL });
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

// Failures carry a short code for the UI's wording and nothing else — no echo
// of the input, no hint about which secret was wrong.
function fail(origin, status, code) {
  return json(origin, status, { error: code });
}
