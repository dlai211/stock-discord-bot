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
├── utils.py                      # Formatting, async/retry, atomic JSON helpers
├── verify.py                     # Offline smoke test: loads every cog without connecting
├── requirements.txt
├── Procfile                      # Hosting entry point (worker: python bot.py)
├── .env                          # Secrets — gitignored, never logged
├── .env.example                  # Template with blank values
├── PRD.md                        # This file
├── cogs/
│   ├── __init__.py
│   ├── market_commands.py        # /stock, /macro
│   ├── scheduled_briefings.py    # Monday 08:00 ET briefing, /test_briefing
│   ├── macro_alerts.py           # FRED release watcher, /test_macro_alert
│   └── earnings_tracker.py       # /track_earnings watch list, daily 17:30 ET check
└── state/                        # Runtime cache, created on first run (gitignored)
    ├── macro_state.json          # Newest announced observation per FRED series
    ├── tracked_earnings.json     # The earnings watch list
    └── earnings_notified.json    # Earnings alerts already sent, to dedupe
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
              ├── cogs/earnings_tracker.py    tasks.loop ──► finnhub ──► Finnhub
              │                                     │
              │                                     ├── state/tracked_earnings.json
              │                                     └── state/earnings_notified.json
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
6. **Schedules run on the Eastern clock; everything displayed is Pacific.**
   Exchanges and the release calendar work in US/Eastern, so windows are defined
   there. Every time a reader sees is converted to US/Pacific through
   `zoneinfo` — never by subtracting three hours — and derived from the Eastern
   definition rather than typed in twice. See §5.7.

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

The embed also carries a **headline CPI inflation rate**: the year-over-year
change in `CPIAUCSL`, computed across the latest 13 monthly observations
(`(latest − 12 months prior) / 12 months prior`). It is displayed with both
endpoints — the period and index level of each — so the window is never
ambiguous. If FRED has not published a full 13-month window, the field says so
rather than showing a partial calculation.

### 5.3 Weekly Earnings Briefing — `cogs/scheduled_briefings.py`

- **Schedule:** every Monday at **08:00 US/Eastern** (`tasks.loop(time=...)` with
  a DST-aware `ZoneInfo` timezone, so it stays at 08:00 ET across the March and
  November clock changes).
- **Window:** Monday through Friday of the current week.
- **Selection:** high-interest names plus any company with an analyst estimate.
  See below.
- **Failure handling:** a failed Finnhub call logs and skips the week rather than
  cancelling the schedule. A failed *snapshot* call does not even cost the week —
  the briefing still posts, with the missing quotes rendered `N/A`.
- **Market snapshot:** a `📊 Market Snapshot (Monday Open)` field covering 17
  assets across crypto, equities, bonds, commodities and currencies. See §5.8.

#### Row format

One company per line, with every optional part dropped when the API leaves it
null:

```
⭐ JPM · Q3 · EPS est $5.92 · Pre-Market (~05:30-06:30 PT)
```

The year is left out of the quarter — the title already carries the date range.
`EPS est` is dollar-signed: Finnhub does not state which currency an estimate is
in, and the overwhelming majority of names on this calendar report in USD.

#### What gets listed

Two rules, in priority order:

1. **High-interest names always appear**, whether or not anyone covers them. They
   are never dropped to satisfy a cap.
2. **Everything else needs an analyst estimate.** A ticker with no `epsEstimate`
   is almost always an uncovered micro-cap. These were flooding the listing and
   crowding out the days that actually mattered.

#### Even spread across the week

A per-day cap of **5 lines**, with the weekly budget divided by the number of
days still to come:

```
lines_allowed = max(MIN_PER_DAY, min(MAX_PER_DAY, remaining // days_left))
```

A heavy Monday therefore cannot spend Friday's allowance, and `MIN_PER_DAY = 1`
guarantees each remaining day still gets a line once the budget runs out.

**This replaced a real bug.** The previous version gave each day up to 12 lines
out of a shared 25-line pool and then `break`-ed out of the loop once the pool
was empty — so a busy Monday and Tuesday ended the briefing at Wednesday, and
Thursday and Friday were missing entirely. The `break` is gone; a day is now
skipped only when it has no scheduled earnings at all.

A day whose reports all lack estimates still gets a field reading
`*No analyst estimates available*`, so an empty day is never mistaken for a bug.

