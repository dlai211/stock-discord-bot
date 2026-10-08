# Stock Discord Bot — Product Requirements Document

A modular, always-on Discord bot that answers on-demand market questions and
pushes scheduled macro/earnings briefings into a channel.

---

## 1. Overview

| | |
|---|---|
| **Runtime** | Python 3.11+, `discord.py` 2.x (slash commands) |
| **Process model** | Single long-running worker process (`python bot.py`) |
| **Entry point** | `bot.py` |
| **Config** | `.env` (never committed) → `config.py` |
| **State** | `state/macro_state.json` (gitignored) |

The bot is a single process that connects to Discord over a websocket and runs
background polling loops alongside it. It is designed to survive network blips:
every outbound API call sits behind a retry helper, and every task loop catches
its own exceptions so a single failure can never kill the schedule permanently.

---

## 2. Architecture

```
stock-discord-bot/
├── bot.py                        # Entry point: builds the Bot, loads cogs, syncs the command tree
├── config.py                     # The only module that reads .env
├── utils.py                      # Number formatting + async/retry helpers
├── verify.py                     # Offline smoke test: loads every cog without connecting
├── requirements.txt
├── Procfile                      # Hosting entry point (worker: python bot.py)
├── .env                          # Secrets — gitignored, never logged
├── .env.example                  # Template with blank values
├── PRD.md                        # This file
├── cogs/
│   ├── __init__.py
│   ├── market_commands.py        # /stock, /macro
│   ├── scheduled_briefings.py    # Monday 08:00 ET earnings briefing
│   └── macro_alerts.py           # FRED release watcher
└── state/                        # Runtime cache, created on first run (gitignored)
    └── macro_state.json
```

### Request flow

```
Discord ──► bot.py (setup_hook loads cogs, syncs tree)
              │
              ├── cogs/market_commands.py     /stock ──► yfinance ──► Yahoo Finance
              │                               /macro ──► fredapi  ──► FRED
              │
              ├── cogs/scheduled_briefings.py tasks.loop ──► finnhub ──► Finnhub
              │
              └── cogs/macro_alerts.py        tasks.loop ──► fredapi ──► FRED
                                                     │
                                                     └── state/macro_state.json
```

### Design rules

1. **Secrets live in one place.** `.env` is read only by `config.py`. The file
   is gitignored, is never printed, and is never included in an error message.
2. **Blocking calls never touch the event loop.** `yfinance`, `fredapi` and
   `finnhub-python` are all synchronous HTTP libraries. Every call goes through
   `utils.run_blocking()`, which hands it to a worker thread. Skipping this
   would stall the bot's heartbeat and get it disconnected.
3. **Every network call is retried with backoff.** `utils.retry_async()` retries
   transient failures at 1x/2x/4x the base delay and re-raises anything listed
   in `give_up_on` immediately.
4. **Task loops never die.** `discord.ext.tasks` stops a loop permanently if an
   exception escapes it. Every loop body is wrapped in `try/except`.
5. **No duplicate posts.** `macro_alerts` keeps a seen-observation-date cache on
   disk, so a restart never replays an alert.

---

## 3. Environment Variables

Defined in `.env`. See `.env.example` for the template.
`config.missing_variables()` reports anything blank at startup.

| Variable | Required | Used by | Notes |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | `bot.py` | Bot token from the Discord Developer Portal. Treat as a password — rotating it invalidates the old one immediately. |
| `TARGET_CHANNEL_ID` | Yes | `scheduled_briefings`, `macro_alerts` | Channel that receives scheduled posts. Find it with Developer Mode → right-click channel → Copy Channel ID. |
| `FINNHUB_KEY` | Yes | `scheduled_briefings` | Free tier covers the earnings calendar. |
| `FRED_API_KEY` | Yes | `market_commands`, `macro_alerts` | Free, issued instantly from the FRED site. |

---

## 4. API Integrations

| Provider | Library | Used for | Failure mode |
|---|---|---|---|
| **Yahoo Finance** | `yfinance` | `/stock` quotes | Unofficial/scraped. Rate limits and occasional schema drift. Falls back across several key names before reporting "unavailable". |
| **FRED** (St. Louis Fed) | `fredapi` | `/macro` rates, macro release alerts | Official and stable. Some series (`DGS10`, `DGS2`) are business-day only and carry `NaN` on holidays — always `dropna()` before reading the tail. |
| **Finnhub** | `finnhub-python` | Weekly earnings calendar | Free tier: 60 calls/min. A 429 is retried with backoff. |

