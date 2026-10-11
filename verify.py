"""Offline smoke test.

Loads every cog exactly the way bot.py does — but without logging in to Discord
and without calling any data provider. Nothing here touches the network, so it
is safe to run at any time.

Usage:  python verify.py
Exit code 0 means everything loaded; 1 means something is broken.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from datetime import date, datetime, timedelta

import discord
import pandas as pd
from discord.ext import commands, tasks

import bot as bot_module
import config

RESULTS: list[bool] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    print(f"[{'PASS' if passed else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    RESULTS.append(passed)


def section(title: str) -> None:
    print(f"\n-- {title} ".ljust(60, "-"))


async def main() -> int:
    print(f"Python {sys.version.split()[0]}   discord.py {discord.__version__}")

    # ---------------------------------------------------------- configuration
    section("configuration")

    check(
        "Timezone database resolves",
        config.TIMEZONE_BACKEND == "zoneinfo",
        f"backend={config.TIMEZONE_BACKEND}",
    )

    now_eastern = datetime.now(config.EASTERN)
    offset = now_eastern.utcoffset()
    offset_hours = offset.total_seconds() / 3600 if offset else None
    check(
        "US/Eastern offset is plausible (-4 or -5)",
        offset_hours in (-4.0, -5.0),
        f"{offset_hours:+.0f}h on {now_eastern:%Y-%m-%d}",
    )

    missing = config.missing_variables()
    check(
        "All four environment variables set",
        not missing,
        "all present" if not missing else f"missing {', '.join(missing)}",
    )
    check(
        "TARGET_CHANNEL_ID parses as an integer",
        config.TARGET_CHANNEL_ID is not None,
        str(config.TARGET_CHANNEL_ID),
    )

    # ------------------------------------------------------------ time display
    section("timezone display")

    pacific_summer = datetime(2026, 7, 15, 12, tzinfo=config.PACIFIC).utcoffset()
    pacific_winter = datetime(2026, 1, 15, 12, tzinfo=config.PACIFIC).utcoffset()
    check(
        "Pacific observes DST (PDT -7, PST -8)",
        pacific_summer == timedelta(hours=-7) and pacific_winter == timedelta(hours=-8),
        f"Jul={pacific_summer}, Jan={pacific_winter}",
    )

    from utils import to_pacific_clock

    check(
        "08:30 Eastern renders as 05:30 Pacific",
        to_pacific_clock(8, 30) == "05:30",
        to_pacific_clock(8, 30),
    )
    check(
        "16:00 Eastern renders as 13:00 Pacific",
        to_pacific_clock(16, 0) == "13:00",
        to_pacific_clock(16, 0),
    )
    summer_label = to_pacific_clock(8, 30, reference=date(2026, 7, 15))
    winter_label = to_pacific_clock(8, 30, reference=date(2026, 1, 15))
    check(
        "Conversion is stable across the DST boundary",
        summer_label == winter_label == "05:30",
        f"Jul={summer_label}, Jan={winter_label}",
    )

    # ----------------------------------------------------------- provider APIs
    section("provider APIs (introspection, no network)")

    import finnhub
    import yfinance as yf
    from fredapi import Fred

    check("yfinance Ticker.info exists", hasattr(yf.Ticker, "info"), f"yfinance {yf.__version__}")
    check("finnhub Client.earnings_calendar exists", hasattr(finnhub.Client, "earnings_calendar"))
    check("fredapi Fred.get_series exists", hasattr(Fred, "get_series"))

    # ------------------------------------------------------------------- cogs
    section("cogs")

    intents = discord.Intents.default()
    intents.message_content = True
    client = commands.Bot(command_prefix="!", intents=intents)

    extensions = bot_module.discover_extensions()
    print(f"discovered: {', '.join(extensions) if extensions else 'none'}")
    check("At least one cog discovered", bool(extensions))

    for extension in extensions:
        try:
            await client.load_extension(extension)
            check(f"load {extension}", True)
        except Exception as error:  # noqa: BLE001 - reporting every failure is the point
            check(f"load {extension}", False, f"{type(error).__name__}: {error}")

    slash_commands = sorted(command.qualified_name for command in client.tree.walk_commands())
    print(f"slash commands: {', '.join(slash_commands) if slash_commands else 'none'}")
    for expected in (
        "stock",
        "macro",
        "test_briefing",
        "test_macro_alert",
        "track_earnings",
        "untrack_earnings",
        "list_earnings",
    ):
        check(f"/{expected} registered", expected in slash_commands)

    # --------------------------------------------------------- background tasks
    section("background tasks")

    # tasks.Loop exposes no public name in this version, so identify each loop
    # by the attribute it was assigned to on its cog.
    loops = [
        (attribute, member)
        for cog in client.cogs.values()
        for attribute, member in inspect.getmembers(
            cog, lambda obj: isinstance(obj, tasks.Loop)
        )
    ]
    print(f"registered loops: {', '.join(name for name, _ in loops) if loops else 'none'}")
    check("At least one background loop registered", bool(loops))

    briefing = next((loop for name, loop in loops if name == "weekly_briefing"), None)
    if briefing is None:
        check("weekly_briefing loop present", False)
    elif not briefing.time:
        check("weekly_briefing runs at a fixed time", False, f"time={briefing.time}")
    else:
        scheduled = list(briefing.time)[0]
        check(
            "weekly_briefing scheduled for 08:00 US/Eastern",
            (scheduled.hour, scheduled.minute) == (8, 0),
            f"{scheduled.hour:02d}:{scheduled.minute:02d} tz={scheduled.tzinfo}",
        )

    poller = next((loop for name, loop in loops if name == "poll_macro_releases"), None)
    if poller is not None:
        check("macro poller interval set", poller.minutes > 0, f"every {poller.minutes} min")

    earnings_check = next((loop for name, loop in loops if name == "daily_check"), None)
    if earnings_check is None:
        check("daily_check loop present", False)
    elif not earnings_check.time:
        check("daily_check runs at a fixed time", False, f"time={earnings_check.time}")
    else:
        earnings_time = list(earnings_check.time)[0]
        check(
            "daily_check scheduled for 17:30 US/Eastern",
            (earnings_time.hour, earnings_time.minute) == (17, 30),
            f"{earnings_time.hour:02d}:{earnings_time.minute:02d} tz={earnings_time.tzinfo}",
        )

    # Loops started during cog_load are still pending; unload the cogs so their
    # cog_unload hooks cancel them, otherwise asyncio complains on exit.
    for name in list(client.cogs):
        await client.remove_cog(name)
    await client.close()

    # --------------------------------------------------------------- rendering
    section("embed rendering (synthetic data, no network)")

    from cogs.earnings_tracker import EarningsTracker
    from cogs.macro_alerts import (
        MONITORED_SERIES,
        RELEASE_WINDOWS,
        MacroAlerts,
        release_window_label,
    )
    from cogs.market_commands import MarketCommands
    from cogs.scheduled_briefings import (
        HIGH_INTEREST,
        MAX_PER_DAY,
        SNAPSHOT_GROUPS,
        ScheduledBriefings,
        timing_label,
    )

    check(
        "Three FRED series monitored",
        set(MONITORED_SERIES) == {"CPIAUCSL", "PCEPILFE", "GDPC1"},
        ", ".join(MONITORED_SERIES),
    )

    monday = date(2026, 10, 5)
    friday = monday + timedelta(days=4)
    sample = [
        # High-interest with no estimate: must still be listed.
        {
            "date": monday.isoformat(),
            "symbol": "AAPL",
            "quarter": 4,
            "year": 2026,
            "epsEstimate": None,
            "hour": "amc",
        },
        # Unstarred with no estimate: filtered out as an uncovered micro-cap.
        {"date": monday.isoformat(), "symbol": "ZZZZ", "epsEstimate": None, "hour": None},
        # Unstarred but covered: eligible, to prove the filter is not "starred only".
        {
            "date": monday.isoformat(),
            "symbol": "ACZT",
            "quarter": 1,
            "year": 2026,
            "epsEstimate": 0.12,
            "hour": "bmo",
        },
        # Overflow, to force the per-day cap and the "…and N more" line.
        *[
            {
                "date": friday.isoformat(),
                "symbol": f"T{index:03d}",
                "quarter": 1,
                "year": 2026,
                "epsEstimate": float(index),
                "hour": "bmo",
            }
            for index in range(40)
        ],
    ]

    try:
        briefing = ScheduledBriefings._build_embed(monday, friday, sample)
        day_fields = [
            field for field in briefing.fields if "Market Snapshot" not in field.name
        ]
        check("Briefing embed builds", True, f"{len(day_fields)} day field(s)")
        widest = max((len(field.value) for field in briefing.fields), default=0)
        check(
            "Briefing fields fit Discord's 1024-char cap",
            widest <= 1024,
            f"widest={widest}",
        )
        rendered = " ".join(field.value for field in briefing.fields)
        check("Briefing includes the priority name", "AAPL" in rendered)
        check("Briefing reports overflow", "more" in rendered)
    except Exception as error:  # noqa: BLE001 - reporting the failure is the point
        check("Briefing embed builds", False, f"{type(error).__name__}: {error}")

    try:
        alert = MacroAlerts._alert_embed(
            "CPIAUCSL",
            {
                "date": date(2026, 9, 1),
                "value": 325.4,
                "previous": 324.1,
                "year_ago": 315.2,
            },
            "08:30 ET window (CPI / PCE / GDP)",
        )
        check("Macro alert embed builds", True, f"{len(alert.fields)} field(s)")
    except Exception as error:  # noqa: BLE001
        check("Macro alert embed builds", False, f"{type(error).__name__}: {error}")

    for periods, expected in ((4, "Q2 2026"), (12, "June 2026")):
        label = MacroAlerts._period_label(date(2026, 6, 30), periods)
        check(f"Period label for {periods}/yr", label == expected, label)

    # ------------------------------------------------------------- CPI maths
    section("CPI year-over-year (synthetic series)")

    index = pd.date_range("2025-09-01", periods=13, freq="MS")
    rising = pd.Series([300.0 + offset for offset in range(13)], index=index)
    cpi = MarketCommands._cpi_year_over_year(rising)

    check(
        "CPI YoY computes the right rate",
        cpi is not None and round(cpi["yoy_percent"], 6) == 4.0,
        f"{cpi['yoy_percent']:.4f}% from 300 -> 312" if cpi else "returned None",
    )
    if cpi is not None:
        check(
            "CPI YoY uses exactly 13 observations",
            cpi["observations"] == 13,
            str(cpi["observations"]),
        )
        check(
            "CPI YoY compares the two endpoints",
            (cpi["year_ago_date"], cpi["latest_date"])
            == (date(2025, 9, 1), date(2026, 9, 1)),
            f"{cpi['year_ago_date']} -> {cpi['latest_date']}",
        )
    check(
        "CPI YoY refuses a window shorter than 13",
        MarketCommands._cpi_year_over_year(rising.tail(6)) is None,
    )
    check(
        "CPI YoY handles a flat series",
        MarketCommands._cpi_year_over_year(
            pd.Series([100.0] * 13, index=index)
        )["yoy_percent"]
        == 0.0,
    )

    # ------------------------------------------------------- briefing windows
    section("briefing week selection")

    midweek = ScheduledBriefings._briefing_week(date(2026, 10, 8))  # a Thursday
    check(
        "Midweek stays in the current week",
        midweek == (date(2026, 10, 5), date(2026, 10, 9)),
        f"{midweek[0]} -> {midweek[1]}",
    )
    weekend = ScheduledBriefings._briefing_week(date(2026, 10, 10))  # a Saturday
    check(
        "Weekend rolls forward to the coming week",
        weekend == (date(2026, 10, 12), date(2026, 10, 16)),
        f"{weekend[0]} -> {weekend[1]}",
    )

    # -------------------------------------------------- reported display times
    section("reporting slot labels (US/Pacific)")

    check(
        "Pre-market label matches the spec",
        timing_label("bmo") == "Pre-Market (~05:30-06:30 PT)",
        timing_label("bmo"),
    )
    check(
        "Post-market label matches the spec",
        timing_label("amc") == "Post-Market (~13:00-14:00 PT)",
        timing_label("amc"),
    )
    check("Market-hours label is derived too", timing_label("dmh") != "", timing_label("dmh"))
    check("An unknown timing code yields no label", timing_label("zzz") == "")
    check("A missing timing code yields no label", timing_label("") == "")

    check(
        "CPI/PCE/GDP release window is labelled in PT",
        release_window_label(RELEASE_WINDOWS[0]) == "05:25-06:30 PT window (CPI / PCE / GDP)",
        release_window_label(RELEASE_WINDOWS[0]),
    )
    check(
        "FOMC release window is labelled in PT",
        release_window_label(RELEASE_WINDOWS[1]) == "10:55-11:45 PT window (FOMC)",
        release_window_label(RELEASE_WINDOWS[1]),
    )

    entry_line = ScheduledBriefings._format_entry(
        {"symbol": "AAPL", "quarter": 4, "year": 2026, "hour": "amc"}
    )
    check(
        "Earnings lines carry the PT slot",
        "Post-Market (~13:00-14:00 PT)" in entry_line,
        entry_line,
    )

    # --------------------------------------------------------- market snapshot
    section("market snapshot")

    tickers = [ticker for _, assets in SNAPSHOT_GROUPS for ticker, _, _ in assets]
    check("Every requested asset is configured", len(tickers) == 17, f"{len(tickers)} symbols")
    check("No symbol is listed twice", len(set(tickers)) == len(tickers))

    sample_snapshot = {
        "BTC-USD": (67000.0, 66000.0),
        "ETH-USD": (3400.0, 3450.0),
        "^TNX": (4.23, 4.20),
        "EURUSD=X": (1.0876, 1.0863),
        "HYPE32196-USD": (12.34, None),  # no previous close available
    }

    blocks = ScheduledBriefings._snapshot_blocks(sample_snapshot)
    body = "\n".join(blocks)
    check("Snapshot renders a code block", bool(blocks), f"{len(blocks)} block(s)")
    check(
        "Every block is fenced",
        all(block.startswith("```") and block.endswith("```") for block in blocks),
    )
    widest = max((len(block) for block in blocks), default=0)
    check(
        "Snapshot blocks fit Discord's 1024-char field cap",
        widest <= 1024,
        f"widest={widest}",
    )
    check("Snapshot uses tree branches", "├" in body and "└" in body)
    check(
        "Snapshot lists every group",
        all(name in body for name, _ in SNAPSHOT_GROUPS),
        ", ".join(name for name, _ in SNAPSHOT_GROUPS),
    )
    check("Crypto is dollar-quoted", "BTC: $67,000.00 | +1.52%" in body)
    check("A fall in price shows a negative change", "ETH: $3,400.00 | -1.45%" in body)
    check("The 10Y yield is rendered as a percent", "US 10Y Yield: 4.23% | +0.71%" in body)
    check("FX is quoted to four decimals", "EUR/USD: 1.0876 | +0.12%" in body)
    check(
        "A missing previous close falls back to N/A",
        "HYPE: $12.34 | N/A" in body,
        "no crash",
    )
    check("Unfetched symbols fall back to N/A", body.count("N/A") >= 20, f"{body.count('N/A')} N/A")

    empty_snapshot_blocks = ScheduledBriefings._snapshot_blocks({})
    check(
        "An empty snapshot still renders every group",
        all(name in "\n".join(empty_snapshot_blocks) for name, _ in SNAPSHOT_GROUPS),
    )

    with_snapshot = ScheduledBriefings._build_embed(
        monday, friday, sample, sample_snapshot
    )
    field_names = [field.name for field in with_snapshot.fields]
    check(
        "Briefing embed carries the snapshot field",
        any("Market Snapshot" in name for name in field_names),
        ", ".join(field_names),
    )
    oversized = [
        field.name for field in with_snapshot.fields if len(field.value) > 1024
    ]
    check(
        "No briefing field exceeds the 1024-char cap",
        not oversized,
        ", ".join(oversized) if oversized else "all within cap",
    )
    check(
        "Briefing footer states Pacific",
        "US/Pacific" in (with_snapshot.footer.text or ""),
        with_snapshot.footer.text,
    )

    no_earnings = ScheduledBriefings._build_embed(monday, friday, [], sample_snapshot)
    check(
        "Snapshot still renders when the week has no earnings",
        any("Market Snapshot" in field.name for field in no_earnings.fields),
        "; ".join(field.name for field in no_earnings.fields),
    )

    # ------------------------------------------------- briefing row selection
    section("earnings briefing selection")

    def format_entry(entry):
        return ScheduledBriefings._format_entry(entry)

    # The exact row the brief specified.
    check(
        "Row format matches the spec",
        format_entry({"symbol": "JPM", "quarter": 3, "epsEstimate": 5.92, "hour": "bmo"})
        == "⭐ JPM · Q3 · EPS est $5.92 · Pre-Market (~05:30-06:30 PT)",
        format_entry({"symbol": "JPM", "quarter": 3, "epsEstimate": 5.92, "hour": "bmo"}),
    )
    check(
        "An unstarred row carries no star",
        format_entry({"symbol": "XYZ", "epsEstimate": 1.5}) == "XYZ · EPS est $1.50",
        format_entry({"symbol": "XYZ", "epsEstimate": 1.5}),
    )
    check(
        "A row with no details is just the symbol",
        format_entry({"symbol": "XYZ"}) == "XYZ",
        format_entry({"symbol": "XYZ"}),
    )
    check(
        "The year is not repeated in the quarter",
        "2026" not in format_entry(
            {"symbol": "JPM", "quarter": 3, "year": 2026, "epsEstimate": 5.92}
        ),
    )

    selection = ScheduledBriefings._build_embed(monday, friday, sample)
    monday_value = next(
        (field.value for field in selection.fields if field.name.startswith("Monday")),
        "",
    )
    check(
        "A high-interest name with no estimate is still listed",
        "AAPL" in monday_value,
        monday_value.replace("\n", " / "),
    )
    check(
        "An uncovered unstarred ticker is filtered out",
        "ZZZZ" not in monday_value,
        "ZZZZ absent" if "ZZZZ" not in monday_value else "ZZZZ still present",
    )
    check(
        "A covered unstarred ticker is still listed",
        "ACZT" in monday_value,
        monday_value.replace("\n", " / "),
    )

    # The reported bug: a heavy Monday and Tuesday consumed the whole 25-line
    # budget, so Thursday and Friday were cut off entirely.
    busy_week = []
    for offset in range(5):
        day = (monday + timedelta(days=offset)).isoformat()
        busy_week.extend(
            {
                "date": day,
                "symbol": f"D{offset}X{index:02d}",
                "quarter": 3,
                "year": 2026,
                "epsEstimate": 1.0 + index,
                "hour": "bmo",
            }
            for index in range(30)
        )

    busy = ScheduledBriefings._build_embed(monday, friday, busy_week)
    busy_days = [
        field for field in busy.fields if "Market Snapshot" not in field.name
    ]
    check("Every weekday gets a field", len(busy_days) == 5, f"{len(busy_days)} of 5")
    check(
        "Friday is not cut off by a heavy Monday",
        any("Friday" in field.name for field in busy_days),
        ", ".join(field.name for field in busy_days),
    )
    line_counts = [len(field.value.split("\n")) for field in busy_days]
    check(
        "No day exceeds the cap, plus room for its overflow note",
        all(count <= MAX_PER_DAY + 1 for count in line_counts),
        f"lines per day: {line_counts}",
    )
    check(
        "Every busy day reports an overflow count",
        all("more with estimates" in field.value for field in busy_days),
        "overflow wording distinguishes filtered micro-caps",
    )

    # Requirement: high-interest names are never dropped to meet the per-day cap.
    starred_heavy = [
        {
            "date": monday.isoformat(),
            "symbol": symbol,
            "quarter": 3,
            "year": 2026,
            "epsEstimate": None,
            "hour": "bmo",
        }
        for symbol in list(HIGH_INTEREST)[:8]
    ]
    overflowing = ScheduledBriefings._build_embed(monday, friday, starred_heavy)
    starred_day = next(
        field for field in overflowing.fields if field.name.startswith("Monday")
    )
    check(
        "More high-interest names than the cap all still show",
        all(symbol in starred_day.value for symbol in list(HIGH_INTEREST)[:8]),
        f"{len(starred_day.value.splitlines())} lines for 8 starred names (cap {MAX_PER_DAY})",
    )
    check(
        "A starred-only day still fits Discord's field cap",
        len(starred_day.value) <= 1024,
        f"{len(starred_day.value)} chars",
    )

    # A day whose reports all lack estimates still gets a field, so it is clear
    # the day was not dropped by a bug.
    uncovered = ScheduledBriefings._build_embed(
        monday,
        friday,
        [{"date": monday.isoformat(), "symbol": "MICRO", "epsEstimate": None}],
    )
    uncovered_day = next(
        field for field in uncovered.fields if field.name.startswith("Monday")
    )
    check(
        "A day with no covered names says so",
        "No analyst estimates" in uncovered_day.value,
        uncovered_day.value,
    )

    # ------------------------------------------------------------- test posts
    section("test-post labelling")

    test_briefing = ScheduledBriefings._build_embed(monday, friday, sample, is_test=True)
    check("Briefing test post is labelled", "[TEST]" in test_briefing.title)
    check(
        "Briefing test post says so in the footer",
        "TEST POST" in (test_briefing.footer.text or ""),
    )

    alert_observation = {
        "date": date(2026, 9, 1),
        "value": 325.4,
        "previous": 324.1,
        "year_ago": 315.2,
    }
    test_alert = MacroAlerts._alert_embed("CPIAUCSL", alert_observation, None, is_test=True)
    check("Alert test post is labelled", "[TEST]" in test_alert.title)
    check(
        "Alert test post warns the cache is unchanged",
        "cache unchanged" in (test_alert.footer.text or ""),
    )
    check(
        "Test post is visually distinct from a real alert",
        test_alert.color.value != MacroAlerts._alert_embed(
            "CPIAUCSL", alert_observation, None
        ).color.value,
    )

    # --------------------------------------------------------- earnings result
    section("earnings result embed")

    beat = EarningsTracker._result_embed(
        "AAPL",
        {
            "symbol": "AAPL",
            "quarter": 4,
            "year": 2026,
            "hour": "amc",
            "epsActual": 1.85,
            "epsEstimate": 1.60,
            "revenueActual": 94_000_000_000,
            "revenueEstimate": 92_000_000_000,
        },
        date(2026, 10, 30),
    )
    check("Earnings result embed builds", len(beat.fields) >= 4, f"{len(beat.fields)} field(s)")
    check("A beat is coloured green", beat.color.value == 0x2ECC71, hex(beat.color.value))
    check(
        "Earnings result reads revenue as a compact number",
        any("B" in field.value for field in beat.fields),
    )

    miss = EarningsTracker._result_embed(
        "MSFT",
        {"symbol": "MSFT", "hour": "bmo", "epsActual": 1.10, "epsEstimate": 1.50},
        date(2026, 10, 30),
    )
    check("A miss is coloured red", miss.color.value == 0xE74C3C, hex(miss.color.value))
    check(
        "An entry with no consensus still renders",
        EarningsTracker._result_embed(
            "ZZZZ", {"symbol": "ZZZZ", "epsActual": 0.5}, date(2026, 10, 30)
        )
        is not None,
    )

    # ----------------------------------------------------------------- summary
    section("summary")
    failed = RESULTS.count(False)
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