Overflow reads `*…and N more with estimates*`. The word "estimates" matters here:
the headline company count includes the filtered-out micro-caps, so a bare "and
2 more" would look wrong next to a count of 201.

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

Release windows are tracked as labels and recorded in the alert footer. They are
defined in Eastern and displayed in Pacific — `05:25-06:30 PT window
(CPI / PCE / GDP)` and `10:55-11:45 PT window (FOMC)`. See §5.7.

### 5.5 Test commands

Two commands exercise the scheduled paths on demand, so neither has to wait for
a Monday or a real data release.

| Command | Cog | Effect |
|---|---|---|
| `/test_briefing` | `scheduled_briefings` | Runs the real briefing generator and posts it to `TARGET_CHANNEL_ID`. |
| `/test_macro_alert <series>` | `macro_alerts` | Pulls the latest real observation for the chosen series and posts it as an alert. |

Both share the production code path rather than a copy of it, so a passing test
means the real post will work.

**Test posts are unambiguous.** The title is prefixed `[TEST]`, the embed colour
differs from a real post (amber→grey for alerts, blue→amber for briefings), and
the footer says `TEST POST`.

**The release cache is never touched by a test.** `state/macro_state.json`
tracks which periods have already been announced. If `/test_macro_alert` wrote
to it, the *real* alert for that period would later be silently suppressed.
The command deliberately skips the write, so testing is always safe to repeat.

`/test_macro_alert` still reads live data, so it needs FRED to be reachable; if
it is not, the command reports the failure instead of posting.

`/test_briefing` reports on the current week, or the coming week when run on a
Saturday or Sunday — otherwise a weekend test could only ever answer "no
earnings".

### 5.6 Earnings tracker — `cogs/earnings_tracker.py`

A persistent watch list of tickers whose results are announced as they land.

| Command | Effect |
|---|---|
| `/track_earnings <ticker>` | Verifies the symbol against Finnhub, then adds it to `state/tracked_earnings.json`. |
| `/untrack_earnings <ticker>` | Removes it. |
| `/list_earnings` | Shows the watch list. |

All three reply ephemerally so they don't clutter the channel.

**Symbol validation** uses Finnhub's `/search` endpoint with an *exact* symbol
match. Search is fuzzy, so accepting the top result would happily track the
wrong company after a typo.

**The daily check** runs at **17:30 US/Eastern**. It makes one Finnhub call for
the day's whole earnings calendar and filters it locally, rather than one call
per tracked ticker. For each match it posts an embed with actual vs consensus
EPS, the surprise in dollars and percent, and revenue where Finnhub provides it.
The embed is green on a beat, red on a miss.

**Tracked tickers persist until removed.** Companies report roughly quarterly,
so a watch list that emptied itself after each report would be useless.

**Both commands take whole embeds, not raw strings.** An entry missing its
consensus estimate still renders — the surprise line reads `N/A` rather than
failing.

> **Known gap.** Because this runs once a day, a company that reports after the
> close and whose actual EPS is not on Finnhub by 17:30 is logged and skipped —
> there is no second check that day. To close it, add a second time to the
> loop's `time=` argument (it accepts a list), for example 08:00 ET the
> following morning. See the changelog entry for why this is not enabled by
> default.

### 5.7 Time handling

Two clocks are in play, and mixing them up is the easiest way to ship a wrong
time, so the rule is fixed:

- **Schedules are defined in US/Eastern** — the clock the exchanges and the
  release calendar run on. The briefing fires at 08:00 ET, the earnings check at
  17:30 ET, and the release windows are 08:25–09:30 ET and 13:55–14:45 ET.
- **Everything a reader sees is US/Pacific.** `utils.to_pacific_clock()` converts
  through `zoneinfo("America/Los_Angeles")` against a real date, so PST/PDT is
  applied by the timezone database rather than by subtracting three hours.

Pacific labels are **derived from the Eastern definition**, never typed in
twice, so a window can only be edited in one place:

| Earnings code | Eastern | Displayed |
|---|---|---|
| `bmo` | 08:30–09:30 | `Pre-Market (~05:30-06:30 PT)` |
| `amc` | 16:00–17:00 | `Post-Market (~13:00-14:00 PT)` |
| `dmh` | 09:30–16:00 | `Market Hours (~06:30-13:00 PT)` |