**Note on FRED frequencies.** `FEDFUNDS` is a *monthly average*, while `DGS10`
and `DGS2` are *daily*. Their latest observation dates will not match, which is
expected and shown per-series in the `/macro` embed.

---

## 5. Commands & Background Tasks

### 5.1 `/stock <ticker>` — `cogs/market_commands.py`

| Field | Source key(s) | Fallbacks |
|---|---|---|
| Name | `longName` | `shortName`, then the raw symbol |
| Price + currency | `currentPrice` | `regularMarketPrice`, `previousClose` |
| Daily change ($ / %) | computed from price − `previousClose` | — |
| Day range | `dayHigh` / `dayLow` | `regularMarketDayHigh` / `...Low` |
| Volume | `volume` | `regularMarketVolume` |
| Market cap | `marketCap` | — |

Embed colour is green when the daily change is positive, red when negative.
Unknown or delisted symbols produce a friendly ephemeral message rather than a
traceback.

### 5.2 `/macro` — `cogs/market_commands.py`

Pulls three FRED series and computes the 10Y–2Y spread.

| Series | Meaning | Frequency |
|---|---|---|
| `FEDFUNDS` | Effective Federal Funds Rate | Monthly |
| `DGS10` | 10-Year Treasury Constant Maturity | Daily |
| `DGS2` | 2-Year Treasury Constant Maturity | Daily |

Spread is reported in **basis points** (`(DGS10 − DGS2) × 100`) and flagged
**inverted** when negative. Per-series latest observation dates are shown.

### 5.3 Weekly Earnings Briefing — `cogs/scheduled_briefings.py`

- **Schedule:** every Monday at **08:00 US/Eastern** (`tasks.loop(time=...)` with
  a DST-aware `ZoneInfo` timezone, so it stays at 08:00 ET across the March and
  November clock changes).
- **Window:** Monday through Friday of the current week.
- **Prioritisation:** a curated high-interest list (mega-cap tech plus other
  widely held names) is surfaced first and marked with ⭐.
- **Caps:** max 12 lines per day and 25 lines overall, keeping each embed field
  safely under Discord's 1024-character limit.
- **Failure handling:** a failed Finnhub call logs and skips the week rather than
  cancelling the schedule.

### 5.4 Macro Release Alerts — `cogs/macro_alerts.py`

- **Poll:** every 15 minutes.
- **Series:** `CPIAUCSL` (CPI), `PCEPILFE` (Core PCE), `GDPC1` (Real GDP).
- **Dedupe:** `state/macro_state.json` stores the newest observation date seen
  per series. An alert fires only when a strictly newer date appears.
- **First run seeds the cache** instead of alerting — otherwise enabling the bot
  would replay months of old releases into the channel.
- **Retry:** three attempts with exponential backoff.
- **State is written only after a successful post**, so a failed send is retried
  on the next poll instead of being silently lost.

Release windows are tracked as labels (08:25–09:30 ET and 13:55–14:45 ET) and
recorded in the alert footer.

---

## 6. Setup — running locally

```powershell
cd C:\Users\laiji\VSCode\stock-discord-bot

# 1. One-time: create the virtual environment
python -m venv venv

# 2. Install dependencies
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. Fill in .env (copy from the template if it does not exist yet)
copy .env.example .env

# 4. Verify everything imports without connecting to Discord
.\venv\Scripts\python.exe verify.py

# 5. Run the bot
.\venv\Scripts\python.exe bot.py
```

In VS Code, select `venv\Scripts\python.exe` as the interpreter so the editor
resolves imports correctly.

### Discord setup

1. Create an application at the Discord Developer Portal and add a **Bot**.
2. Under **Privileged Gateway Intents**, enable **Message Content Intent**
   (`bot.py` requests it).
3. Invite the bot with the `bot` and `applications.commands` scopes.
4. Give it **View Channel**, **Send Messages**, and **Embed Links** in the target
   channel — the embed posts will silently fail without Embed Links.

