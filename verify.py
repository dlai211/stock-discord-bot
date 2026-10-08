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
    for expected in ("stock", "macro"):
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

    # Loops started during cog_load are still pending; unload the cogs so their
    # cog_unload hooks cancel them, otherwise asyncio complains on exit.
    for name in list(client.cogs):
        await client.remove_cog(name)
    await client.close()

    # --------------------------------------------------------------- rendering
    section("embed rendering (synthetic data, no network)")

    from cogs.macro_alerts import MONITORED_SERIES, MacroAlerts
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

    # ----------------------------------------------------------------- summary
    section("summary")
    failed = RESULTS.count(False)
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