| Release window | Eastern | Displayed |
|---|---|---|
| CPI / PCE / GDP | 08:25–09:30 | `05:25-06:30 PT window` |
| FOMC | 13:55–14:45 | `10:55-11:45 PT window` |

The **trigger** for every one of these stays Eastern; only the label converts.
ET and PT currently always differ by exactly three hours because US DST rules
align, but the conversion goes through the timezone database regardless, so the
labels remain correct if a window is redefined or the rules ever diverge. This is
asserted in `verify.py`, including across a DST boundary.

### 5.8 Market snapshot — `cogs/scheduled_briefings.py`

A `📊 Market Snapshot (Monday Open)` field on the Monday briefing, covering 17
assets in five groups.

| Group | Assets |
|---|---|
| Crypto | BTC, ETH, HYPE, XMR |
| Equities | SP500 (`SPY`), QQQ, World (`URTH`), EEM |
| Bonds | HYG, US 10Y Yield (`^TNX`) |
| Commodities | Crude Oil (`CL=F`), Gold (`GC=F`), Silver (`SI=F`) |
| Currencies | EUR/USD, USD/JPY, AUD/USD, USD/CHF |

Rendered as a fenced code block using tree branches, so the columns stay aligned
in Discord's monospace font:

```
Crypto
├ BTC: $81,661.02 | -1.94%
├ ETH: $2,474.37 | -3.85%
├ HYPE: $83.85 | -5.22%
└ XMR: $541.48 | -2.18%
Equities
├ SP500: $777.22 | -0.24%
…
```

**Fetching.** All 17 symbols come from **one batched `yf.download()` call**, not
seventeen `Ticker` round trips, run through `utils.run_blocking()` so the event
loop is never stalled. `auto_adjust=False` keeps raw closes, so the daily change
is not skewed by dividend adjustments.

**Value formatting** is driven by a per-asset `kind`:

| Kind | Renders as | Used by |
|---|---|---|
| `usd` | `$67,421.30`, six decimals below $1 | crypto, equities, commodities |
| `percent` | `5.23%` | `^TNX`, which already quotes its yield in percent |
| `rate` | `1.0876`, or two decimals above 50 | FX pairs |

The change column is the relative percentage move against the previous close.
For `^TNX` that is a *relative* change in the yield rather than a basis-point
move — the layout calls for a uniform row format, but it is worth knowing when
reading the bond line.

**Failure handling.** A symbol that does not resolve, or that has no previous
close, renders `N/A` rather than raising. If the entire batch fails, the
briefing still posts with every row as `N/A`.

**Symbol gotcha.** Yahoo lists Hyperliquid under `HYPE32196-USD`; the obvious
`HYPE-USD` returns no data at all. Every symbol in the table was checked against
live Yahoo data when this was added — 17/17 resolve.

**Placement.** The snapshot is the last field in the embed, after the day-by-day
earnings. The earnings detail is the point of the post, so it should stay the
first thing read; move the field earlier if market context matters more to you.

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

Use the **worker** process type, not `web`. The bot holds a persistent websocket
to Discord and never listens on a port, so hosting it as a web process makes the
platform health-check a port that nothing is bound to.

### Railway / Render / Heroku

1. Push the repo to GitHub — confirm `.env` is not in the commit first.
2. Create a project from the repo. `Procfile` and `requirements.txt` are read
   automatically, so no build configuration is needed.
3. Add the four variables in the dashboard's variables/secrets panel:
   `DISCORD_TOKEN`, `FINNHUB_KEY`, `TARGET_CHANNEL_ID`, `FRED_API_KEY`.
   Do not upload `.env`.
4. Deploy and check the logs for, in order:
   `Loaded 3 cog(s)` → `Synced 2 slash command(s) globally` →
   `Logged in as <name>`.

`config.py` reads real environment variables first and only fills gaps from
`.env`, so dashboard variables take precedence and no `.env` file needs to exist
on the host at all.

**Never commit `.env`.** Prefer the platform's secret manager over uploading the
file.

### Sleep behaviour

