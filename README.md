# CampWatch

Watch Recreation.gov campgrounds for cancellations and get an alert on your phone the moment a site opens up. You can also have it put the site in your Recreation.gov cart so it's held while you pay.

- **Watches.** Each watch covers one or more campgrounds (optionally only certain sites) and one or more sets of dates. Add as many watches as you like.
- **Stay rules.** Ask for the whole stay at one site, or for any N nights in a row within a wider window, such as "any 2 nights in July".
- **Alerts.** Push notifications through [ntfy](https://ntfy.sh) or [Pushover](https://pushover.net), or email. Each alert has a "Book" link that opens the site's page. You're alerted once per opening, not every minute.
- **Hold in cart (optional, experimental).** CampWatch signs in to your Recreation.gov account and adds the site to your cart. You then have about 15 minutes to pay. Payment is always left to you.
- **Web app and iPhone app.** One mobile-first web app. On iPhone, add it to your home screen and it opens full screen like a native app.

<p>
  <img src="docs/screenshots/watches.png" width="260" alt="Watches list showing an open site with a Book button">
  <img src="docs/screenshots/editor.png" width="260" alt="Watch editor with campground and site picker">
  <img src="docs/screenshots/activity.png" width="260" alt="Activity feed of openings">
</p>

<sub>Screenshots use made-up campgrounds.</sub>

## Quick start

You need [uv](https://docs.astral.sh/uv/) (it installs Python for you).

```bash
cp .env.example .env        # then set at least CAMPWATCH_NTFY_TOPIC (see "Alerts")
uv sync
uv run campwatch            # http://127.0.0.1:8000
```

Open http://127.0.0.1:8000, tap **New watch**, search for a campground (or paste its recreation.gov link), pick your dates, and tap **Start watching**. CampWatch checks every minute while it's running.

## Using it from your iPhone

The checker has to run on a machine that stays on, because a phone can't poll in the background. Pick one:

1. **A computer at home (Mac, PC, Raspberry Pi) plus [Tailscale](https://tailscale.com).** This is free, and nothing is exposed to the internet. Install Tailscale on the computer and the phone, then run:

   ```bash
   uv run campwatch --host 127.0.0.1 --port 8000
   tailscale serve --bg 8000          # gives you https://<machine>.<tailnet>.ts.net
   ```

   Set `CAMPWATCH_COOKIE_SECURE=true` and a `CAMPWATCH_APP_PASSWORD` in `.env`.
2. **A small cloud server or container host** (a $5 VPS, Fly.io, Railway, and so on) running the Docker image. Use HTTPS and set `CAMPWATCH_APP_PASSWORD`. If you use a container host, mount a persistent volume at `/data`.

Then, on the iPhone:

1. Open the app's URL in **Safari**, log in, tap **Share**, then **Add to Home Screen**.
2. Install the free **ntfy** app and subscribe to your `CAMPWATCH_NTFY_TOPIC`. Tap **Settings → Send a test alert** in CampWatch to check it works.

### Docker

```bash
cp .env.example .env    # fill it in
docker compose up -d --build
```

The data (SQLite database and session key) lives in `./data`. To include hold-in-cart support, set `WITH_BOOKING: "true"` in `docker-compose.yml`. That adds Chromium to the image.

## Alerts

Configure any combination in `.env`:

| Channel | Settings | Notes |
| --- | --- | --- |
| ntfy (recommended) | `CAMPWATCH_NTFY_TOPIC` | Free iPhone app, no account needed. Anyone who knows the topic can read it, so use a long random name (e.g. `campwatch-7f3k9q2x`). You can self-host ntfy and set `CAMPWATCH_NTFY_SERVER` / `CAMPWATCH_NTFY_TOKEN`. |
| Pushover | `CAMPWATCH_PUSHOVER_USER_KEY`, `CAMPWATCH_PUSHOVER_APP_TOKEN` | One-time purchase, very reliable. |
| Email | `CAMPWATCH_SMTP_*`, `CAMPWATCH_EMAIL_TO` | For Gmail: `smtp.gmail.com`, port 465, and a [Google app password](https://myaccount.google.com/apppasswords). |

## Hold in cart (experimental)

When a watch is set to **Alert me and hold the site in my Recreation.gov cart**, CampWatch:

1. Sends the alert first, so a failed hold never costs you the alert.
2. Opens a headless browser, signs in with `CAMPWATCH_RECGOV_EMAIL` / `CAMPWATCH_RECGOV_PASSWORD`, sets the dates on the campsite's page, and clicks **Add to Cart**. If several sites opened at once, it picks the longest stay.
3. Sends a second alert, either "In your cart" with a link to the cart, or "Couldn't hold" with a Book link. After a successful hold, the watch pauses so it doesn't keep grabbing sites.

Setup: `uv sync --extra booking && uv run playwright install chromium`.

Limitations, stated plainly:

- **It stops at the cart. It never pays.** Storing card details and auto-submitting payment is risky, and Recreation.gov's checkout changes often. You have about 15 minutes to sign in on your phone and check out.
- **It is best effort.** Recreation.gov is a JavaScript app whose layout changes, and it can show a CAPTCHA. The selectors live at the top of `campwatch/booker.py`. When a hold fails, a screenshot is saved to `data/screenshots/` so you can see what happened.
- **Read Recreation.gov's terms of use first.** They have prohibited bots and scraping, and the site's operator has said accounts caught using bots can be banned. In June 2026 a U.S. senator asked the Forest Service to set a clearer policy against bots. Alert-only mode reads the same public availability data the website shows, at a modest rate. Cart holding acts on your account, which is a bigger risk. Use it at your own risk.

## Configuration

All settings are environment variables, or lines in `.env`. See [`.env.example`](.env.example) for the full list.

| Variable | Default | What it does |
| --- | --- | --- |
| `CAMPWATCH_APP_PASSWORD` | unset | Password for the web app. Leave it unset only when nobody else can reach the app. |
| `CAMPWATCH_COOKIE_SECURE` | `false` | Set to `true` when served over HTTPS. |
| `CAMPWATCH_POLL_INTERVAL_SECONDS` | `60` | Time between checks. The minimum is 30, and each check varies by ±10%. |
| `CAMPWATCH_REQUEST_DELAY_SECONDS` | `1.0` | Pause between requests within a check. |
| `CAMPWATCH_DATA_DIR` | `data` | Location of the SQLite database, the session key and screenshots. |

If Recreation.gov answers "too many requests", CampWatch backs off automatically, up to an extra 15 minutes, and shows the delay on the Settings tab.

## How it works

```
campwatch/
  recgov.py        Recreation.gov client (availability, campground search and details)
  availability.py  Pure logic: which months to fetch, which stays are open
  checker.py       Polling loop: fetch, match, record openings, alert, optionally hold
  notify.py        ntfy, Pushover and email
  booker.py        Optional Playwright "add to cart"
  models.py        SQLite tables: watches, openings
  app.py           FastAPI JSON API (/api/...) and the static web app
  static/          The web app: plain HTML, CSS and JS, with no build step; installable as a PWA
tests/             pytest with synthetic data and a mocked network
```

- Availability comes from `GET /api/camps/availability/campground/{id}/month`, the same undocumented endpoint the Recreation.gov website uses. It returns a whole campground for one calendar month. Each check fetches each (campground, month) pair once, however many watches need it.
- An **opening** is a run of consecutive "Available" nights at one site that meets the watch's stay rule. CampWatch remembers the openings it has already alerted on. An opening that disappears and later comes back triggers a new alert.
- A watch stops by itself once all its dates have passed.

The JSON API is documented at `/docs` while the server is running. A native app or a Shortcuts automation could use the same API later.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format .
```

The tests use only synthetic campgrounds and a mocked network, so they never contact Recreation.gov.

## Finding a campground ID

You usually don't need one: search by name in the app, or paste the campground's link. The ID is the number in a URL like `https://www.recreation.gov/camping/campgrounds/232447`.
