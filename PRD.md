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
├── charting.py                   # Matplotlib chart rendering (Agg backend only)
├── verify.py                     # Offline smoke test: loads every cog without connecting
├── requirements.txt
├── Procfile                      # Hosting entry point (worker: python bot.py)
├── .env                          # Secrets — gitignored, never logged
├── .env.example                  # Template with blank values
├── PRD.md                        # This file
├── COMMANDS.md                   # Short user-facing command reference
├── cogs/
│   ├── __init__.py
│   ├── market_commands.py        # /stock, /macro, /earnings
│   ├── scheduled_briefings.py    # Monday 08:00 ET briefing + macro calendar, /test_briefing
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
| Open | `open` | `regularMarketOpen` |
| Daily change ($ / %) | computed from price − `previousClose` | — |
| Day range | `dayHigh` / `dayLow` | `regularMarketDayHigh` / `...Low` |
| Volume | `volume` | `regularMarketVolume` |
| Market cap | `marketCap` | — |

Embed colour is green when the daily change is positive, red when negative.
Unknown or delisted symbols produce a friendly ephemeral message rather than a
traceback.

A **chart image** is attached and the message carries four timeframe buttons.
See §5.10.

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

Only two things:

1. **The curated 37 high-interest names** (⭐), whether or not anyone covers them.
2. **Tickers you track** with `/track_earnings`, re-read from
   `state/tracked_earnings.json` at post time so newly added ones appear without
   a restart.

Everything else is left out. The calendar runs to 150–250 companies a week, and
listing them buried the handful of names that actually matter.

An earlier version also admitted any unstarred company that had an analyst
estimate. That still produced dozens of lines a day, so it was dropped too.

#### Even spread across the week

Filtering this hard removes the need for a line budget. There is no per-day cap
and no weekly total any more — every listed company appears on its day.

A **1,000-character ceiling per day field** (`DAY_FIELD_BUDGET`) is the only
limit, and it binds only on a freak day; anything it cuts is reported as
`*…and N more*`.

A day with scheduled earnings but nothing listed still gets a field reading
`*No high-interest or tracked companies.*`, so a quiet day can never be mistaken
for one the code dropped. A day with **no** earnings at all gets no field.

**History worth remembering.** Each day used to take up to 12 lines from a shared
25-line pool, and the loop `break`-ed once the pool was empty — so a busy Monday
and Tuesday ended the briefing at Wednesday and Thursday and Friday never
appeared at all. That was fixed with a fair-share allocation, and the filter has
since made any cap unnecessary.

#### Macro economic calendar

A `📅 Macro Economic Calendar This Week` field listing tier-1 US releases between
Monday and Friday, each with its date and its time in Pacific:

```
Wednesday, Oct 14 · CPI Inflation · 05:30 PT
```

| FRED release name | Shown as | Publishes (ET) |
|---|---|---|
| `Consumer Price Index` | CPI Inflation | 08:30 |
| `Personal Income and Outlays` | Core PCE Price Index | 08:30 |
| `Gross Domestic Product` | Real GDP | 08:30 |
| `Employment Situation` | Non-Farm Payrolls | 08:30 |

If nothing is scheduled the field reads *"No major tier-1 macro releases
scheduled this week."* If the FRED call fails it reads *"Unavailable"* instead —
an empty week and a broken lookup must never look the same.

**Matching is exact, not substring.** FRED also publishes "Sticky Price CPI",
"Current Median CPI", "Gross Domestic Product by State" and "Trimmed Mean PCE
Inflation Rate". A substring match would list all of those as headline prints.

**Times are mapped, not fetched.** FRED publishes release *dates* but no clock
times, so each release carries the fixed time its issuing agency uses — BLS and
BEA both publish at 08:30 ET. The Pacific rendering is derived from that and
anchored to the release's own date, so the correct standard or daylight offset
applies.

**FOMC is not included, and cannot be sourced from FRED.** FRED's only FOMC
release is `FOMC Press Release`, which carries a date for *every day of the year*
(365 in 2026) because it also covers routine speeches and statements, so it
cannot isolate a rate decision. `DFEDTARU` and the other fed funds target series
resolve to that same release, and a *future* meeting day cannot be derived from
anything FRED publishes. Adding FOMC back needs a dated meeting calendar, not a
data feed.

**Why not Finnhub.** Finnhub's `/calendar/economic` is the natural source and
carries real times, but it returns **HTTP 403 on a free-tier key** — verified
directly. FRED is the free alternative, at the cost of the mapped times above.

**Why not fredapi.** The installed `fredapi` exposes no release-calendar method
(only `search_by_release` and per-series release helpers), so this uses FRED's
`/releases/dates` REST endpoint directly through `requests`.

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

#### The alert leads with rates, not levels

An alert reading "CPI 336.30" tells a reader nothing — the market trades the rate
of change. Every field is a rate now, with the index level demoted to context:

| Series | Primary | Context |
|---|---|---|
| CPI | headline **and** core, as a table (see below) | index levels, link to the BLS release |
| Core PCE | MoM rate, YoY rate | Index level (2017=100) |
| Real GDP | Annualized QoQ (SAAR), YoY growth | Real output, $B |

Each headline carries the previous period's rate beside it, so acceleration and
deceleration are visible without a second lookup:

```
MoM Rate
   +0.65%
   vs +0.40% prior month
```

GDP uses BEA's standard annualisation, `((new / prev) ** 4 - 1) * 100`, reported
to one decimal — `+2.2%`, not four more digits nobody quotes.

#### The CPI alert is a table

BLS publishes headline and core together, so one alert carries both rather than
two firing at the same second. Four metrics as inline fields would wrap
three-to-a-row and strand the fourth, so they go in a monospace code block —
**grouped by measure rather than flattened**:

