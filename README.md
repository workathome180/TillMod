# FileMod — PayPal CSV → QuickBooks Online CSV

v1: a working converter with real credit tracking, Stripe payments, and
email verification wired in — one free trial conversion per email, then
pay-per-use, a 10-pack, or an unlimited monthly subscription.

## What's here

- `transform.py` — the actual conversion logic. Pure Python, no dependencies
  beyond the standard library. This is the core IP; everything else is
  plumbing around it.
- `db.py` — SQLite-backed user/credit tracking. Email-only accounts (no
  passwords) — enough to track balances and subscription status, not a full
  auth system.
- `app.py` — Flask web app: upload UI, `/request-code` + `/verify-code`
  (email verification), `/convert` (credit-gated), `/account` (balance
  lookup), `/create-checkout-session` (Stripe Checkout), and `/webhook`
  (credits/activates subscriptions after a real payment).
- `templates/index.html` — the upload page: verify your email, then upload,
  with working Buy buttons.
- `samples/sample_paypal_export.csv` — a realistic fake PayPal export you can
  test with immediately.

## Email verification

Every identity-sensitive route (`/convert`, `/account`,
`/create-checkout-session`) requires proof that you actually control the
email you're using — not just an email string typed into a form. Without
this, anyone who knew or guessed another person's email could spend their
credits, see their balance, or (once "keep a copy" existed) have a file
planted in their archive folder under that person's name.

- `/request-code` emails (or in dev, prints to the console — see below) a
  6-digit code and returns an opaque signed "receipt" to the browser.
- `/verify-code` checks the submitted code against the receipt. On success it
  issues a signed session token (30-day expiry) that the browser stores in
  `localStorage` and sends as `Authorization: Bearer <token>` on every
  request from then on.
- Every route that touches credits, balances, or files derives "who is this"
  from that token alone — never from a client-supplied email field. See the
  `require_verified_email()` / `verified_email_required` decorator in
  `app.py`.

Required setup:

```
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
```

Without `SECRET_KEY` set, a random one is generated each time the process
starts — fine for a quick local test, but it means every restart silently
invalidates every outstanding code and logs everyone out. Never run without
an explicit `SECRET_KEY` anywhere that isn't your own machine.

To actually send codes by email, set:

```
export SMTP_HOST=... SMTP_PORT=587 SMTP_USER=... SMTP_PASSWORD=... SMTP_FROM=no-reply@filemod.app
```

Without `SMTP_HOST` set, codes print to the server console instead
(`[DEV] verification code for x@y.com: 123456`) — that's how local
development and the tests for this feature work without any real email
setup, but it means real users can't receive a real code until SMTP is
configured. Any standard SMTP provider works (SendGrid, Postmark, AWS SES's
SMTP interface, even a Gmail app password for very low volume).

## How the credit system works

- First conversion for a brand-new email is free (tracked by a persistent
  `free_trial_used` flag — not inferred from balance, so it can only ever
  fire once per account, even if credits later hit zero again).
- After that, `/convert` returns HTTP 402 if the account has no credits and
  no active subscription.
- A successful Stripe Checkout fires `checkout.session.completed`, which the
  `/webhook` route uses to add credits (single/10-pack) or activate a
  subscription (monthly), keyed by the email passed into Checkout.
- Subscriptions get a 32-day grace window past their last confirmed webhook,
  so a slightly-delayed renewal event doesn't lock out a paying customer.
  Cancellations arrive via `customer.subscription.deleted`.

This was tested end-to-end (free trial grant, exhaustion, purchased-credit
top-up, and subscription unlimited use) with Flask's test client — not just
read over. One real bug was caught and fixed in the process: the first draft
re-granted the free trial every time a balance hit zero, instead of only
once ever. Fixed by tracking trial use as a persistent flag rather than
inferring it from the current balance.

## Keeping a copy of the original file (opt-in)

By default, uploads are processed in memory and never written to disk — the
"processed and discarded" line on both pages is literally true unless a user
checks the box.

- A "Keep a copy of my original file" checkbox on the upload form is
  unchecked by default. When checked, `/convert` saves the untouched upload
  via `save_original()` in `app.py` before attempting conversion, so it's
  captured even if the conversion itself fails.
- Files land in `uploads/originals/<email>/<timestamp>__<filename>`, one
  subfolder per email — the point is that if a customer says a conversion
  came out wrong, you can go find exactly what they sent without asking them
  to dig it up and re-send it.
- `uploads/` is gitignored — these can be real PayPal transaction exports
  with real customer/financial data, and should never end up in git.
- Not web-reachable: no Flask route serves this directory (unlike Flask's
  auto-served `static/` folder), so there's no URL that reaches these files.
- Locked to the app's own OS user: both the per-email folder (0700) and each
  file (0600) are chmod'd on write, so no other local user or process on the
  same machine can read them either.
- What this does **not** cover: disk encryption. Before this holds real
  customers' data in production, put `uploads/` on an encrypted volume (most
  hosts — Render, Railway, Fly.io — encrypt at rest by default, but confirm
  it). Same underlying caveat as the SQLite note below — plain local disk,
  fine for testing, not a permanent answer.