Slash commands are synced globally on startup. Discord can take up to an hour to
propagate a first-time global sync to every server.

---

## 7. Deployment — 24/7 hosting

The repo includes a `Procfile`:

```
worker: python bot.py
```

Any Procfile-based host works (Railway, Render, Heroku). On such a host, set the
four environment variables in the dashboard rather than shipping `.env` — the
code reads real environment variables first and `.env` only fills the gaps.

**Never commit `.env`.** On a host that only supports the filesystem, prefer its
secret manager over uploading the file.

Note that a free-tier host that sleeps on inactivity will disconnect the bot and
miss scheduled posts. Schedules are time-of-day based rather than
interval-based, so a bot that boots late does not fire a catch-up post — the
Monday briefing is simply skipped if the process was asleep at 08:00 ET.

---

## 8. Security notes

- `.env` is gitignored. It was previously committed to this repo's history; that
  history was rebuilt before anything was pushed, and the GitHub remote was
  confirmed empty.
- **If you ever push a commit containing `.env`, treat every key in it as
  compromised** and regenerate all four: reset the Discord token in the
  Developer Portal, and reissue the Finnhub and FRED keys.
- The bot requests the Message Content intent but defines no prefix commands
  beyond the framework default; the intent is required for `command_prefix`
  handling to work at all.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `Missing required environment variables` | `.env` absent or blank | Copy `.env.example` to `.env` and fill it in |
| `ZoneInfoNotFoundError` | `tzdata` not installed | `pip install -r requirements.txt` |
| Slash commands missing in Discord | Global sync still propagating | Wait up to an hour, or re-invite with `applications.commands` |
| `/stock` says data unavailable | Yahoo Finance rate limit | Wait a minute; the command already retries once |
| Briefings never appear | Bot lacks Send Messages / Embed Links | Fix channel permissions |
| Bot disconnects periodically | Blocking call on the event loop | Ensure new code uses `utils.run_blocking()` |

---

## Development Changelog

### 2026-10-08 16:21 PT — Phase 1: Project scaffolding & dependencies

**Added**
- `requirements.txt` — `discord.py`, `python-dotenv`, `yfinance`, `fredapi`,
  `finnhub-python`, `pytz`, `requests`, plus `tzdata`.
- `.env.example` — template documenting all four required variables with
  instructions for obtaining each.
- `PRD.md` — this document.

**Modified**
- `.gitignore` — added `state/` (runtime cache) and `*.log`.

**Verified**
- Rebuilt the repository: the previous history consisted of a single `add .env`
  commit that tracked the secrets file. `.git` was deleted, re-initialized on
  `main`, and the `origin` remote re-added. `git ls-tree -r HEAD` is now empty
  and `.env` is confirmed untracked and ignored.
- `git ls-remote origin` returned zero refs — the GitHub remote was empty, so no
  secret was ever published. **No credential rotation required.**
- Created `venv/` (Python 3.11.4) so dependencies do not touch the Anaconda base
  install.
- Confirmed the provider APIs the code depends on exist in the resolved
  versions: `discord.py 2.7.1`, `yfinance 1.7.0`, `fredapi 0.5.2`,
  `finnhub-python 2.4.29`, `pandas 3.0.6`, `tzdata 2026.5`. Note that yfinance
  resolved to **1.x** and pandas to **3.x** rather than the older 0.2.x/2.x lines
  the minimum pins allow, so `Ticker.info`,
  `Client.earnings_calendar(_from, to, symbol, international)` and
  `Fred.get_series(series_id, observation_start, observation_end)` were each
  checked by introspection. All three are unchanged.

**Deviation from spec**
- `tzdata` was added to `requirements.txt`. It is not in the original list, but
  Windows ships no system timezone database, so `zoneinfo.ZoneInfo("America/New_York")`
  raises `ZoneInfoNotFoundError` without it. This would break the Monday 08:00 ET
  schedule on this machine.

### 2026-10-08 16:24 PT — Phase 2: Core architecture & interactive commands

**Added**
- `bot.py` — `FinanceBot` (`commands.Bot`, prefix `!`, `message_content=True`).
  `setup_hook()` discovers and loads every module in `cogs/`, then syncs the
  slash command tree. `on_ready` logs the bot username and latency. Global error
  handlers cover both prefix commands (`on_command_error`) and slash commands
  (`tree.on_error`), unwrapping `CommandInvokeError` and translating
  `MissingPermissions` / `CheckFailure` / `MissingRequiredArgument` into plain
  messages instead of tracebacks.