```
Metric     Actual  Prior   Trend
Headline
  YoY        3.7%   3.4%   ▲︎ Accel
  MoM       +0.6%  +0.4%   ▲︎ Accel
Core
  YoY        2.6%   2.4%   ▲︎ Accel
  MoM       +0.3%  +0.3%   ⏸︎ Flat
```

**Width is the binding constraint.** A code block scrolls sideways rather than
wrapping, and a phone shows roughly 35 monospace columns. A flat four-row version
with the full status wording ran to **62 columns**, which pushed the trend — the
one column you would actually scan — off the right edge on mobile. The grouped
form is **35**, and both a code comment and a test pin the ceiling at 40.

Three changes buy that width back:

| Change | Saves |
|---|---|
| Headline/Core become group headings instead of a prefix on every row | `CPI` on 4 rows |
| Status wording shortens — Accel, Uptick, Flat, Cooling, Cooled | ~8 chars per row |
| The period moves to its own column instead of repeating inside the label | ~4 chars per row |

The long wording is still what `status_for()` returns by default. `short=True` is
for the table alone, so any future non-tabular rendering keeps the full labels.

A compact field below carries the index levels, the base period and a link to the
full release; the title itself links to `cpi.nr0.htm`.

**The markers carry `U+FE0E`.** The variation selector forces text presentation,
so `▲︎`, `▼︎` and `⏸︎` are all exactly one column wide. Without it `⏸` renders as a
double-width emoji and the Status column drifts.

**Status is derived from the rounded one-decimal rates**, compared in integer
tenths:

| `diff_tenths` | Label |
|---|---|
| `>= 2` | ▲ Accelerated |
| `== 1` | ▲ Slight uptick |
| `== 0` | ⏸ In line with prior |
| `== -1` | ▼ Slight cooldown |
| `<= -2` | ▼ Cooled |

**The tenths are not fussiness.** `0.6 - 0.4` is `0.19999999999999998` in binary
floating point, so a direct `diff >= 0.2` test fails on precisely the boundary the
rule is written around — headline MoM against a +0.4% prior would read as a
slight uptick instead of an acceleration. A regression test asserts both that
`0.6 - 0.4 < 0.2` and that the status still comes out right.

Colours follow the series: gold for CPI, purple for Core PCE, blue for GDP. A
test post overrides all three with grey.

#### Where the numbers come from

`rate_metrics()` is a pure function over the observations, so the arithmetic is
unit-tested against **real CPI and GDP observations** rather than synthetic ones
— the expected rates were computed by hand from those same values and
cross-checked against a live pull.

It returns `None` rather than a partial figure when there is not enough history
to state every rate: a rate needs the latest pair *and* both of their year-ago
counterparts. A half-derivable number is worse than none on an alert someone
trades off.

**Lookups are by calendar date, not by position.** `iloc[-1 - months]` is only
correct on a gapless series, and `CPIAUCSL` is not one: **October 2025 is missing
from every CPI series**, so the 13th-from-last observation is August 2025 —
thirteen months back, not twelve. That overstated year-on-year inflation by
0.31pp (4.02% against the correct 3.72%) and core by 0.22pp, which is exactly the
kind of gap a side-by-side comparison against the BLS release surfaces.

`value_months_back()` walks the index until it finds the month the value is meant
to represent, and returns `None` when that month is genuinely absent rather than
quietly reaching one month further back. `PCEPILFE` and `GDPC1` are gapless, so
for those two the two approaches agree exactly — the bug never touched them.

Every offline test had passed a synthetic, gap-free series, which is why this
went unnoticed until the numbers were checked against the actual BLS release.

#### Footer

`Source: {agency} via FRED • Released {date}` — BLS for CPI, BEA for PCE and GDP.

The release date comes from FRED's own `last_updated` metadata. It is **not** the
observation period: September 2026 CPI was released on Oct 14, and the alert says
so. The same metadata supplies the units string (`Index 1982-1984=100`,
`Billions of Chained 2017 Dollars`), so base periods are read from FRED rather
than hardcoded; each series carries a fallback in case that lookup fails.

Metadata is fetched **only when an alert is actually posting**, so the 15-minute
poll does not pay for a lookup it will not use.

Release windows are still computed and **logged** (05:25-06:30 PT for CPI/PCE/GDP,
10:55-11:45 PT for FOMC — see §5.7) but no longer appear in the embed. The actual
release date is strictly more informative than which window it landed in.

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

### 5.9 `/earnings <ticker>` — `cogs/market_commands.py`

An on-demand earnings lookup, independent of the watch list — you can look up
any ticker without tracking it.

| Field | Source |
|---|---|
| Next report date | `earnings_calendar`, 200-day lookahead |
| Session timing | `hour` → `Pre-Market (~05:30-06:30 PT)` / `Post-Market (~13:00-14:00 PT)` |
| Consensus EPS and revenue | `epsEstimate`, `revenueEstimate` |
| Last reported quarter | `company_earnings`, newest first |
| Surprise | `surprisePercent`, green embed for a beat, red for a miss |

**The next report is sorted, not taken from the first row.** Finnhub returns
`earnings_calendar` entries *furthest-out first* — a 200-day window for MSFT came
back as April 2027, January 2027, then October 2026 — so the command takes the
minimum date rather than trusting the ordering. Reading `[0]` would have
announced a report eight months away as "next".

**Session timing may be blank.** `hour` is frequently `""` for a report whose
timing has not been confirmed yet, so the embed says the session is unconfirmed
rather than guessing.

**A company with no records at all** gets a friendly "Finnhub has no earnings
records for `X`" rather than an empty embed.

`/earnings` and `/stock` are deliberately separate: one is a calendar lookup, the
other a price quote, and neither depends on the watch list or on each other.

### 5.10 Stock chart — `charting.py` + `StockChartView`

