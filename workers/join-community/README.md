# psyconnect-join

Verifies a Cloudflare Turnstile token (plus passphrase, honeypot, dwell time and
a per-IP rate limit) and, only if everything passes, returns the WhatsApp
community invite URL. Backs `https://psyconnect.london/join-community`.

This Worker exists because psyconnect is a static GitHub Pages site: Turnstile
can only be verified server-side, and the invite URL must never be in the page.

Deployed by hand, not by CI — that keeps the secrets out of GitHub entirely.

## Deploy

```sh
cd workers/join-community
npx wrangler login          # once
npx wrangler deploy
```

Then set the secrets (once, or whenever they change):

```sh
npx wrangler secret put TURNSTILE_SECRET
npx wrangler secret put WHATSAPP_INVITE_URL
npx wrangler secret put JOIN_PASSPHRASE     # optional — see below
```

`wrangler deploy` prints the URL (`https://psyconnect-join.<subdomain>.workers.dev`).
Paste it into `sites/psyconnect/config.js` as `SITE.joinCommunity.workerUrl`,
alongside the Turnstile **site** key, and push — the page builds itself.

## Rotating the invite link

Cloudflare dashboard → Workers & Pages → `psyconnect-join` → Settings →
Variables and Secrets → edit `WHATSAPP_INVITE_URL` → Save. Live immediately; no
commit, no rebuild, no deploy. This is the only place the real URL exists.

## The passphrase

`JOIN_PASSPHRASE` is optional. Unset (or deleted) and the check is skipped
entirely — the page then shows no passphrase field, provided
`joinCommunity.passphraseHint` is also cleared in `config.js`.

Matching is trimmed and case-insensitive. Set the hint in `config.js` to tell
people where to find the word ("the word from tonight's event page").

## Local testing

```sh
npx wrangler dev
```

Cloudflare's [test keys](https://developers.cloudflare.com/turnstile/troubleshooting/testing/)
let you exercise both paths without a browser:

| Secret key | Result |
| --- | --- |
| `1x0000000000000000000000000000000AA` | always passes |
| `2x0000000000000000000000000000000AA` | always fails |

`.dev.vars` (gitignored) holds local values:

```
TURNSTILE_SECRET=1x0000000000000000000000000000000AA
WHATSAPP_INVITE_URL=https://chat.whatsapp.com/EXAMPLE
JOIN_PASSPHRASE=testword
```

Note the always-passes secret returns a `challenge_ts` of the current time, so
the freshness check passes too. The rate-limit binding is a no-op under
`wrangler dev` — it only enforces once deployed.