Time-of-day schedules do not catch up. If the host suspends an idle worker, the
Monday 08:00 ET briefing is simply skipped for that week — `tasks.loop(time=...)`
computes the *next* occurrence, it does not fire a missed one on boot. A
free tier that sleeps will therefore miss briefings and macro alerts. Enable the
always-on / no-sleep option if that matters to you.

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
| `/track_earnings` rejects a real ticker | Finnhub's search has no *exact* match for it | Use the symbol exactly as Finnhub lists it (e.g. `BRK.B`) |
| Earnings alerts never arrive | Watch list is empty, or the company's actual EPS was not on Finnhub at 17:30 | Check `/list_earnings`; see the known gap in §5.6 |
| A `[TEST]` post appeared in the channel | Someone ran a test command | Expected — both test commands are open to everyone in the server |
| `/macro` shows "has not published a full 13-month window" | `CPIAUCSL` is short (early in a series' life, or after an outage) | Nothing to fix; it resolves as FRED publishes |

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
- Confirmed `TARGET_CHANNEL_ID` parses to an integer, and that `zoneinfo`
  resolves US/Eastern to the correct −4h (EDT) offset on today's date. The
  channel ID itself is deliberately not reproduced in this document.
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

### 2026-10-08 16:26 PT — Phase 4: Macro release polling

**Added**
- `cogs/macro_alerts.py` — polls FRED every 15 minutes for `CPIAUCSL`,
  `PCEPILFE` and `GDPC1`, and posts an alert the first time a new observation
  date appears.
  - **State cache:** `state/macro_state.json` maps each series to the newest
    observation date already announced. Dates are compared as ISO strings, which
    sort correctly, so no parsing is needed.
  - **Seeding:** a series with no cache entry has its current observation date
    recorded *without* announcing. Without this, switching the bot on would
    replay every release in the lookback window into the channel at once.
  - **Atomic writes:** the cache is written to a `.tmp` file and then moved into
    place, so a crash mid-write cannot leave a corrupt JSON file behind.
  - **Write-after-post:** the cache is only advanced once a message actually
    sends. A failed post is retried on the next poll instead of being silently
    swallowed.
  - **Alert contents:** release category, period label (month for monthly
    series, quarter for GDP), new value, previous value, change versus the prior
    period, change versus a year ago, and the release window it landed in.
  - **Isolation:** each series is fetched independently, so one failing series
    cannot stop the other two from being checked.
  - Retries use exponential backoff (3 attempts), and the whole body is wrapped
    so no exception can stop the loop permanently.

**Modified**
- `verify.py` — added an embed-rendering section that builds both embeds from
  synthetic data. It feeds in a company with null fields (to prove nulls are
  skipped rather than rendered as "None") and 40 companies on one day (to prove
  the per-day cap and overflow line work), then asserts every field stays inside
  Discord's 1024-character limit.

**Verified**
- `python verify.py` — **24/24 checks passed**.
- Both loops register: `weekly_briefing` at `08:00 America/New_York`, and
  `poll_macro_releases` every 15 minutes.
- Briefing embed renders 2 day fields with the widest at 592 characters — well
  inside the cap.
- Macro alert embed renders 6 fields; period labels verified as `Q2 2026`
  (quarterly) and `June 2026` (monthly).

**Deviation from spec**
- The 02:00 PM ET FOMC window is currently **label-only**. The brief specifies
  monitoring CPI, Core PCE and Real GDP — none of which is a rate decision — so
  no monitored series can produce a 14:00 release. The window is defined and
  reported in the alert footer, and making it functional is a one-line change:
  add `DFEDTARU` (fed funds target range, upper limit) to `MONITORED_SERIES`.
  This is noted in a comment at the definition rather than silently left empty.

### 2026-10-08 16:27 PT — Phase 5: Verification & cloud readiness

**Added**
- `Procfile` — `worker: python bot.py`.

**Modified**
- `PRD.md` — §7 Deployment rewritten with concrete per-platform steps, an
  explicit warning to use the **worker** process type rather than `web` (the bot
  never binds a port, so a web health check would fail), and a note that
  time-of-day schedules do not fire a catch-up post after a host sleeps.

**Verified**
- `python -m compileall` across `bot.py`, `config.py`, `utils.py`, `verify.py`
  and all four files in `cogs/` — exit 0, no syntax errors.
- `python verify.py` — **24/24 checks passed**, exit 0. All three cogs load
  without connecting to Discord; both background loops register with the
  expected schedule; both embeds render from synthetic data inside Discord's
  field limits.
- Secret scan: every value in `.env` was searched for across all 12 tracked
  files. **No value appears in any tracked file**, and `.env` is untracked.

**Correction made during this phase**
- The Phase 2 entry above originally reproduced the literal `TARGET_CHANNEL_ID`
  value in its "Verified" notes. That has been replaced with a redacted
  description. The scan above is what caught it.
- No credential was ever committed: the Discord token, Finnhub key and FRED key
  were confirmed absent from every tracked file, both before and after the fix.
  A Discord channel ID is not usable to authenticate — it identifies a channel,
  not a secret — but it is still an environment value and does not belong in a
  repo that may become public.

### Final state

| Check | Result |
|---|---|
| Cogs load offline | 3/3 |
| Slash commands registered | `/stock`, `/macro` |
| Background loops | `weekly_briefing` @ 08:00 ET Mondays, `poll_macro_releases` every 15 min |
| Tracked files containing a secret | 0 |
| `.env` tracked | No |
| Byte-compile | Clean |
| Offline check suite | 24/24 |

**Not verified by design:** at the time of this phase the bot had not been
connected to Discord. The user chose offline verification, so login, live
slash-command sync, real provider responses, and the scheduled posts had not
been exercised against the live services. (Superseded — see the entry below.)

### 2026-10-08 17:02 PT — Enhancements: CPI inflation, test commands, earnings tracker

**Correction to the Phase 5 note above.** The bot has since been run live. The
state cache at `state/macro_state.json` contains real observation dates for
`CPIAUCSL`, `GDPC1` and `PCEPILFE` — a file only `_check_for_releases()` writes,
and only after `resolve_channel()` has returned a channel. Discord login,
channel resolution and a live FRED round trip are therefore all confirmed
working, and the first-run seeding behaved exactly as designed by recording the
current periods without announcing them.

**Added**
- `/macro` now reports **headline CPI inflation** — the year-over-year change in
  `CPIAUCSL` across the latest 13 monthly observations, shown with both
  endpoints (period and index level). See §5.2.
- `/test_briefing` — runs the real briefing generator and posts it now. See §5.5.
- `/test_macro_alert <series>` — posts a live-data alert for `CPIAUCSL`,
  `PCEPILFE` or `GDPC1` on demand. See §5.5.
- `cogs/earnings_tracker.py` — a persistent earnings watch list with
  `/track_earnings`, `/untrack_earnings`, `/list_earnings` and a daily 17:30 ET
  result check. See §5.6.
- `utils.load_json()` / `utils.save_json()` — shared atomic JSON I/O.

**Modified**
- `utils.py` — added the JSON helpers; `save_json` writes to a `.tmp` file and
  moves it into place, and both helpers never raise.
- `cogs/macro_alerts.py` — `_load_state` / `_save_state` now delegate to the
  shared helpers, removing a duplicate atomic-write implementation.
- `cogs/scheduled_briefings.py` — `_post_briefing()` now takes an explicit
  Monday–Friday window and raises `BriefingNotSent` (with a user-safe message)
  instead of returning silently. The scheduled loop and `/test_briefing` call
  the same function, so the test exercises production code rather than a copy.
  Added `_briefing_week()`.
- `verify.py` — grown from 24 to **48 checks**.

**Decisions taken with the user**
- The two test commands are **unrestricted** — any member who can see the
  channel can run them and produce a `[TEST]` post. The alternative (gating on
  "Manage Server") was offered and declined.
- The earnings check runs **once daily at 17:30 ET**, not twice. The trade-off —
  after-close prints published later than 17:30 are missed — was shown in
  plain language and accepted. It is recorded as a known gap in §5.6 along with
  the one-line change to close it.
- `/test_macro_alert` uses **real FRED data** and is labelled `[TEST]`, rather
  than fabricated numbers, so it proves the whole pipeline.

**Verified**
- `python verify.py` — **48/48 checks passed**, exit 0. Now covers all seven
  slash commands, all three background loops, both test-post variants and the
  earnings result embed.
- New checks include a genuine unit test of the CPI arithmetic: a synthetic
  13-point series rising 300 → 312 must yield exactly 4.00% year-over-year,
  must report 13 observations, must compare the correct two endpoints, must
  return `None` for a 6-point window, and must return 0.00% for a flat series.
- `_briefing_week()` verified against real calendar dates: a Thursday resolves
  to that week's Monday–Friday, a Saturday rolls forward to the coming week.
- `python -m compileall` across all cogs — exit 0.
- Secret scan — no `.env` value appears in any tracked file.

**Note on `bot.py`** — this file needed **no changes**. `discover_extensions()`
globs `cogs/*.py` at startup, so `earnings_tracker.py` was picked up
automatically and its commands synced without registration. Adding a manual
import list would have been a regression.

**Final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | `/stock`, `/macro`, `/test_briefing`, `/test_macro_alert`, `/track_earnings`, `/untrack_earnings`, `/list_earnings` |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 48/48 |

**Still not exercised:** posting behaviour for the new commands, a real earnings
result embed from live Finnhub data, and the 17:30 check firing on schedule.
Run `/test_briefing` and `/test_macro_alert` in Discord to confirm those.

### 2026-10-08 17:20 PT — Briefing: Pacific display times and Monday market snapshot

**Added**
- **Pacific display conversion.** `config.PACIFIC` (`America/Los_Angeles`) and
  `utils.to_pacific_clock()`. Every time shown to a reader is now converted
  through `zoneinfo`. See §5.7.
- **`📊 Market Snapshot (Monday Open)`**, a 17-asset field on the Monday
  briefing. See §5.8.

**Modified**
- `config.py` — added `PACIFIC` alongside `EASTERN`, loaded under the same
  `zoneinfo`/`pytz` fallback.
- `utils.py` — added `to_pacific_clock()`.
- `cogs/scheduled_briefings.py` — `TIMING_LABELS` replaced by `TIMING_WINDOWS`,
  which stores each slot as an Eastern time range; `timing_label()` derives the
  Pacific text from it. Added the snapshot constants, the batched fetch, the
  block renderer, and the new embed field. Footer now reads *times are
  US/Pacific*. `_build_embed` no longer returns early when the week has no
  earnings, so the snapshot renders in that case too.
- `cogs/macro_alerts.py` — `RELEASE_WINDOWS` now holds Eastern hours rather than
  pre-formatted minute offsets; `release_window_label()` derives the Pacific
  text. The *trigger* comparison is unchanged.
- `verify.py` — grown from 48 to **78 checks**.

**Bug found and fixed**
- **`HYPE-USD` does not exist on Yahoo Finance.** A live symbol check returned
  16/17: Yahoo lists Hyperliquid under the numeric-suffixed `HYPE32196-USD`, and
  the obvious symbol returns no data at all. Left unfixed, that row would have
  read `N/A` forever with no error anywhere. The correct symbol is now
  configured, and all 17 resolve.

**Verified**
- `python verify.py` — **78/78 checks passed**, exit 0.
- Pacific DST confirmed: `America/Los_Angeles` resolves to −7h in July and −8h in
  January, and `to_pacific_clock(8, 30)` returns `05:30` on both a summer and a
  winter date — so the display is stable across the DST boundary.
- The two labels the brief named are asserted as **exact strings**:
  `Pre-Market (~05:30-06:30 PT)` and `Post-Market (~13:00-14:00 PT)`.
- Snapshot renderer checked for fenced blocks, tree branches, presence of all
  five groups, the `N/A` fallback for both a missing previous close and a wholly
  absent symbol, and that every block and every resulting embed field stays
  inside Discord's 1024-character cap (a realistic render is 494 characters).
- Asserted that the snapshot still renders when the week has **no** earnings —
  a regression test for the early return removed above.
- **Live Yahoo check:** all 17 symbols fetched successfully. The 10Y yield came
  back at `5.231`, confirming the `percent` kind renders it as `5.23%` rather
  than scaling it.
- `python -m compileall` across all cogs — exit 0.

**Notes**
- **On "Monday Open".** The briefing fires at 08:00 ET, which is 05:00 PT —
  before the 06:30 PT equity open. Equity, bond and commodity rows therefore
  reflect **Friday's close**, not a Monday opening print. Crypto trades
  continuously, so those rows are live. The field name is as specified; the
  data it holds is the latest available, not literally the Monday open.
- **Live network call made during this phase.** Verifying the symbol list
  required one read-only, unauthenticated request to Yahoo Finance. No API key
  and no Discord connection was involved.
- **Formatting tweaks beyond the brief:** FX pairs above 50 render to two
  decimals (`USD/JPY: 158.07`) rather than four, and dollar-quoted assets below
  $1 render to six decimals — both to avoid absurd-looking output. USD/JPY at
  four decimals would have read `158.0700`.
- `URTH` was chosen over `VT` for the "World" line, since `VT` (total world)
  would overlap with the `EEM` emerging-markets line directly beneath it.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 7 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Market snapshot symbols resolving | 17/17 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 78/78 |

### 2026-10-11 16:50 PT — Briefing: filter uncovered names, spread the week evenly

**Fixed — days were being cut off entirely.** The briefing gave each day up to
**12** lines out of a shared 25-line pool, then `break`-ed out of the loop once
the pool hit zero. Two busy days spent the whole budget, so the briefing ended at
Wednesday and **Thursday and Friday never appeared at all**. Three changes:

1. `MAX_PER_DAY` dropped from 12 to **5**, which is what "4-5 top names a day"
   asks for and lands exactly on the 25-line budget across five days.
2. The budget is now divided by the days still to come —
   `max(MIN_PER_DAY, min(MAX_PER_DAY, remaining // days_left))` — so a heavy
   Monday cannot spend Friday's allowance.
3. **The `break` is gone.** With `MIN_PER_DAY = 1`, a day is now skipped only
   when it genuinely has no scheduled earnings.

**Fixed — the listing was dominated by uncovered micro-caps.** A company now
earns a line only if it is on the high-interest list **or** has an analyst
estimate. High-interest names are exempt from the filter and from the cap: they
are never dropped, even on a day with more of them than the per-day allowance.

**Changed — compact rows.** The format is now exactly as specified:

```
⭐ JPM · Q3 · EPS est $5.92 · Pre-Market (~05:30-06:30 PT)
```

Bold was dropped from the ticker, the year dropped from the quarter (the title
already carries the date range), the separator after the symbol is now ` · `
rather than ` — `, and `EPS est` gained a `$`.

**Added — an empty day says so.** A day whose reports all lack estimates still
gets a field reading `*No analyst estimates available*`, so a quiet day is never
mistaken for the filter misfiring.

**Modified**
- `cogs/scheduled_briefings.py` — added `MIN_PER_DAY`, `DAY_FIELD_BUDGET`, the
  `_is_high_interest` / `_has_estimate` / `_is_worth_listing` predicates and
  `_render_day()`, which returns the day's lines plus how many eligible entries
  were cut.
- `verify.py` — grown from 78 to **92 checks**.

**Verified**
- `python verify.py` — **92/92 checks passed**, exit 0.
- The row format is asserted as an **exact string** against the one in the brief.
- A busy-week fixture (30 companies on each of five days) now asserts that all
  five days get a field, that **Friday survives**, and that no day exceeds the
  cap plus its overflow note. This is the regression test for the bug above.
- A day with **8 high-interest names** — more than the cap of 5 — asserts that
  all 8 still appear and the field stays inside Discord's 1024-character limit.
- The filter is checked in both directions: a starred ticker with **no** estimate
  is still listed, an unstarred ticker with **no** estimate is dropped, and an
  unstarred ticker **with** an estimate is kept.
- Rendered a realistic noisy week (201 companies, 11 of them notable, with
  micro-caps on Monday through Wednesday) and read the output: every day present,
  rows in the specified format, overflow counted correctly.
- `python -m compileall` — exit 0.

**Notes**
- **No network calls were made in this phase.** The preview above used synthetic
  data, so nothing was fetched from Yahoo or Finnhub.
- The overflow line reads `*…and N more with estimates*` rather than `*…and N
  more*`. The headline count includes the filtered micro-caps, so "and 2 more"
  next to "201 companies report" reads as a contradiction. Saying which pool the
  "more" comes from removes the ambiguity.
- **`$` on EPS is an assumption.** Finnhub does not state the estimate's
  currency. The filter and the high-interest list both skew heavily toward US
  issuers, so USD is right in practice, but a foreign issuer reporting in EUR
  would be labelled with a dollar sign.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 7 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Market snapshot symbols resolving | 17/17 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 92/92 |