`/stock` attaches a price chart and five timeframe buttons.

| Button | yfinance period | interval | Extended hours | Tick labels |
|---|---|---|---|---|
| `1D` | `5d`, trimmed to one session | `2m` | **yes** | `%H:%M` |
| `1W` | `5d` | `5m` | **yes** | `%a`, one per day |
| `1M` | `1mo` | `1h` | no | `%b %d` |
| `3M` | `3mo` | `1h` (falls back to `1d`) | no | `%b %d` |
| `1Y` | `1y` | `1d` | no | `%b '%y` |

The brief asked for `interval="10m"` on 1W. **`10m` is not a valid Yahoo
interval** — it raises `Invalid input` at fetch time. `5m` is used instead, as
the closer match to the intended density of the two options that exist (5m gives
~630 bars; 15m gives ~210).

#### Extended hours

**1D and 1W include pre-market and after-hours trading** (`prepost=True`); the
longer timeframes do not. Verified live: a 5-day extended pull returns **597
pre-market and 115 after-hours bars** against 975 regular-hours bars in the same
window.

**1D is fetched over `5d`, then trimmed to the latest session.** `period="1d"`
with extended hours starts at 04:00, so during the pre-market it returns only a
few minutes of data, and just after 20:00 it would span two partial sessions.
Fetching five days and keeping the bars dated the same as the last one yields
exactly one session — and leaves the previous close available, which is what the
return is measured from.

Early in the pre-market the 1D chart is genuinely short: at 04:10 ET it covers
ten minutes. That is not a bug — there is nothing else to draw yet, and
Robinhood shows the same window.

**1W ticks are anchored to each session**, not evenly spaced, so a week reads as
Fri / Mon / Tue / Wed / Thu. If a window ever held more than seven sessions the
labels would collide, so it falls back to even spacing.

#### Where the return starts from

| Timeframe | Baseline | Why |
|---|---|---|
| `1D` | **previous regular-session close** | Robinhood's daily change is measured from it |
| `1W`–`1Y` | first bar of the range | also what Robinhood does |

The distinction matters. With extended hours on, the first bar of the day is the
**04:00 pre-market**, not yesterday's close. Measuring from it understated AAPL's
move by 0.10pp when this was written — `+1.29%` instead of `+1.39%`. Small, but
precisely the kind of gap a side-by-side comparison against Robinhood catches.

`previous_session_close()` takes the last bar at or before **16:00 ET** on an
earlier day, so the 20:00 end of after-hours is never mistaken for the official
close. It is derived from the frame already fetched, so it costs no extra
request, and returns `None` rather than inventing a number when there is no
earlier session.

#### Continuous trading-period axis

The x-axis is **bar position**, not timestamps. Plotting against a datetime scale
draws a straight diagonal across every market closure — a line that never
happened. A 1Y daily series has ~55 such gaps; 3M hourly has ~13.

`x = np.arange(len(df))`, so a weekend costs one bar of horizontal space instead
of two days of it. A price step reads as a near-vertical jump rather than a long
shallow slope.

Ticks are five evenly spaced integer positions across `range(len(df))`, labelled
from the real index at those positions. `tick_positions()` is a pure function and
is unit-tested. A datetime axis does the opposite — ticks evenly spaced in
*time* — which bunches labels wherever the data is dense.

#### Why 1M and 3M are hourly

A month of daily closes is ~21 points: a zigzag with each day's intraday range
invisible. A month of hourly bars is ~154 and reads as a curve. Likewise 3M is 65
daily points versus 455 hourly. Both verified against live data — 7 bars per
trading day, no missing days, so the finer interval costs nothing in fidelity.

**3M falls back to daily bars.** Yahoo has no hourly history this far back for
some symbols, and none at all for thinly traded ones. On a `ChartUnavailable` the
fetch retries at `1d` rather than failing — daily closes beat no chart. The
button label and tick format still come from the `3M` definition, so the title
stays right through the fallback.

#### Headless rendering

`matplotlib.use("Agg")` runs **before** `import matplotlib.pyplot`. A headless
Linux host has no display server, and importing pyplot with a GUI backend
selected raises at import time — which would stop the bot from starting at all,
not merely break the chart. `verify.py` asserts the backend resolves to `agg`.

#### What it draws

- Background `#2f3136` — Discord's own surface, so the image sits flush in an embed.
- Line `#00ff88` when the latest close is at or above the period's **opening**
  price, `#ff3344` otherwise.
- A vertical fade beneath the line, strongest just under it.
- Dashed grid at `alpha=0.2`, no spines, quiet grey ticks.
- A left-aligned title: `SYMBOL · TIMEFRAME` above price and change.

#### Three things that were not obvious

**The currency symbol has to be escaped.** matplotlib reads a *pair* of `$` as
mathtext, so `$342.96 (+12.82, +3.88%)` renders as italic maths. The subtitle is
escaped to `\$` before it reaches the title.

**Stacked gradient bands leave seams.** The first attempt approximated the fade
with 14 horizontal `fill_between` bands. Each band boundary is an alpha step, and
wherever the curve rises above that boundary the step shows as a hard horizontal
line — glaring on a 1M chart, where the line has only ~22 points. It is now a
single `imshow` ramp clipped to the polygon under the curve.

**pyplot keeps global figure state.** Two renders on different threads can tread
on each other, so rendering is serialised behind a module lock. Charts are small
and infrequent; the lock costs nothing.

#### Request cost and caching

Every button click is a Yahoo request, so rendered PNGs are cached for **45
seconds**, keyed on (symbol, period, interval) and capped at 64 entries. Flicking
through all four timeframes twice costs four requests, not eight.

#### Interaction

