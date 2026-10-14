# Sport Flex

[English](README.md) · [中文](README.zh-TW.md)

Sport Flex looks up courts and bookable slots across configured venues, submits a reservation, and stops before payment. A local web page and optional Telegram bot can also watch open slots or try to book a court when the venue releases the next day.

> **Disclaimer.** This tool accepts no responsibility of any kind. It is provided as-is, with no warranty. You use it at your own risk. See [Disclaimer](#disclaimer).

## What it does

- Lists venues, courts, and bookable times from `venues/`.
- Keeps each account’s login in its own browser profile.
- Submits a reservation and leaves payment to you. The program does not pay.
- Runs a release-time grab from the web UI or Telegram, and can watch already-open slots.
- Records grab results and events locally for 30 days.

The bundled venue is 新北市新店國民運動中心 (`xindian`) on the Changjia (`changjia`) platform. Add another venue by dropping a YAML file in `venues/`.

## Requirements

- Python 3.12 or newer
- Google Chrome (Playwright launches the `chrome` channel)
- A venue account whose username and password live in `.env`, not in git

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chrome
```

On Linux, if Chrome’s system libraries are missing:

```bash
sudo .venv/bin/playwright install-deps chrome
```

Create `.env` in the project root (it is gitignored):

```bash
SPORT_FLEX_DEFAULT_ACCOUNT=your-username
SPORT_FLEX_DEFAULT_PASSWORD=your-password
# Optional mobile-barcode carrier, slash plus 7 characters, for example /ABC1234.
# Omit it to use a personal invoice.
SPORT_FLEX_DEFAULT_INVOICE_CARRIER=

# Optional Telegram alerts and commands.
SPORT_FLEX_TELEGRAM_BOT_TOKEN=
SPORT_FLEX_TELEGRAM_CHAT_ID=
# Label shown on captcha photos when several servers share one bot. Examples: docker, laptop.
SPORT_FLEX_INSTANCE=
```

`config/accounts.yaml` names the `.env` keys. Passwords are not stored there. The same platform can have several accounts (for example family members); each account needs its own browser profile.

Log in once so the profile keeps the session:

```bash
python -m sportflex login --account default
```

## Commands

Run from the project root with the virtualenv active (`python -m sportflex …`).

| Command | Purpose |
| --- | --- |
| `venues` | List configured venues and accounts |
| `courts --venue xindian --category 羽球` | List courts |
| `slots --venue xindian --date 2026-10-09 --from 18:00` | List bookable slots |
| `slots --name 羽球場_A --scan` | Scan the whole bookable date window |
| `login --account default` | Open the login page; the session stays in that account’s profile |
| `book --name 羽球場_A --date 2026-10-10 --time 20:00` | Submit one slot and stop before payment |
| `ui` | Open the web UI on `http://127.0.0.1:8765` |
| `history --days 3` | Show recent grab results and events |

`--venue` defaults to the first file in `venues/`. `--account` defaults to the first account for that platform. `book` places the order and does not click pay. Set `SPORT_FLEX_SUBMIT_ORDER=0` to stop on the confirmation page without placing the order.

`ui` options: `--port` (default `8765`), `--host` (use `0.0.0.0` only when you intend to expose it), `--no-browser` (do not open a local browser).

## Web UI

`python -m sportflex ui` serves a page where you can:

- Enter the captcha and log in
- Browse the court timetable (all day, morning, afternoon, evening)
- Start or cancel a grab for slots that are not open yet
- Watch already-open slots and book one when it is free
- Read the local history of grabs and alerts

The page has no login of its own. Do not publish port `8765` beyond localhost.

## Telegram

When both `SPORT_FLEX_TELEGRAM_BOT_TOKEN` and `SPORT_FLEX_TELEGRAM_CHAT_ID` are set, alerts also go to that chat, and the bot accepts commands from it. One bot token can have only one long-poller, so run one server per bot.

If a login expires, the program tries to read the captcha twice. If both reads fail, it sends the image. Reply to that photo with the characters on it. The photo is prefixed with `[server · account]` so you can tell local, Docker, and multiple accounts apart.

| Command | Purpose |
| --- | --- |
| `/status` | Login and grab status |
| `/login [account]` | Fetch a new captcha |
| `/release` | Next release time |
| `/slots [date] [time]` | Bookable slots |
| `/snipe 19:00 [20:00]` | Grab the next release (any court) |
| `/snipe A 19:00` | Grab a named court |
| `/cancel` | Cancel the grab |
| `/watch 明天 晚上` | Watch open slots and book when one is free |
| `/watch off` | Stop watching |
| `/book 明天 19:00 [court]` | Book an already-open slot (asks you to `/ok`) |
| `/courts` | Court list |
| `/history` | Recent grabs |
| `/help` | This list |

Dates can be `今天`, `明天`, `後天`, or `10/20`. Times can be `上午`, `下午`, `晚上`, or `19:00`. With several accounts, put the account id in the command. `SPORT_FLEX_TELEGRAM_RELOGIN=0` turns off Telegram re-login.

## Venues and rules

Each file in `venues/` is one venue: id, display name, provider, sport categories, booking rules, and notes. Rules the engine actually uses include the timezone, how many days ahead are bookable, the daily release time, slot length, the per-account daily hour cap, and how aggressively to poll. The bundled `xindian` file documents the 新店 center’s published window (14 days, released at 06:00, one hour per booking, at most two hours a day on different courts). Treat those numbers as notes you maintain; the venue can change them.

## Docker

```bash
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml logs -f
docker compose -f infra/docker-compose.yml down
```

Open `http://127.0.0.1:8765/` and log in once with the captcha. The login is stored in the `sportflex-profiles` volume. The container’s Chrome profile is separate from a local `python -m sportflex ui`, so the two do not share a profile. The compose file publishes the UI on `127.0.0.1` only (`SPORT_FLEX_PORT`, default `8765`).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Disclaimer

**This tool accepts no responsibility and no liability of any kind.**

Sport Flex is provided “as is” and “as available,” without warranty of any kind, express or implied, including fitness for a particular purpose, accuracy, or uninterrupted operation.

The authors, contributors, and this tool are not responsible for anything that results from installing, running, or being unable to run it, including:

- missed, duplicate, partial, or unwanted reservations
- venue fees, cancellation charges, non-refundable bookings, or unpaid orders that the venue cancels
- account restrictions, bans, captcha failures, or rate limits imposed by a venue or platform
- payment, invoices, personal data, or anything you type into a venue site
- wrong slot status, prices, or booking rules
- any loss, damage, claim, or dispute

You alone decide to use the tool. You must follow each venue’s terms and the law that applies to you. Review every order before you pay. **The tool does not pay for you.** It is not affiliated with, endorsed by, or operated by any sports center or booking platform.

Using, copying, or modifying this tool means you accept this disclaimer.