- Retention: `cleanup_originals.py` deletes originals older than a chosen
  window (default 90 days) and prunes any per-email folder that ends up
  empty. It's a standalone script, not something the app runs on its own —
  deleting customer files should be a deliberate, visible step, not
  something silently triggered inside a web request. Run it by hand:
  ```
  python3 cleanup_originals.py --dry-run       # preview, deletes nothing
  python3 cleanup_originals.py --days 30       # actually delete, custom window
  ```
  or point a daily cron job at it in production (see the docstring at the
  top of the script for a ready-to-use crontab line). The Privacy Policy
  (`marketing/privacy.html`) says originals are "retained until you ask us
  to delete them or your account is closed" — if you start running this on
  a schedule, update that page to say so, since a fixed retention window is
  more specific than what it currently promises.

## Running it locally

```bash
pip install -r requirements.txt --break-system-packages
python app.py
```

Then open `http://localhost:5000`, drop in `samples/sample_paypal_export.csv`
(or a real PayPal export), and it'll download a QBO-ready CSV.

`python app.py` runs Flask's built-in dev server — fine for local testing,
not for production (it's single-threaded and not hardened for real traffic).

### Running it in production

The repo includes a `Procfile` for platforms that use one (Render, Railway,
Heroku-style buildpacks) plus `gunicorn` in `requirements.txt`:

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 60
```

If your host doesn't read a `Procfile`, use that same command as the start
command. Also set these environment variables before the first deploy:

- `SECRET_KEY` — required (see "Email verification" above for how to
  generate one). The app refuses to run safely without it.
- `DB_PATH` — set this to a path on **persistent** storage if your host's
  local disk is wiped on redeploy (Render, Railway, and Fly.io all do this
  by default unless you attach a volume). Otherwise credits and conversion
  history reset on every deploy. The same applies to the `uploads/`
  directory if you're using the "keep a copy of my original file" feature —
  mount it on the same persistent volume, or those files will disappear on
  redeploy too.
- Stripe and SMTP env vars as described above.

## The QBO import spec this targets

Confirmed against Intuit's own documentation:

- 3-column format: `Date, Description, Amount` (single signed amount)
- UTF-8 encoding
- Dates as `MM/DD/YYYY`
- No currency symbols, no thousands-separator commas
- No special characters in the Description field
- Max ~350KB / ~1,000–1,500 transactions per upload — the converter warns
  you if your file exceeds this, so you know to split it

## Setup to actually take payments

1. Create a Stripe account (or use your existing one, test mode first).
2. Create 3 Products/Prices: "Single Conversion" ($14 one-time), "10
   Conversion Pack" ($69 one-time), "Unlimited Monthly" ($29/mo recurring).
3. Set environment variables before running:
   ```
   export STRIPE_SECRET_KEY=sk_test_...
   export STRIPE_WEBHOOK_SECRET=whsec_...
   export STRIPE_PRICE_SINGLE=price_...
   export STRIPE_PRICE_PACK10=price_...
   export STRIPE_PRICE_MONTHLY=price_...
   ```
4. For local webhook testing: `stripe listen --forward-to localhost:5000/webhook`
   (the Stripe CLI prints the `whsec_...` value to use above).
5. In production, point a Stripe webhook endpoint at
   `https://app.filemod.app/webhook`, listening for `checkout.session.completed`,
   `customer.subscription.deleted`, and `charge.refunded`.

Without these env vars set, the app still runs fine for testing the
converter itself — `/create-checkout-session` just returns a clear 501
instead of erroring unpredictably.

## Legal pages

`marketing/privacy.html` and `marketing/terms.html` are a real Privacy
Policy and Terms of Service, styled to match the site, describing what the
app actually does: email verification, credit tracking, the opt-in
"keep a copy of my original file" feature, and Stripe for payments. The app
itself serves the same pages at `/privacy` and `/terms` (via
`static/privacy.html` and `static/terms.html` — kept as copies with
app-relative links, since the app and the marketing site deploy separately).

**Before launch:**

- Both pages currently point to `privacy@filemod.app` and
  `support@filemod.app`. These need to be real, monitored inboxes before you
  publish the pages — either set up that mailbox, or update the addresses in
  both `marketing/privacy.html`/`marketing/terms.html` and their `static/`
  copies to whatever address you'll actually check.
- These are a solid starting draft, not a substitute for a lawyer — worth a
  quick review once you're taking real payments, especially the refund and
  liability language, and to add your jurisdiction if you want a governing
  law clause.
- If the "keep a copy of my original file" feature stays in the product,
  keep the Privacy Policy in sync with however long you actually end up
  retaining those files (see the cleanup utility note below).

## Branding & the marketing site

- `branding/logo.svg` — full logo lockup (icon + wordmark), for anywhere you
  need the complete brand mark: website header, README, pitch deck.
- `branding/logo-icon.svg` — icon only, for favicons, app icons, social
  avatars, anywhere space is tight.
- `branding/favicon.ico`, `favicon-16.png`, `favicon-32.png`,
  `favicon-192.png`, `apple-touch-icon.png` — pre-generated raster favicons
  rendered from `logo-icon.svg`, already wired into both `<head>`s
  (`marketing/index.html` via `marketing/assets/`, and the app via
  `static/` + `url_for('static', ...)`). No further action needed unless you
  redesign the icon, in which case regenerate these from the new SVG.
- `marketing/` — a standalone landing page (separate from the app itself),
  meant to live at your root domain. It's self-contained: `marketing/assets/`
  has its own copies of the logo files and favicons, so you can deploy the
  `marketing/` folder as-is to any static host.

**Both logo files are SVG** — infinitely scalable, no quality loss at any
size, and easy to recolor or tweak in any vector tool (Figma, Illustrator,
even a text editor, since it's just markup).

### Deployment structure

The marketing site and the actual converter app are two separate things,
deployed separately — this keeps the marketing page fast and free to host
(no server needed for a static page) while the app runs wherever you're
running Flask:

- **Marketing site** (`marketing/` folder) → deploy to the root domain
  (`filemod.app`) via any static host: Netlify, Vercel, Cloudflare Pages,
  or even GitHub Pages. Free tier is enough for a landing page.
- **App** (`app.py` + everything else) → deploy to the subdomain
  `app.filemod.app` on Render, Railway, or Fly.io, as discussed earlier.

`marketing/index.html` already links to `https://app.filemod.app/` (nav CTA,
hero CTA, and all three pricing tiles) — no find-and-replace needed unless
you change the app subdomain.

### On the domain name itself

Decided: the app is branded **FileMod**, at `filemod.app`. (`qbconvert.com`
and the FileMod `.com`/`.io` variants were already taken by unrelated
sites — `.app` was open.) The separate storefront site for other digital
products (apps, ebooks) will use its own name/domain, not yet chosen.

### What's still not done

1. **The second conversion pair** — old POS `.iif` exports → QBO CSV. This is
   a materially harder parser (`.iif` is tab-delimited with multiple section
   types), and was flagged as the higher-pain, less-served opportunity. Build
   this once the PayPal converter has validated real demand.
2. **Deployment** — the code is production-ready (gunicorn, `Procfile`,
   configurable `DB_PATH`), but nothing is actually deployed yet. Render,
   Railway, or Fly.io are the low-maintenance hosting options discussed
   earlier. Remember to attach a persistent volume for `DB_PATH` and
   `uploads/` — see "Running it in production" above.
3. **Stripe account** — you still need to create the Stripe account itself,
   the 3 Products/Prices, and set the real `STRIPE_*` env vars. See "Setup
   to actually take payments" above; `PRICE_IDS` in `app.py` still holds
   placeholder values until then.

## Legal/ToS check: PayPal and Intuit

Researched this so it's not just a "go read 15 minutes of legal text"
placeholder anymore. Neither PayPal's User Agreement nor Intuit's App Center
terms specifically address a tool like FileMod, but the relevant pieces:

- **PayPal's User Agreement** restricts automated/robotic access to *their*
  website ("use any robot, spider, other automatic device... to monitor or
  copy our websites") and restricts copying/modifying PayPal's own branding
  assets. FileMod does neither — it never touches PayPal's site or API at
  all. The user exports their own CSV from their own PayPal account by hand
  and uploads it here; that's the same category of action as opening the
  file in Excel. No API scraping, no impersonation, no automated PayPal
  access anywhere in this codebase.
- **Intuit's App Center terms** govern apps that integrate with the
  QuickBooks Online *API* (OAuth connections, listed in the App Center).
  FileMod isn't one of those — it produces a plain CSV that a user imports
  through QuickBooks Online's own built-in file-upload feature, the same
  mechanism QBO offers for any bank or CSV import. This is a well-established,
  common category of independent tool — DocuClipper, PropperSoft, and several
  others do the same thing commercially.
- **Trademark usage** is the one place real rules exist and apply directly:
  Intuit's trademark guidelines require that "QuickBooks" (and by the same
  logic, PayPal's mark) never appear in your own product/company name or
  logo, never be used as a verb/noun/plural, and be accompanied by a
  disclaimer that you're not affiliated with or endorsed by the trademark
  owner. FileMod's name, domain, and logo don't reference either brand, and
  a "not affiliated with, endorsed by, or sponsored by PayPal or Intuit"
  disclaimer is now in the footer of both the marketing site and the app,
  plus spelled out in `marketing/terms.html`. (Also fixed a stray lowercase
  "Paypal" found in `templates/index.html` while checking this — trademark
  capitalization matters here, not just style.)

None of this is a substitute for an actual lawyer if this app starts making
real money — it's a reasonable-effort check, not legal advice.

## Validate before building further

Per the earlier plan: post this (or a link to it) in r/Bookkeeping and the
QuickBooks Community forum before investing in the `.iif` converter or the
credit-tracking system. If nobody bites on the easy version, that's a signal
worth having early.