`show_timeframe()` calls `await interaction.response.defer()` **before** anything
slow. Discord allows three seconds to acknowledge a component interaction, and a
render plus a Yahoo round trip will often exceed that. It then renders off the
event loop via `run_blocking`, and finishes with
`edit_original_response(embed=..., attachments=[file], view=self)`.

The active timeframe is `ButtonStyle.primary`; the rest are `secondary`. The view
times out after **180 seconds**, and `on_timeout` greys the buttons out so they
do not keep looking clickable after they have stopped working.

**If the chart fails the quote still posts.** Chart generation is wrapped so a
render or Yahoo failure costs the image, not the command.

**Button access is open** — anyone who can see the message can change the
timeframe. Fine for public market data, and consistent with the rest of the bot.

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
| Briefing's macro section says "Unavailable" | FRED's release calendar could not be reached | Check `FRED_API_KEY` and the logs — this is **not** the same as a week with nothing scheduled |
| No FOMC line in the macro section | FOMC is not sourced at all; FRED cannot date a rate decision | Expected — see §5.3 |
| `/earnings` says the session is unconfirmed | Finnhub has no `hour` for that report yet | Normal for a report more than a few weeks out |
| A high-interest name is missing from the briefing | It has no entry in that week's Finnhub calendar | `/earnings <ticker>` shows its actual date |

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

### 2026-10-11 19:03 PT — Briefing filtering, macro calendar, and `/earnings`

**Changed — the briefing listing is now strictly curated.** Only the 37
high-interest names (⭐) and tickers tracked with `/track_earnings` are listed.
The previous rule also admitted any unstarred company with an analyst estimate,
which still produced dozens of lines a day. Because the filter is now this
narrow, the per-day line cap and the weekly total are **gone** — they were only
ever needed to ration a listing that is no longer large enough to ration. A
1,000-character field ceiling remains as a backstop.

**Added — `📅 Macro Economic Calendar This Week`**, a tier-1 US release calendar
for the coming week with each date and time in Pacific. See §5.3.

**Added — `/earnings <ticker>`**, an on-demand lookup for a company's next
report and last reported quarter. See §5.9.

**Modified**
- `cogs/scheduled_briefings.py` — filter replaced with `_is_listed()`; added the
  FRED release-calendar fetch, `tier1_match()`, `_macro_field()`, and
  `TIER1_RELEASES`.
- `cogs/market_commands.py` — added `/earnings` plus its two Finnhub fetchers and
  two text builders.
- `cogs/earnings_tracker.py` — `_load_watchlist()` made public as
  `load_watchlist()`, so the briefing reads the same file rather than duplicating
  the path and format.
- `utils.py` — `timing_label()` and `TIMING_WINDOWS` moved here from
  `scheduled_briefings`, since both the briefing and `/earnings` need them;
  added `parse_iso_date()`.
- `verify.py` — grown from 92 to **120 checks**.

**Findings — three data-source surprises, all caught before shipping**
1. **Finnhub's `/calendar/economic` returns HTTP 403.** It is premium-only; the
   free-tier key cannot use it. This was the natural source and it carries real
   release times, so the macro section had to be rebuilt on FRED instead.
2. **FRED cannot supply FOMC rate decisions.** Its only FOMC release,
   `FOMC Press Release`, carries a date for *every day of the year* — 365 in 2026
   — because it also covers routine speeches. `DFEDTARU` and the other target
   series resolve to that same release, and no FRED endpoint reveals a *future*
   meeting day. **FOMC is therefore not in the section**, and inventing meeting
   dates was not an option. It needs a dated calendar, not a data feed.
3. **Finnhub returns `earnings_calendar` entries furthest-out first.** A 200-day
   window for MSFT came back April 2027 → January 2027 → October 2026. Reading
   `[0]` would have reported a report eight months away as the next one;
   `/earnings` takes the minimum date instead.

**Verified**
- `python verify.py` — **120/120 checks passed**, exit 0.
- Tier-1 matching asserted to be **exact**: `Sticky Price CPI`,
  `Gross Domestic Product by State`, `Trimmed Mean PCE Inflation Rate` and
  `Employment Situation Summary` are all rejected, and `Producer Price Index` is
  ignored.
- The macro line is asserted as an **exact string** against the format in the
  brief: `Tuesday, Oct 13 · CPI Inflation · 05:30 PT`.
- An **empty week and a failed fetch are asserted to differ** — "No major tier-1
  macro releases" versus "Unavailable".
- `/earnings` text builders checked against real Finnhub values: `$2.02` /
  `$115.33B` consensus, `-0.89%` surprise, and the unconfirmed-session path.
- Filter checked in both directions: a starred ticker is listed, an unstarred
  untracked ticker is not, and the same ticker **is** listed once passed in the
  tracked set — proving tracking does not open the door to others.
- A 120-entry day asserted to stay inside Discord's 1,024-character field cap and
  to report what it dropped.
- **Live checks, three read-only calls** (no Discord, no writes): Finnhub
  `/calendar/economic` (403), Finnhub `company_earnings` and `earnings_calendar`
  (both fine), FRED `/releases/dates` across 2026 (CPI 12 dates, Employment
  Situation 12 dates, FOMC Press Release 365 dates). The rendered briefing was
  then re-run against the live FRED calendar and produced exactly one tier-1
  event — `Wednesday, Oct 14 · CPI Inflation · 05:30 PT`, which matches FRED's
  own CPI schedule for October.
- `python -m compileall` — exit 0.

**Notes**
- The tier-1 list is now **four** releases, not the five requested. The missing
  one is FOMC, for the reason above. Everything needed to add it back is a list
  of meeting dates.
- The macro section's times are **conventions, not API data** — 08:30 ET for BLS
  and BEA releases. If an agency ever shifts its publication time the label will
  be wrong until `TIER1_RELEASES` is updated.
