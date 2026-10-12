# Bot Commands

A quick reference for anyone in the server. **All times are US/Pacific.**

## On demand

Type these any time. Every one replies privately — only you see the answer.

| Command | What you get |
|---|---|
| `/stock <ticker>` | Price, daily change, day range, volume, market cap. |
| `/macro` | Fed funds rate, 10Y and 2Y Treasury yields, the 10Y–2Y spread, and headline CPI inflation year over year. |
| `/earnings <ticker>` | Next scheduled report (date + Pre-Market/Post-Market) and the last reported quarter, actual vs consensus. |
| `/track_earnings <ticker>` | Adds a ticker to the earnings watch list. |
| `/untrack_earnings <ticker>` | Removes it. |
| `/list_earnings` | Shows the current watch list. |

## Automatic posts

These arrive on their own. You don't type anything.

| What | When |
|---|---|
| **📅 Week Ahead: Earnings** | Mondays at 05:00 PT — the week's high-interest earnings, the macro calendar, and a market snapshot |
| **📊 Macro release alert** | Within ~15 minutes of CPI, Core PCE or Real GDP being published |
| **📈 Earnings result** | Daily at 14:30 PT, for tickers on the watch list that reported that day |

## Test commands

Both post a **real message into the channel**, titled `[TEST]`. They're for
checking the bot is alive, not routine use. Anyone in the server can run them.

| Command | Does |
|---|---|
| `/test_briefing` | Posts the weekly briefing now, without waiting for Monday. |
| `/test_macro_alert <series>` | Posts a sample macro alert for CPI, Core PCE or Real GDP. |

## It will not spam the channel

Every automatic post is deduplicated against a file on disk, so the same news is
never announced twice:

- **A macro alert fires once per data release.** The bot polls FRED every 15
  minutes, but it only posts when a *new* observation appears — polling
  frequency is not posting frequency. Restarting the bot does not replay what it
  already announced. If two of the three series publish at the same moment you
  get two messages, one per release.
- **The weekly briefing posts once a week.** The schedule wakes daily and stops
  immediately unless the day is Monday.
- **A tracked earnings result posts once per company per report** — not on every
  daily check, and not again after a restart.
- **A bad ticker posts nothing.** `/stock` and `/earnings` reply privately with
  an error instead of writing to the channel.

## A few things worth knowing

- **Times shown are Pacific.** The underlying schedules run on Eastern (the
  exchange clock) and are converted for display.
- **Quotes may be delayed.** Market data comes from Yahoo Finance via `yfinance`
  and is not real-time exchange data.
- **The weekly briefing only lists high-interest names and your own tracked
  tickers.** Roughly 150–250 companies report each week; listing them all would
  bury the ones that matter. Use `/track_earnings` to add anything it misses.
- **A day with nothing notable says so** rather than disappearing, so a quiet
  day is never confused with a broken one.
- **FOMC rate decisions are not in the macro calendar** — the free data source
  cannot distinguish a rate decision from a routine Fed speech.
