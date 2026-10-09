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
    from cogs.macro_alerts import MONITORED_SERIES, MacroAlerts
    from cogs.market_commands import MarketCommands
    from cogs.scheduled_briefings import ScheduledBriefings

    check(
        "Three FRED series monitored",
        set(MONITORED_SERIES) == {"CPIAUCSL", "PCEPILFE", "GDPC1"},
        ", ".join(MONITORED_SERIES),
    )

    monday = date(2026, 10, 5)
    friday = monday + timedelta(days=4)
    sample = [
        {
            "date": monday.isoformat(),
            "symbol": "AAPL",
            "quarter": 4,
            "year": 2026,
            "epsEstimate": 1.2345,
            "hour": "amc",
        },
        # Deliberately missing fields, to prove nulls are skipped rather than rendered.
        {"date": monday.isoformat(), "symbol": "ZZZZ", "epsEstimate": None, "hour": None},
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
        check("Briefing embed builds", True, f"{len(briefing.fields)} day field(s)")
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