- `/test_briefing` renders the macro section unchanged, because it calls the same
  `_build_embed`; this is asserted rather than assumed.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Market snapshot symbols resolving | 17/17 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 120/120 |

### 2026-10-12 10:26 PT — Duplicate-suppression tests and a user-facing guide

**Added — `COMMANDS.md`**, a short reference for anyone in the server: the six
on-demand commands, the three automatic posts and their cadence, the two test
commands, and a plain-language statement of the no-spam guarantees.

**Added — "duplicate suppression" to `verify.py`.** The posting-frequency
question had only ever been reasoned about by reading the code. These tests
drive the real poll loop and count messages, with the on-disk caches redirected
to a temp directory so the live state is untouched:

| Assertion | Result |
|---|---|
| A first run seeds instead of announcing history | 0 messages |
| A new period posts | one per series that published |
| Three further polls over the same release | **0 additional messages** |
| A restart does not replay an announced release | 0 messages |
| A tracked result is posted once, not on every check | 1 message after 3 checks |
| A restart does not repeat a tracked earnings alert | 0 messages |
| Exactly three background loops exist, none on a short interval | pass |

**Verified**
- `python verify.py` — **128/128 checks passed**, exit 0.

**Note on a failing first run.** The new test initially reported 3 messages
where it expected 1, which looked like a duplicate-alert bug. It was the *test*
that was wrong: there are three monitored series, and the stub returned the same
observation for all three, so all three correctly fired. The assertion now reads
"one per series that published". Worth recording because the failure mode looked
convincing.

**Confirming the original question.** Polling every 15 minutes does **not** mean
posting every 15 minutes. `_check_for_releases` compares each series' newest
observation date against `state/macro_state.json` and only enqueues a post when
the date is *strictly newer*; the cache advances only after a send succeeds, so
a failed post retries rather than being lost. On a day when CPI publishes, the
channel gets exactly one CPI message, and nothing further however many times the
bot polls afterwards.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Market snapshot symbols resolving | 17/17 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 128/128 |

### 2026-10-15 00:38 PT — Charts on `/stock`

**Added**
- `charting.py` — `generate_stock_chart(symbol, period, interval) -> io.BytesIO`
  and `render_chart()`, the pure drawing half, split out so it can be exercised
  offline. See §5.10.
- `StockChartView` and `TimeframeButton` in `cogs/market_commands.py` — four
  timeframe buttons, 180-second timeout, active button highlighted.
- `matplotlib>=3.9.0` in `requirements.txt` (resolved to 3.11.2).

**Modified**
- `cogs/market_commands.py` — `/stock` now attaches a chart and the button view,
  and the quote embed gained an **Open** field.
- `verify.py` — grown from 128 to **152 checks**.

**Two corrections to the brief**
1. **`/stock` does not use Finnhub.** It reads quotes from `yfinance` (Yahoo);
   Finnhub is used only by `/earnings`. Confirmed by reading the code before
   starting. Per the answer given, quotes stay on yfinance — Yahoo supplies the
   company name and market cap the embed shows, neither of which Finnhub's free
   `/quote` returns.
2. **There was no Open field.** The brief listed "(Open, High, Low, Previous
   Close, Volume)" as existing fields; the embed actually had Price, Daily
   change, Previous close, Day range, Volume, Market cap. High and Low are shown
   as a combined "Day range", and Open was genuinely missing, so it was added.
   A test now pins the exact field list so nothing can be silently dropped.

**Verified**
- `python verify.py` — **152/152 checks passed**, exit 0.
- **The PNG is really a PNG**: the buffer's first eight bytes are checked against
  the PNG magic number, and the buffer is asserted to come back rewound to index
  0 (a buffer left at the end would upload as an empty file).
- **The line colour is checked by reading pixels**, not by trusting a variable.
  The rendered image is decoded and clearly-green pixels are counted against
  clearly-red ones: an up period must be green-dominant, a down period
  red-dominant.
- Degenerate inputs are asserted to raise `ChartUnavailable` rather than render
  something broken: empty history, a frame with no `Close` column, and a
  single-point series. A perfectly flat series is asserted to still render.
- The view is checked for four buttons, the right labels, exactly one
  `primary`/three `secondary`, a 180-second timeout, that the highlight follows
  a selection, and that `chart_embed()` points at `attachment://chart.png` while
  the base embed stays imageless so redraws start clean.
- **Live check, all twelve combinations** (AAPL, `^TNX`, BTC-USD × four
  timeframes) fetched real history and rendered real PNGs — 53–72 KB each, well
  under Discord's 8 MB attachment limit. Everything the offline suite cannot
  reach.

**Found by looking at the output, not by a test**
- The first gradient implementation stacked 14 `fill_between` bands. On a
  synthetic 200-point series it looked fine. Rendered against **real** AAPL 1M
  data — 22 points — it showed unmistakable horizontal seams under every peak,
  because each band boundary is an alpha step. Replaced with a single `imshow`
  ramp clipped to the polygon; confirmed visually against the same live data.
  Worth recording because the offline tests passed both versions.

**Notes**
- Chart failures are non-fatal: `/stock` still posts the quote, without an image.
- Rendered charts are cached for 45 seconds, so flicking through the timeframes
  does not burn a Yahoo request per click.
- `matplotlib` needs no system libraries on Linux (the manylinux wheel bundles
  what it needs) and needs no display with Agg, so the Procfile deployment is
  unchanged — the build is simply larger and slower.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Market snapshot symbols resolving | 17/17 |
| Chart timeframes resolved live | 12/12 |
| Headless matplotlib backend | Agg |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 152/152 |

### 2026-10-15 00:56 PT — Continuous chart axis, finer bars, and a 3M timeframe

**Fixed — market closures were drawn as lines that never happened.**
The chart plotted `Close` against a datetime index, so matplotlib connected
Friday's close to Monday's open with a straight diagonal across the weekend.
This was not a rare edge case: a 1Y daily series has **~55** such gaps, 3M hourly
has ~13, and 1M hourly has 4.