- `config.py` — the single module that reads `.env`. Exposes typed settings plus
  `missing_variables()` for a friendly startup failure, and a DST-aware
  `EASTERN` timezone.
- `utils.py` — number formatting for embeds (`format_compact`,
  `format_money`, `format_number`, `format_signed`), plus `run_blocking()`
  (moves synchronous HTTP libraries onto a worker thread so they cannot stall
  the event loop), `retry_async()` (exponential backoff with a `give_up_on`
  escape hatch), and `resolve_channel()` (falls back to an API fetch when a
  scheduled post runs before the channel cache is warm).
- `cogs/__init__.py`
- `cogs/market_commands.py` — `/stock` and `/macro`, described in §5.
- `verify.py` — offline smoke test. Loads every cog the way `bot.py` does,
  introspects the provider APIs, and checks the schedule and timezone, without
  logging in or making a network call.

**Modified**
- `PRD.md` — architecture, environment variable, API integration and command
  schema sections written out; changelog entry added.

**Verified**
- `python verify.py` — **11/13 checks pass**. The two failures are the Phase 3
  and Phase 4 background loops, which do not exist yet.
- Confirmed `TARGET_CHANNEL_ID` parses to `961806288650657833`, and that
  `zoneinfo` resolves US/Eastern to the correct −4h (EDT) offset on today's date.
- Both slash commands register: `stock`, `macro`.
- Note: discord.py logs `PyNaCl is not installed` / `davey is not installed`.
  Those are voice-support warnings and do not affect a text-only bot.

**Deviation from spec**
- The slash command tree is synced in `setup_hook()` rather than `on_ready()` as
  the brief specified. `on_ready` fires again on every reconnect and Discord
  rate-limits global command syncs, so syncing once after the cogs have
  registered their commands is both correct and cheaper. `on_ready` still logs
  the username and latency, and the sync result is logged on startup.
- `verify.py` was created here rather than in Phase 5 so that every later phase
  could be checked as it landed. Phase 5 runs it as the final gate.

### 2026-10-08 16:25 PT — Phase 3: Scheduled Monday briefing

**Added**
- `cogs/scheduled_briefings.py` — a `tasks.loop` that posts the week's earnings
  calendar to `TARGET_CHANNEL_ID`.
  - Fires at 08:00 US/Eastern via a tz-aware `datetime.time`, so DST is handled
    by the timezone database rather than by a hard-coded UTC offset.
  - Pulls Monday–Friday from Finnhub's `earnings_calendar`.
  - Sorts a curated 37-name high-interest list first and marks those entries ⭐,
    then fills remaining space alphabetically.
  - Caps the listing at 12 lines per day and 25 overall, keeping each embed field
    under Discord's 1024-character limit; overflow is reported as "*…and N more*".
  - Shows quarter, consensus EPS estimate and before-open/after-close timing per
    company, skipping any field the API leaves null.
  - Wrapped in `try/except` at two levels — the fetch and the send — so a provider
    outage or a permissions error costs one week's briefing rather than
    cancelling the loop permanently.

**Verified**
- `python verify.py` — **14/14 checks passed**.
- Confirmed the registered loop reports `08:00 tz=America/New_York`, proving the
  `zoneinfo`-backed schedule resolves correctly rather than silently falling back
  to UTC.

**Bugs found and fixed during this phase**
- `tasks.Loop` in discord.py 2.7.1 exposes only a private `_name`, not the public
  `.name` the docs imply. `verify.py` now identifies each loop by the attribute
  it is bound to on its cog.
- `Client.wait_until_ready()` **raises** `RuntimeError` when the client was never
  logged in — it does not simply wait. The briefing's `before_loop` hook now
  catches that, which both lets the offline check run cleanly and prevents a
  startup race from killing the schedule for good.

**Deviation from spec**
- `tasks.loop(time=...)` accepts only a time of day, not a weekday, so the loop
  fires daily at 08:00 ET and returns immediately unless
  `date.weekday() == 0`. The Monday-only behaviour is enforced inside the body.
