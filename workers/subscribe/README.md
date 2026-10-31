# psyconnect-subscribe

Backs the weekly-email sign-up modal (`web/signup.js`). Checks origin, rate
limit, honeypot, dwell time and Cloudflare Turnstile, then hands the email to
Kit: creates the subscriber (active: single opt-in), adds them to the form and
tags them with the interests of the page they signed up on.

Deployed by hand, not by CI, so the secrets stay out of GitHub.

## One-time Kit setup

1. **Form:** Grow → Landing Pages & Forms → create a form (any style; it's
   never shown). In its settings → Incentive, leave **"Send incentive
   email"** off: that's Kit's double opt-in, and the sign-up is single opt-in.
   The form's number is in its URL: `app.kit.com/forms/<ID>/edit`.
2. **Custom fields:** Subscribers → any subscriber → add fields named
   `signup_page` and `signup_kind` (or create them via the API). Kit may
   reject sign-ups that carry unknown fields.
3. **API key:** Settings → Developer → create a **v4** API key.
4. Tags are created automatically on first use: topic slugs (`psychedelics`,
   `consciousness`, `spirituality`, `connection`, `healing`) plus
   `practice:<slug>`, `host:<slug>` and `venue:<slug>`.

## Deploy

```sh
cd workers/subscribe
npx wrangler deploy
npx wrangler secret put TURNSTILE_SECRET   # same value as psyconnect-join
npx wrangler secret put KIT_API_KEY
npx wrangler secret put KIT_FORM_ID
```

Paste the printed URL (`https://psyconnect-subscribe.<subdomain>.workers.dev`)
into `sites/psyconnect/config.js` as `SITE.signup.workerUrl` and push. Until
that URL is set the modal never appears (except with `#signup` on the URL, for
previewing).

## Testing

Open any page with `#signup` on the end to see the modal at once. To start
fresh after dismissing or signing up, clear `pc-signup` from localStorage.

`npx wrangler dev` with a `.dev.vars` (gitignored) using Turnstile's
always-passes secret `1x0000000000000000000000000000000AA` exercises the
whole path against your real Kit account.