The x-axis is now **bar position** (`np.arange(len(values))`), so a closure costs
one bar of horizontal space rather than two days of it.

**Changed — 1M and 3M now fetch hourly bars.** A month of daily closes is ~21
points, a zigzag with every day's intraday range erased. Hourly gives ~154; 3M
goes from 65 points to ~455. Verified against live data that the hourly series is
complete — 7 bars per trading day, no missing days — so the finer interval does
not buy detail at the cost of holes.

**Added — a `3M` button**, between `1M` and `1Y`, taking the view to five. 3M
declares `fallback_interval="1d"`: if Yahoo has no hourly history that far back,
or none at all for a thinly traded symbol, the fetch retries at daily rather than
failing. The label and tick format still come from the `3M` definition, so the
title reads `3M` even when the bars behind it are daily.

**Changed — tick labels are chosen per timeframe** rather than by matplotlib's
date locator: `%H:%M` for 1D, `%b %d` for 1W/1M/3M, `%b '%y` for 1Y. Five evenly
spaced bar positions, ends included.

**Added — `verify.py --live`.** The suite stays offline by default; the flag adds
a section that fetches and renders all five timeframes against real Yahoo data.
A network-dependent default would make the gate fail on a plane or behind a rate
limit, where a red result says nothing about the code.

**Verified**
- `python verify.py` — **163/163 checks passed**, exit 0.
- `python verify.py --live` — **168/168**, with all five timeframes fetching and
  rendering live: 1D 61 KB, 1W 66 KB, 1M 75 KB, 3M 72 KB, 1Y 68 KB.
- **The gap fix is asserted, not eyeballed.** A synthetic 400-bar series with a
  five-day hole and a price step is rendered, and the image is decoded to measure
  how far the line travels vertically *within a single pixel column*. A
  near-vertical step does the whole move in one column (~330px), a diagonal does a
  sliver per column across hundreds (~5px). The check requires >100px, so it
  fails if anyone reintroduces a datetime axis.
- `tick_positions()` unit-tested: 4–6 ticks, spanning first bar to last, integer,
  evenly spaced within rounding, one-per-bar for short series, none for empty.
- Timeframe table asserted: five buttons in the order `1D 1W 1M 3M 1Y`, 1M and 3M
  both `1h`, only 3M declaring a fallback, every timeframe declaring a tick
  format.
- View asserted to hold five buttons, `primary` for the active one and
  `secondary` for the other four, moving correctly when the selection changes.
- Rendered all three real timeframes (1M/3M/1Y AAPL) and inspected them.

**One thing that looked like a bug and was not.** The 3M chart showed a near
vertical drop to ~301 just past the Aug 06 label. Rather than assume, I checked
Yahoo's raw data for holes: the hourly series is perfectly regular (7 bars every
trading day, 65/65 days), and the daily closes show AAPL going **333.14 on Jul 30
to 308.64 on Jul 31** — a real ~7% earnings move, faithfully drawn. Worth
recording because a genuine ~7% single-day move looks exactly like a rendering
fault.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Chart timeframes resolved live | 5/5 |
| Headless matplotlib backend | Agg |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 163/163 (168/168 with `--live`) |

### 2026-10-15 01:12 PT — Extended hours and Robinhood's return basis

**Added — extended hours on 1D and 1W** (`prepost=True`). Pre-market and
after-hours bars are now on the chart; 1M/3M/1Y stay regular-session only.

**Added — 1W day ticks.** One tick per trading session, anchored to the first bar
of each day, labelled `%a` — so a week reads Fri / Mon / Tue / Wed / Thu instead
of five evenly spaced dates that land mid-session.

**Changed — 1D measures its return from the previous close**, not from the first
bar of the period as the brief specified. With extended hours on, the first bar
is the 04:00 pre-market, so the two differ — see below.

**Changed — 1D is fetched over `5d` and trimmed to the latest session.**
`period="1d"` plus extended hours returns only a few minutes of data during the
pre-market, and would span two partial sessions just after 20:00.

**Two corrections to the brief**
1. **`interval="10m"` does not exist.** Yahoo accepts `1m, 2m, 5m, 15m, 30m,
   60m, 90m, 1h, 4h, 1d, 5d, 1wk, 1mo, 3mo` — anything else raises
   `Invalid input` at fetch time. 1W uses `5m`. A test now pins every configured
   interval against that list, so the trap cannot be walked into again.
2. **The return baseline as specified would not have matched Robinhood.** The
   brief said to measure from the first bar of the period; with extended hours
   that bar is the 04:00 pre-market, not the previous close. Measured on AAPL:
   `+1.29%` from the first bar versus `+1.39%` from the previous close — a 0.10pp
   gap, every day. Confirmed with the user and switched to the previous close for
   1D; 1W–1Y keep the first-bar basis, which is also what Robinhood uses.

**Verified**
- `python verify.py` — **175/175 checks passed**, exit 0.
- `python verify.py --live` — **183/183**: all five timeframes fetch and render,
  and the extended-hours premise is checked against real data rather than
  assumed — **597 pre-market and 115 after-hours bars**, with regular hours
  confirmed as a strict subset (975 vs 1492 bars).
- `previous_session_close()` asserted to pick the **15:58 regular bar at
  341.27**, not the 19:58 after-hours bar at 341.90, and to return `None` when
  there is no earlier session rather than inventing a number.
- The baseline is asserted to actually *drive* the render, not merely be
  accepted: the same frame drawn against a baseline of 100 comes out
  green-dominant and against 400 comes out red-dominant.
- `trim_to_last_session()` asserted to keep only the latest day's bars.
- `day_start_positions()` asserted to land on the first bar of each session.
- Every configured interval asserted against Yahoo's accepted list.
- Rendered 1D and 1W from live data and inspected them. 1W shows the five day
  ticks and continuous extended-hours trading; 1D at 04:10 ET shows ten minutes
  of pre-market with the return measured from the previous close.

**Note worth keeping.** At 04:10 ET the 1D chart is ten minutes long. That is
not a defect — the session has only just started, there is nothing else to draw,
and Robinhood shows the same window. It resolves itself by mid-morning.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Chart timeframes resolved live | 5/5 |
| Extended-hours bars confirmed live | 597 pre-market + 115 after-hours |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 175/175 (183/183 with `--live`) |

### 2026-10-16 10:20 PT — Macro alerts now lead with rates of change

**Changed — the alerts report rates, not index levels.** A CPI alert reading
"336.30" told a reader nothing; the market trades the rate of change. Every field
is now a rate, and the index level is demoted to context. See §5.4.

| Series | Before | After |
|---|---|---|
| CPI | 336.30, previous 334.13 | **+0.65% MoM** (vs +0.40%), **+4.02% YoY** (vs +3.71%) |
| Core PCE | 130.46, previous 130.13 | **+0.25% MoM** (vs +0.13%), **+3.01% YoY** (vs +2.98%) |
| Real GDP | $24,408.01B | **+2.2% SAAR** (vs +2.5%), **+2.19% YoY** |

Each headline carries the prior period's rate beside it, so acceleration and
deceleration are readable without a second lookup.

**Added — series-specific titles and colours**: `📊 US Consumer Price Index (CPI)`
in gold, `📊 US Core PCE Price Index (Fed's Preferred Gauge)` in purple, and
`📈 US Real Gross Domestic Product (GDP)` in blue. Test posts override all three
with grey, and the `[TEST]` tag goes in the title without touching the fields.

**Added — `rate_metrics()`**, a pure function so the arithmetic is unit-testable
offline. It returns `None` rather than a partial figure when there is not enough
history — 14 monthly or 6 quarterly observations.

**Changed — the footer is now `Source: {agency} via FRED • Released {date}`**,
with BLS for CPI and BEA for PCE and GDP.

**Verified**
- `python verify.py` — **212/212 checks passed**, exit 0.
- `python verify.py --live` — **220/220**.
- The rates are asserted against **real observations**, not synthetic ones: CPI
  from the actual last 15 `CPIAUCSL` prints must give exactly +0.65% MoM, +0.40%
  prior month, +4.02% YoY, +3.71% prior year; GDP from the real `GDPC1` quarters
  must annualise to +2.2% with a +2.5% prior quarter. Those expected values were
  worked out by hand from the same numbers and cross-checked against a live pull.
- Field names, titles, colours and footer attribution asserted per series, plus
  that a test post's fields are **byte-identical** to a real one.
- Rendered all three embeds from live FRED and read them. The release dates came
  back as **Oct 14 for CPI and Sep 30 for PCE and GDP** — and the Oct 14 CPI date
  independently corroborates the macro calendar built three sessions ago, which
  had predicted `Wednesday, Oct 14 · CPI Inflation · 05:30 PT`.

**A real bug the offline suite could not have caught**
- `fredapi.get_series_info()` returns a **pandas Series, not a dict**. The embed
  did `(info or {}).get("units")`, and a Series has no truth value, so this
  raised `ValueError: The truth value of a Series is ambiguous` — but only on a
  live FRED call. Every offline test passed a dict, so every offline test passed.
  Caught the first time the preview ran against real FRED.
- Fixed with `as_mapping()`, which normalises whatever comes back, and there is
  now a regression test that feeds a real `pd.Series` through both
  `release_date()` and the embed builder.

**Note on `Released` vs the period.** The footer date is when FRED published the
series, from `last_updated` — *not* the observation period. September 2026 CPI
was released on Oct 14; labelling the observation date as "Released" would have
put "Sep 1" on a number that came out six weeks later.

**Note on the release window.** It no longer appears in the embed; the footer now
carries the agency and the actual release date, which is strictly more
informative than which window it landed in. The window is still computed and
written to the log when an alert posts.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Macro alerts rendered from live FRED | 3/3, rates matching hand-checked values |
| Chart timeframes resolved live | 5/5 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 212/212 (220/220 with `--live`) |

### 2026-10-16 11:44 PT — CPI alerts rebuilt as a table, on date-based indexing

**Fixed — a real off-by-one, from a cause nobody had guessed.**
`CPIAUCSL` is missing **October 2025**. The index arithmetic was right —
`iloc[-1 - 12]` really is `iloc[-13]` — but on a series with a hole, position
`-13` lands **13 months back**, not 12. That overstated headline year-on-year
inflation by **0.31pp** (4.02% against the correct 3.72%) and core by **0.22pp**.

`value_months_back()` now finds the year-ago point by matching the calendar month
it is supposed to represent, and returns `None` when that month is genuinely
absent rather than quietly reaching one month further back.

**`PCEPILFE` and `GDPC1` were never affected** — both are gapless, and for them
the positional and date-based lookups agree to 0.00pp. The gap is unique to the
CPI family: `CPIAUCSL`, `CPILFESL`, `CPIAUCNS` and `CPILFENS` all have it.

**The SA-vs-NSA hypothesis was not the cause.** Both give 3.7% once the indexing
is right. It does matter for MoM though, where the BLS convention holds: core MoM
from the seasonally adjusted series is 0.3% ✓, from the not seasonally adjusted
series it is 0.4% ✗.

**Added — Core CPI.** `CPILFESL` (MoM) and `CPILFENS` (YoY) join `CPIAUCSL` /
`CPIAUCNS`, so one alert carries headline and core together — they come from the
same BLS release and would otherwise fire two messages in the same second.

**Changed — the CPI body is a monospace table**, with the index levels and a link
to the full release in a compact field below, and the title itself linking to
`cpi.nr0.htm`. See §5.4.

**Added — deterministic status labels**, from the rounded one-decimal rates:
`▲ Accelerated` / `▲ Slight uptick` / `⏸ In line with prior` / `▼ Slight cooldown`
/ `▼ Cooled`.

**A floating-point trap in that rule.** `0.6 - 0.4` is `0.19999999999999998`, so a
naive `diff >= 0.2` test fails on exactly the boundary the rule exists for —
headline MoM against a +0.4% prior would be labelled a slight uptick instead of an
acceleration. `status_for()` compares `round(actual*10) - round(prior*10)` in
integer tenths.

**Corrected in the request's own spec.** It stated `diff >= +0.2 → Accelerated`
and then labelled two `+0.2` rows differently — headline MoM as *Accelerated*,
core YoY as *Slight uptick*. Same diff, two labels. The written rule was followed.

**Verified**
- `python verify.py` — **236/236 checks passed**, exit 0.
- `python verify.py --live` — **244/244**.
- **The gap is a regression test.** A synthetic `CPIAUCSL` carrying the real
  October 2025 hole must give 3.7% YoY and 3.4% prior-year — the published BLS
  figures — and the test asserts in the same breath that `iloc[-13]` gives 4.0%,
  so the contrast is pinned rather than described.
- A series with the **year-ago month itself missing** must return `None`, not
  silently reach a month further back.
- `0.6 - 0.4 < 0.2` is asserted as a fact, next to the status it must still
  produce — so anyone "simplifying" the tenths back to floats breaks a test that
  explains why.
- The full status ladder covered: `+0.3`, `+0.2`, `+0.1`, `0`, `-0.1`, `-0.2`.
- The CPI embed asserted for field names, title, title URL, fenced code block,
  all four metric rows **in BLS order**, every rate present, three Accelerated
  and one In line, colour `#f1c40f`, footer attribution, both index levels, the
  base period, and the release link. Plus that a test post is byte-identical
  apart from its tag and colour.
- `PCEPILFE` and `GDPC1` asserted **unchanged** — same three fields, same layout,
  and no release link (only CPI declares one).
- Rendered all three alerts from live FRED and read them: the CPI table came back
  with 3.7% / 3.4% / +0.6% / +0.4% / 2.6% / 2.4% / +0.3% / +0.3%, matching the
  BLS release exactly, with all four status values aligned at the same column.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Macro alerts rendered from live FRED | 3/3, CPI matching the BLS release to the decimal |
| Chart timeframes resolved live | 5/5 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 236/236 (244/244 with `--live`) |

### 2026-10-16 12:20 PT — CPI table regrouped to fit a phone

**Changed — the glance table is grouped by measure, and nearly half as wide.**

```
Metric     Actual  Prior   Trend          Metric                Actual   Prior    Status
Headline                                 Headline CPI (YoY)      3.7%    3.4%    ▲︎ Accelerated
  YoY        3.7%   3.4%   ▲︎ Accel        Headline CPI (MoM)     +0.6%   +0.4%    ▲︎ Accelerated
  MoM       +0.6%  +0.4%   ▲︎ Accel   →    Core CPI (YoY)          2.6%    2.4%    ▲︎ Accelerated
Core                                     Core CPI (MoM)         +0.3%   +0.3%    ⏸︎ In line with prior
  YoY        2.6%   2.4%   ▲︎ Accel
  MoM       +0.3%  +0.3%   ⏸︎ Flat                  35 columns        62 columns
```

**Why.** A code block scrolls sideways instead of wrapping, and a phone shows
about 35 monospace columns. At 62, the trend — the column you would scan for —
was the first thing off the right edge. Headline and core became group headings
instead of a prefix on every row, and the status wording gained short forms.

**The long labels are unchanged.** `status_for()` still returns `▲ Accelerated`,
`▼ Slight cooldown` and so on by default; `short=True` is used only by the table,
so any future non-tabular rendering keeps the full wording.

**Verified**
- `python verify.py` — **241/241 checks passed**, exit 0.
- `python verify.py --live` — **249/249**.
- The table's width is now asserted: `widest <= 40`, reported as *"widest row is
  35 columns, down from 62"*, so a future addition that widens it fails with the
  reason attached rather than silently pushing the trend off-screen.
- Asserted the table **never contains "CPI"** — the title and the measure
  headings already say it.
- Asserted the row order is Headline → YoY → MoM → Core → YoY → MoM, and that
  all four metric rows still carry the published figures.
- Asserted every short status fits the trend column (≤ 10 characters) and that
  the long wording remains the default.
- Rendered from live FRED: 35 columns, field value 197 of 1,024 characters.

**Note on the test that failed first.** Two checks reported failures against a
table that was rendering correctly — the *test* was wrong. `"  YoY    3.7%…".strip()`
is the whole row, not `"YoY"`, so only the group headings matched the filter. The
row parser now reads the leading token. Worth recording because it looked like a
layout bug and was not.

**Updated final state**

| Check | Result |
|---|---|
| Cogs load offline | 4/4 |
| Slash commands registered | 8 |
| Background loops | `weekly_briefing` Mon 08:00 ET · `poll_macro_releases` every 15 min · `daily_check` daily 17:30 ET |
| Tier-1 macro releases sourced | 4 of 5 (FOMC not sourceable from FRED) |
| Macro alerts rendered from live FRED | 3/3, CPI matching the BLS release to the decimal |
| CPI table width | 35 columns (ceiling 40, was 62) |
| Chart timeframes resolved live | 5/5 |
| Tracked files containing a secret | 0 |
| Byte-compile | Clean |
| Offline check suite | 241/241 (249/249 with `--live`) |
