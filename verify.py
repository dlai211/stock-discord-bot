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
from datetime import date, datetime, time as dtime, timedelta

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


async def main(live: bool = False) -> int:
    print(f"Python {sys.version.split()[0]}   discord.py {discord.__version__}")
    if not live:
        print("offline mode — nothing here touches the network")
        print("pass --live to also fetch every timeframe from Yahoo for real")

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
        "earnings",
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
        STATUS_ACCELERATED,
        STATUS_COOLED,
        STATUS_COOLDOWN,
        STATUS_INLINE,
        STATUS_UPTICK,
        MacroAlerts,
        as_mapping,
        base_period,
        metric_line,
        rate_metrics,
        release_date,
        release_window_label,
        status_for,
        value_months_back,
    )
    from cogs.market_commands import MarketCommands
    from cogs.scheduled_briefings import (
        HIGH_INTEREST,
        SNAPSHOT_GROUPS,
        TIER1_RELEASES,
        ScheduledBriefings,
        tier1_match,
    )
    from utils import timing_label

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

    def day_fields_of(embed):
        """Day-by-day earnings fields, excluding the macro and snapshot sections."""
        return [
            field
            for field in embed.fields
            if "Snapshot" not in field.name and "Macro" not in field.name
        ]

    try:
        briefing = ScheduledBriefings._build_embed(monday, friday, sample)
        day_fields = day_fields_of(briefing)
        check("Briefing embed builds", True, f"{len(day_fields)} day field(s)")
        widest = max((len(field.value) for field in briefing.fields), default=0)
        check(
            "Briefing fields fit Discord's 1024-char cap",
            widest <= 1024,
            f"widest={widest}",
        )
        rendered = " ".join(field.value for field in briefing.fields)
        check("Briefing includes the priority name", "AAPL" in rendered)
        check(
            "A day holding only untracked companies says so",
            "No high-interest or tracked companies" in rendered,
            "the filter emptied Friday, not a bug",
        )
    except Exception as error:  # noqa: BLE001 - reporting the failure is the point
        check("Briefing embed builds", False, f"{type(error).__name__}: {error}")

    # ------------------------------------------------------- macro alert rates
    section("macro alert rates of change")

    # Real CPIAUCSL observations — including the hole FRED actually has.
    # October 2025 is absent from every CPI series, so the 13th-from-last
    # observation is August 2025: 13 months back, not 12.
    cpi_months = [
        "2025-06", "2025-07", "2025-08", "2025-09",
        # 2025-10 is the missing month.
        "2025-11", "2025-12",
        "2026-01", "2026-02", "2026-03", "2026-04", "2026-05",
        "2026-06", "2026-07", "2026-08", "2026-09",
    ]
    cpi_levels = [
        321.44, 322.17, 323.29, 324.25,
        325.06, 326.03,
        326.59, 327.46, 330.29, 332.41, 333.98,
        332.57, 332.81, 334.13, 336.30,
    ]
    cpi_series = pd.Series(
        cpi_levels, index=pd.to_datetime([f"{month}-01" for month in cpi_months])
    )
    cpi_metrics = rate_metrics(cpi_series, 12)

    check("CPI metrics compute", cpi_metrics is not None)
    if cpi_metrics:
        # These four are the numbers BLS published for September 2026.
        check(
            "CPI YoY matches the BLS release",
            round(cpi_metrics["yoy"], 1) == 3.7,
            f"{cpi_metrics['yoy']:+.2f}% -> 3.7%",
        )
        check(
            "CPI prior-year rate matches the BLS release",
            round(cpi_metrics["prior_yoy"], 1) == 3.4,
            f"{cpi_metrics['prior_yoy']:+.2f}% -> 3.4%",
        )
        check(
            "CPI MoM matches the BLS release",
            round(cpi_metrics["mom"], 1) == 0.6,
            f"{cpi_metrics['mom']:+.2f}% -> +0.6%",
        )
        check(
            "CPI prior-month rate matches the BLS release",
            round(cpi_metrics["prior_mom"], 1) == 0.4,
            f"{cpi_metrics['prior_mom']:+.2f}% -> +0.4%",
        )
        check(
            "A monthly series reports no annualised rate",
            cpi_metrics["annualized"] is None,
            "annualisation is a quarterly thing only",
        )

    # The contrast that makes the bug concrete: same series, two lookups.
    positional = (cpi_series.iloc[-1] / cpi_series.iloc[-13] - 1) * 100
    check(
        "Positional indexing would overstate CPI YoY by a third of a point",
        round(positional, 1) == 4.0 and round(cpi_metrics["yoy"], 1) == 3.7,
        f"iloc[-13] = 2025-08 gives {positional:.2f}%; the date lookup gives {cpi_metrics['yoy']:.2f}%",
    )
    check(
        "The lookup walks back 12 calendar months, not 12 rows",
        value_months_back(cpi_series, 12) == cpi_series.loc["2025-09-01"],
        f"{value_months_back(cpi_series, 12)} is September 2025",
    )

    # If the year-ago month itself is missing, refuse rather than reach further.
    holed = pd.Series(
        [value for month, value in zip(cpi_months, cpi_levels) if month != "2025-09"],
        index=pd.to_datetime(
            [f"{month}-01" for month in cpi_months if month != "2025-09"]
        ),
    )
    check(
        "A missing year-ago month states no rate at all",
        rate_metrics(holed, 12) is None,
        "None rather than silently reaching a month further back",
    )

    # Real GDPC1 observations. Six quarters, because a rate needs the latest
    # pair *and* both of their year-ago counterparts.
    gdp_months = ["2025-01", "2025-04", "2025-07", "2025-10", "2026-01", "2026-04"]
    gdp_levels = [23650.21, 23884.56, 24112.97, 24125.48, 24274.38, 24408.01]
    gdp_series = pd.Series(
        gdp_levels, index=pd.to_datetime([f"{month}-01" for month in gdp_months])
    )
    gdp_metrics = rate_metrics(gdp_series, 4)
    check("GDP metrics compute", gdp_metrics is not None)
    if gdp_metrics:
        check(
            "GDP annualised QoQ (SAAR)",
            round(gdp_metrics["annualized"], 1) == 2.2,
            f"{gdp_metrics['annualized']:+.1f}% — BEA's ((new/prev)**4 - 1)",
        )
        check(
            "GDP prior-quarter annualised rate",
            round(gdp_metrics["prior_annualized"], 1) == 2.5,
            f"{gdp_metrics['prior_annualized']:+.1f}%",
        )
        check(
            "GDP YoY growth",
            round(gdp_metrics["yoy"], 2) == 2.19,
            f"{gdp_metrics['yoy']:+.2f}%",
        )
        check(
            "A quarter is three months, not twelve",
            value_months_back(gdp_series, 3) == gdp_series.loc["2026-01-01"],
            "the date lookup uses periods_per_year, not a fixed year",
        )

    check(
        "Too little history states no rate at all",
        rate_metrics(cpi_series.tail(3), 12) is None,
        "a half-derivable number is worse than none",
    )
    check(
        "A zero or negative level states no rate",
        rate_metrics(
            pd.Series([0.0] * len(cpi_series), index=cpi_series.index), 12
        )
        is None,
        "ratios against zero are meaningless",
    )

    # --- the status ladder, and the float trap underneath it
    check(
        "0.6 - 0.4 really is below 0.2 in binary floating point",
        0.6 - 0.4 < 0.2,
        f"{0.6 - 0.4!r} — why status_for compares in tenths",
    )
    for actual, prior, expected, why in (
        (3.7173, 3.3530, STATUS_ACCELERATED, "+0.3"),
        (0.6485, 0.3960, STATUS_ACCELERATED, "+0.2 on the boundary"),
        (2.5658, 2.4462, STATUS_ACCELERATED, "+0.2 on the boundary"),
        (0.3348, 0.2898, STATUS_INLINE, "no change"),
        (0.41, 0.30, STATUS_UPTICK, "+0.1"),
        (0.30, 0.41, STATUS_COOLDOWN, "-0.1"),
        (0.20, 0.41, STATUS_COOLED, "-0.2"),
    ):
        check(
            f"Status for {actual} vs {prior} ({why})",
            status_for(actual, prior) == expected,
            status_for(actual, prior),
        )
    check(
        "Every status marker is forced to text presentation",
        all("︎" in marker for marker in (STATUS_ACCELERATED, STATUS_INLINE)),
        "single-width, so the monospace table stays aligned",
    )

    # --- the CPI embed: headline and core in one table
    def component(label, short, value, mom, prior_mom, yoy, prior_yoy, annualized=None, prior_annualized=None):
        return {
            "label": label,
            "short": short,
            "value": value,
            "mom": mom,
            "prior_mom": prior_mom,
            "yoy": yoy,
            "prior_yoy": prior_yoy,
            "annualized": annualized,
            "prior_annualized": prior_annualized,
        }

    cpi_alert_metrics = {
        "date": date(2026, 9, 1),
        "components": [
            component("Headline CPI", "Headline", 336.30, 0.6485, 0.3960, 3.7173, 3.3530),
            component("Core CPI", "Core", 338.90, 0.3348, 0.2898, 2.5658, 2.4462),
        ],
    }

    alert = MacroAlerts._alert_embed("CPIAUCSL", cpi_alert_metrics)
    check(
        "CPI embed is one field per measure plus an index field",
        [field.name for field in alert.fields]
        == ["Headline CPI", "Core CPI", "Index Levels & Official Release"],
        ", ".join(field.name for field in alert.fields),
    )
    check(
        "CPI title matches the spec",
        alert.title == "\U0001f4ca US Consumer Price Index (CPI) — September 2026",
        alert.title,
    )
    check(
        "CPI title links to the official BLS release",
        alert.url == "https://www.bls.gov/news.release/cpi.nr0.htm",
        str(alert.url),
    )
    check(
        "No part of the CPI alert is a code block",
        not any("```" in field.value for field in alert.fields),
        "fields render in Discord's own typography",
    )
    check(
        "Every measure field is full width",
        all(field.inline is False for field in alert.fields),
        "a third-width inline field would wrap these lines",
    )

    headline_lines = alert.fields[0].value.split("\n")
    check(
        "The headline field shows YoY above MoM",
        [line[:3] for line in headline_lines] == ["YoY", "MoM"],
        " · ".join(headline_lines),
    )
    check(
        "A measure field is exactly two lines",
        all(len(field.value.split("\n")) == 2 for field in alert.fields[:2]),
        "one reading per period, nothing wrapping by design",
    )
    rendered = " ".join(field.value for field in alert.fields)
    check(
        "Every figure from the BLS release is present",
        all(
            snippet in rendered
            for snippet in ("3.7%", "3.4%", "+0.6%", "+0.4%", "2.6%", "2.4%", "+0.3%")
        ),
        "both measures, actual and prior",
    )
    check(
        "The alert uses the short status wording",
        rendered.count("Accel") == 3 and rendered.count("Flat") == 1,
        f"{rendered.count('Accel')} accel, {rendered.count('Flat')} flat",
    )
    check(
        "metric_line reads value first, direction next, prior last",
        metric_line(cpi_alert_metrics["components"][0], "yoy", "YoY")
        == "YoY **3.7%** · ▲︎ Accel · was 3.4%",
        metric_line(cpi_alert_metrics["components"][0], "yoy", "YoY"),
    )
    check(
        "metric_line signs month-on-month but not the 12-month rate",
        metric_line(cpi_alert_metrics["components"][1], "mom", "MoM")
        == "MoM **+0.3%** · ⏸︎ Flat · was +0.3%",
        metric_line(cpi_alert_metrics["components"][1], "mom", "MoM"),
    )
    check(
        "The long status wording is still the default",
        status_for(3.7173, 3.3530) == STATUS_ACCELERATED,
        status_for(3.7173, 3.3530),
    )
    check("CPI colour is gold", alert.color.value == 0xF1C40F, hex(alert.color.value))
    check(
        "CPI footer credits the Bureau of Labor Statistics",
        (alert.footer.text or "").startswith(
            "Source: Bureau of Labor Statistics via FRED"
        ),
        alert.footer.text,
    )

    index_field = alert.fields[2].value
    check(
        "The index field names both levels",
        "Headline 336.30" in index_field and "Core 338.90" in index_field,
        index_field.replace("\n", " / "),
    )
    check(
        "The index field states the base period",
        "Base 1982-1984=100" in index_field,
        index_field.replace("\n", " / "),
    )
    check(
        "The index field links the full release",
        "[Read Full BLS Release](https://www.bls.gov/news.release/cpi.nr0.htm)"
        in index_field,
    )

    # --- the single-component embeds are unchanged
    gdp_alert_metrics = {
        "date": date(2026, 4, 1),
        "components": [
            component("Real GDP", "Real GDP", 24408.01, 0.5507, 0.6172, 2.1916, 2.6427, 2.217, 2.493)
        ],
    }
    gdp_alert = MacroAlerts._alert_embed(
        "GDPC1", gdp_alert_metrics, {"units": "Billions of Chained 2017 Dollars"}
    )
    check(
        "GDP fields lead with annualised growth and YoY",
        [field.name for field in gdp_alert.fields]
        == ["Annualized QoQ (SAAR)", "YoY Growth", "Real Output"],
        ", ".join(field.name for field in gdp_alert.fields),
    )
    check(
        "GDP title matches the spec",
        gdp_alert.title == "\U0001f4c8 US Real Gross Domestic Product (GDP) — Q2 2026",
        gdp_alert.title,
    )
    check(
        "GDP reports the annualised rate to one decimal",
        "+2.2%" in gdp_alert.fields[0].value,
        gdp_alert.fields[0].value.replace("\n", " / "),
    )
    check(
        "GDP real output is shown in billions",
        "$24,408.0B" in gdp_alert.fields[2].value,
        gdp_alert.fields[2].value.replace("\n", " / "),
    )
    check("GDP colour is blue", gdp_alert.color.value == 0x3498DB, hex(gdp_alert.color.value))
    check("Only CPI declares a release link", gdp_alert.url is None)

    pce_alert_metrics = {
        "date": date(2026, 8, 1),
        "components": [
            component("Core PCE", "Core PCE", 130.46, 0.25, 0.13, 3.01, 2.98)
        ],
    }
    pce_alert = MacroAlerts._alert_embed(
        "PCEPILFE", pce_alert_metrics, {"units": "Index 2017=100"}
    )
    check(
        "Core PCE title matches the spec",
        pce_alert.title
        == "\U0001f4ca US Core PCE Price Index (Fed's Preferred Gauge) — August 2026",
        pce_alert.title,
    )
    check(
        "Core PCE keeps the three-field layout",
        [field.name for field in pce_alert.fields]
        == ["MoM Rate", "YoY Rate", "Index Level"],
        ", ".join(field.name for field in pce_alert.fields),
    )
    check("Core PCE colour is purple", pce_alert.color.value == 0x9B59B6, hex(pce_alert.color.value))
    check(
        "Core PCE footer credits the BEA",
        (pce_alert.footer.text or "").startswith("Source: BEA via FRED"),
        pce_alert.footer.text,
    )

    # --- units come from FRED, and the base period is derived from them
    check(
        "base_period turns FRED's wording into a base period",
        base_period("Index 1982-1984=100", "unused") == "Base 1982-1984=100",
        base_period("Index 1982-1984=100", "unused"),
    )
    check(
        "base_period falls back when FRED gives nothing",
        base_period("", "Index 2017=100") == "Base 2017=100",
        base_period("", "Index 2017=100"),
    )

    # --- release date and units come from FRED's own metadata
    check(
        "The release date is read from FRED's last_updated",
        release_date({"last_updated": "2026-10-14 08:08:29-05"}) == date(2026, 10, 14),
        str(release_date({"last_updated": "2026-10-14 08:08:29-05"})),
    )
    check("A missing release date is tolerated", release_date(None) is None)
    check("A malformed release date is tolerated", release_date({"last_updated": "?"}) is None)

    # fredapi returns series metadata as a pandas Series, and a Series has no
    # truth value — `info or {}` raises "truth value is ambiguous" on it. Every
    # offline test above passed a dict, so only a live run surfaced this.
    series_info = pd.Series(
        {"units": "Index 2017=100", "last_updated": "2026-09-30 07:43:28-05"}
    )
    check(
        "Series metadata normalises to a dict",
        as_mapping(series_info)["units"] == "Index 2017=100",
        "the form fredapi actually returns",
    )
    check("None metadata normalises to an empty dict", as_mapping(None) == {})
    check(
        "A release date survives coming from a Series",
        release_date(series_info) == date(2026, 9, 30),
        str(release_date(series_info)),
    )
    from_series = MacroAlerts._alert_embed("PCEPILFE", pce_alert_metrics, series_info)
    check(
        "An alert renders from Series metadata",
        "Index 2017=100" in from_series.fields[2].value,
        from_series.fields[2].value.replace("\n", " / "),
    )

    dated = MacroAlerts._alert_embed(
        "CPIAUCSL", cpi_alert_metrics, {"last_updated": "2026-10-14 08:08:29-05"}
    )
    check(
        "The footer carries the release date",
        "Released Oct 14, 2026" in (dated.footer.text or ""),
        dated.footer.text,
    )
    fallback_units = MacroAlerts._alert_embed("CPIAUCSL", cpi_alert_metrics)
    check(
        "Without metadata the configured base period is used",
        "Base 1982-1984=100" in fallback_units.fields[2].value,
        fallback_units.fields[2].value.replace("\n", " / "),
    )
    check(
        "Every macro alert field fits Discord's cap",
        all(
            len(field.value) <= 1024 and len(field.name) <= 256
            for embed in (alert, gdp_alert, pce_alert, dated)
            for field in embed.fields
        ),
    )
    check(
        "Every macro alert title fits Discord's cap",
        all(len(embed.title or "") <= 256 for embed in (alert, gdp_alert, pce_alert)),
    )

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

    # The listing is now: high-interest names, or tickers the user tracks.
    # Everything else is left out however well covered it is.
    selection = ScheduledBriefings._build_embed(monday, friday, sample)
    monday_value = next(
        (field.value for field in selection.fields if field.name.startswith("Monday")),
        "",
    )
    check(
        "A high-interest name is listed",
        "AAPL" in monday_value,
        monday_value.replace("\n", " / "),
    )
    check(
        "An unstarred, untracked ticker is left out",
        "ACZT" not in monday_value,
        monday_value.replace("\n", " / "),
    )
    check(
        "An uncovered ticker is left out",
        "ZZZZ" not in monday_value,
        "ZZZZ absent" if "ZZZZ" not in monday_value else "ZZZZ still present",
    )

    tracked_view = ScheduledBriefings._build_embed(
        monday, friday, sample, tracked={"ACZT"}
    )
    tracked_monday = next(
        (
            field.value
            for field in tracked_view.fields
            if field.name.startswith("Monday")
        ),
        "",
    )
    check(
        "A tracked ticker is listed even though it is unstarred",
        "ACZT" in tracked_monday,
        tracked_monday.replace("\n", " / "),
    )
    check(
        "Tracking one ticker does not let other unstarred tickers in",
        "ZZZZ" not in tracked_monday,
    )

    # The reported bug: a heavy Monday and Tuesday consumed the whole budget and
    # Thursday and Friday were cut off entirely. With the listing filtered this
    # narrowly there is no line cap left to starve a day, so every day has to
    # show every one of its names.
    busy_week = []
    for offset in range(5):
        day = (monday + timedelta(days=offset)).isoformat()
        busy_week.extend(
            {
                "date": day,
                "symbol": f"TRK{offset}{index:02d}",
                "quarter": 3,
                "year": 2026,
                "epsEstimate": 1.0 + index,
                "hour": "bmo",
            }
            for index in range(8)
        )
    busy_tracked = {entry["symbol"] for entry in busy_week}

    busy = ScheduledBriefings._build_embed(
        monday, friday, busy_week, tracked=busy_tracked
    )
    busy_days = day_fields_of(busy)
    check("Every weekday gets a field", len(busy_days) == 5, f"{len(busy_days)} of 5")
    check(
        "Friday is not cut off by a heavy Monday",
        any("Friday" in field.name for field in busy_days),
        ", ".join(field.name for field in busy_days),
    )
    line_counts = [len(field.value.split("\n")) for field in busy_days]
    check(
        "Every tracked company is listed, with no cap",
        all(count == 8 for count in line_counts),
        f"lines per day: {line_counts}",
    )

    # A day with more names than a single field can hold still degrades cleanly.
    flood = [
        {
            "date": monday.isoformat(),
            "symbol": f"FLOOD{index:03d}",
            "quarter": 3,
            "year": 2026,
            "epsEstimate": None,
            "hour": "bmo",
        }
        for index in range(120)
    ]
    flooded = ScheduledBriefings._build_embed(
        monday, friday, flood, tracked={entry["symbol"] for entry in flood}
    )
    flooded_day = next(
        field for field in flooded.fields if field.name.startswith("Monday")
    )
    check(
        "An over-long day stays inside Discord's field cap",
        len(flooded_day.value) <= 1024,
        f"{len(flooded_day.value)} chars",
    )
    check(
        "An over-long day reports what it dropped",
        "more" in flooded_day.value,
        flooded_day.value.splitlines()[-1],
    )

    # A day whose reports are all ineligible still gets a field, so a quiet day
    # can never be mistaken for one the code dropped.
    uncovered = ScheduledBriefings._build_embed(
        monday,
        friday,
        [{"date": monday.isoformat(), "symbol": "MICRO", "epsEstimate": None}],
    )
    uncovered_day = next(
        field for field in uncovered.fields if field.name.startswith("Monday")
    )
    check(
        "A day with nothing notable says so",
        "No high-interest or tracked companies" in uncovered_day.value,
        uncovered_day.value,
    )

    # ---------------------------------------------------------- macro calendar
    section("macro economic calendar")

    check(
        "The high-interest list is the curated 37",
        len(HIGH_INTEREST) == 37,
        f"{len(HIGH_INTEREST)} names",
    )
    check(
        "Tier-1 list covers the four FRED-sourced releases",
        [label for _, label, _, _ in TIER1_RELEASES]
        == [
            "CPI Inflation",
            "Core PCE Price Index",
            "Real GDP",
            "Non-Farm Payrolls",
        ],
        ", ".join(label for _, label, _, _ in TIER1_RELEASES),
    )
    check(
        "FOMC is excluded, because FRED cannot date a rate decision",
        tier1_match("FOMC Press Release") is None,
        "release 101 carries 365 dates a year",
    )

    check(
        "A tier-1 release name matches",
        tier1_match("Consumer Price Index") is not None,
        str(tier1_match("Consumer Price Index")),
    )
    matched = tier1_match("Personal Income and Outlays")
    check(
        "Core PCE maps to the Personal Income and Outlays release",
        matched is not None and matched[0] == "Core PCE Price Index",
        str(matched),
    )
    check(
        "Matching is exact, not by substring",
        tier1_match("Sticky Price CPI") is None
        and tier1_match("Gross Domestic Product by State") is None
        and tier1_match("Trimmed Mean PCE Inflation Rate") is None
        and tier1_match("Employment Situation Summary") is None,
        "near-miss releases are rejected",
    )
    check(
        "Non-tier-1 releases are ignored",
        tier1_match("Producer Price Index") is None,
        "PPI is not on the list",
    )

    macro_line = ScheduledBriefings._macro_field(
        [{"date": date(2026, 10, 13), "label": "CPI Inflation", "clock": "05:30"}]
    )
    check(
        "Macro line matches the spec",
        macro_line == "Tuesday, Oct 13 · CPI Inflation · 05:30 PT",
        macro_line,
    )
    check(
        "An empty week says so explicitly",
        "No major tier-1 macro releases" in ScheduledBriefings._macro_field([]),
        ScheduledBriefings._macro_field([]),
    )
    check(
        "A failed fetch is not reported as an empty week",
        "Unavailable" in ScheduledBriefings._macro_field(None)
        and "No major" not in ScheduledBriefings._macro_field(None),
        ScheduledBriefings._macro_field(None),
    )

    macro_embed = ScheduledBriefings._build_embed(
        monday,
        friday,
        sample,
        macro_events=[
            {"date": date(2026, 10, 13), "label": "CPI Inflation", "clock": "05:30"}
        ],
    )
    macro_field = next(
        (field for field in macro_embed.fields if "Macro" in field.name), None
    )
    check(
        "Briefing carries the macro field",
        macro_field is not None,
        ", ".join(field.name for field in macro_embed.fields),
    )
    check(
        "Macro field fits Discord's cap",
        macro_field is not None and len(macro_field.value) <= 1024,
        f"{len(macro_field.value)} chars" if macro_field else "missing",
    )
    # /test_briefing runs the same _build_embed, so a test post must carry the
    # macro section too — not just the scheduled Monday run.
    test_macro_embed = ScheduledBriefings._build_embed(
        monday,
        friday,
        sample,
        macro_events=[
            {"date": date(2026, 10, 13), "label": "CPI Inflation", "clock": "05:30"}
        ],
        is_test=True,
    )
    check(
        "A test post renders the macro field too",
        any("Macro" in field.name for field in test_macro_embed.fields)
        and "[TEST]" in (test_macro_embed.title or ""),
        ", ".join(field.name for field in test_macro_embed.fields),
    )

    # ------------------------------------------------------------- /earnings
    section("earnings lookup")

    next_text = MarketCommands._next_report_text(
        {
            "date": "2026-11-02",
            "hour": "amc",
            "epsEstimate": 2.0214,
            "revenueEstimate": 115327453620,
        }
    )
    check(
        "Next report shows the date",
        "Monday, November 2, 2026" in next_text,
        next_text.replace("\n", " / "),
    )
    check(
        "Next report shows the session in Pacific",
        "Post-Market (~13:00-14:00 PT)" in next_text,
    )
    check(
        "Next report shows the consensus figures",
        "$2.02" in next_text and "$115.33B" in next_text,
    )
    check(
        "An unscheduled report says so",
        "No upcoming report" in MarketCommands._next_report_text(None),
    )
    unconfirmed = MarketCommands._next_report_text({"date": "2026-11-02", "hour": ""})
    check("An unconfirmed session says so", "not yet confirmed" in unconfirmed, unconfirmed)

    last_text = MarketCommands._last_report_text(
        [
            {
                "quarter": 3,
                "year": 2026,
                "period": "2026-06-30",
                "actual": 1.91,
                "estimate": 1.9271,
                "surprisePercent": -0.8873,
            },
            {
                "quarter": 2,
                "year": 2026,
                "period": "2026-03-31",
                "actual": 2.01,
                "estimate": 1.9884,
                "surprisePercent": 1.0863,
            },
        ]
    )
    check(
        "Last quarter shows actual against estimate",
        "$1.91" in last_text and "$1.93" in last_text,
        last_text.replace("\n", " / "),
    )
    check("Last quarter shows the surprise", "-0.89%" in last_text)
    check(
        "Prior quarters are summarised on one line",
        "Prior" in last_text and "$2.01" in last_text,
    )
    check(
        "No reported quarters says so",
        "No reported quarters" in MarketCommands._last_report_text([]),
    )
    check(
        "A quarter with a missing estimate still renders",
        "N/A"
        in MarketCommands._last_report_text([{"quarter": 1, "year": 2026, "actual": 1.0}]),
    )
    check(
        "Earnings text stays inside Discord's field cap",
        len(next_text) <= 1024 and len(last_text) <= 1024,
        f"next={len(next_text)}, last={len(last_text)}",
    )

    lookup_cog = MarketCommands(bot=None)
    lookup_embed = lookup_cog._earnings_embed(
        "AAPL",
        {
            "date": "2026-11-02",
            "hour": "amc",
            "epsEstimate": 2.0214,
            "revenueEstimate": 115327453620,
        },
        [
            {
                "quarter": 3,
                "year": 2026,
                "period": "2026-06-30",
                "actual": 1.91,
                "estimate": 1.9271,
                "surprisePercent": -0.8873,
            }
        ],
    )
    check(
        "Earnings embed builds",
        [field.name for field in lookup_embed.fields] == ["Next report", "Last reported"],
        ", ".join(field.name for field in lookup_embed.fields),
    )
    check(
        "A miss is coloured red",
        lookup_embed.color.value == 0xE74C3C,
        hex(lookup_embed.color.value),
    )

    # ------------------------------------------------------------ stock charts
    section("stock charts")

    import matplotlib
    import numpy as np
    from PIL import Image

    from charting import (
        BASELINE_PREVIOUS_CLOSE,
        CHART_FILENAME,
        DEFAULT_TIMEFRAME,
        TIMEFRAMES,
        ChartUnavailable,
        day_start_positions,
        generate_stock_chart,
        label_for,
        previous_session_close,
        render_chart,
        tick_positions,
        timeframe,
        timeframe_args,
        trim_to_last_session,
    )
    from cogs.market_commands import StockChartView

    check(
        "Matplotlib runs headless on the Agg backend",
        matplotlib.get_backend().lower() == "agg",
        matplotlib.get_backend(),
    )
    check(
        "Timeframes match the spec",
        [(entry.label, entry.period, entry.interval) for entry in TIMEFRAMES]
        == [
            ("1D", "5d", "2m"),
            ("1W", "5d", "5m"),
            ("1M", "1mo", "1h"),
            ("3M", "3mo", "1h"),
            ("1Y", "1y", "1d"),
        ],
        ", ".join(f"{entry.label}={entry.period}/{entry.interval}" for entry in TIMEFRAMES),
    )
    # Yahoo rejects anything outside this list. It matters: the brief asked for
    # interval="10m" for 1W, which raises "Invalid input" at fetch time.
    yahoo_intervals = {
        "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "4h",
        "1d", "5d", "1wk", "1mo", "3mo",
    }
    check(
        "Every interval is one Yahoo accepts",
        all(entry.interval in yahoo_intervals for entry in TIMEFRAMES),
        ", ".join(sorted({entry.interval for entry in TIMEFRAMES})),
    )
    check(
        "Extended hours are on for the short timeframes only",
        [entry.label for entry in TIMEFRAMES if entry.prepost] == ["1D", "1W"],
        ", ".join(entry.label for entry in TIMEFRAMES if entry.prepost),
    )
    check(
        "1D measures its return from the previous close",
        [
            entry.label
            for entry in TIMEFRAMES
            if entry.baseline == BASELINE_PREVIOUS_CLOSE
        ]
        == ["1D"],
        ", ".join(f"{entry.label}:{entry.baseline}" for entry in TIMEFRAMES),
    )
    check(
        "Only 1D trims to a single session",
        [entry.label for entry in TIMEFRAMES if entry.session_only] == ["1D"],
    )
    check(
        "Only 1W uses per-day ticks",
        [entry.label for entry in TIMEFRAMES if entry.day_ticks] == ["1W"],
        ", ".join(
            entry.label for entry in TIMEFRAMES if entry.day_ticks
        )
        or "none",
    )
    check(
        "Buttons are ordered 1D, 1W, 1M, 3M, 1Y",
        [entry.label for entry in TIMEFRAMES] == ["1D", "1W", "1M", "3M", "1Y"],
        " · ".join(entry.label for entry in TIMEFRAMES),
    )
    check(
        "1M and 3M fetch hourly bars, not daily",
        [timeframe(label).interval for label in ("1M", "3M")] == ["1h", "1h"],
        ", ".join(f"{label}={timeframe(label).interval}" for label in ("1M", "3M")),
    )
    check(
        "Only 3M declares a daily fallback",
        [
            (entry.label, entry.fallback_interval)
            for entry in TIMEFRAMES
            if entry.fallback_interval
        ]
        == [("3M", "1d")],
    )
    check(
        "Every timeframe declares a tick format",
        all(entry.tick_format for entry in TIMEFRAMES),
        ", ".join(f"{entry.label}:{entry.tick_format}" for entry in TIMEFRAMES),
    )
    check("The default timeframe is 1D", DEFAULT_TIMEFRAME == "1D")
    check("A label maps back to its period/interval", timeframe_args("1W") == ("5d", "5m"))
    check("A period/interval maps back to its label", label_for("1y", "1d") == "1Y")
    check("The attachment name is the one the embed points at", CHART_FILENAME == "chart.png")

    # --- x-axis ticks land on bar positions, not on timestamps
    positions = tick_positions(195)
    check(
        "Tick count sits in the brief's 4-6 range",
        4 <= len(positions) <= 6,
        f"{len(positions)} ticks for 195 bars",
    )
    check(
        "Ticks span the whole series, first bar to last",
        positions[0] == 0 and positions[-1] == 194,
        str(positions),
    )
    check(
        "Ticks are integers inside the series",
        all(isinstance(value, int) and 0 <= value < 195 for value in positions),
        str(positions),
    )
    spacing = [b - a for a, b in zip(positions, positions[1:])]
    check("Tick spacing is even", max(spacing) - min(spacing) <= 1, f"gaps={spacing}")
    check("A short series gets one tick per bar", tick_positions(3) == [0, 1, 2])
    check("An empty series gets no ticks", tick_positions(0) == [])

    def price_frame(values, start="2026-10-13 09:30", freq="2min"):
        index = pd.date_range(start, periods=len(values), freq=freq)
        return pd.DataFrame(
            {
                "Open": list(values),
                "High": [value * 1.002 for value in values],
                "Low": [value * 0.998 for value in values],
                "Close": list(values),
                "Volume": [1000.0] * len(values),
            },
            index=index,
        )

    def count_hues(buffer):
        """Rough count of clearly-green versus clearly-red line pixels."""
        buffer.seek(0)
        with Image.open(buffer) as image:
            pixels = np.asarray(image.convert("RGB")).astype(int)
        red, green = pixels[:, :, 0], pixels[:, :, 1]
        return int(((green > 120) & (green > red + 60)).sum()), int(
            ((red > 120) & (red > green + 60)).sum()
        )

    rising = price_frame([100 + index * 0.5 for index in range(60)])
    falling = price_frame([200 - index * 0.5 for index in range(60)])

    up_buffer = render_chart("AAPL", rising, "1d", "2m")
    check(
        "A chart renders to a real PNG",
        up_buffer.getvalue()[:8] == b"\x89PNG\r\n\x1a\n",
        f"{len(up_buffer.getvalue()) / 1024:.0f} KB",
    )
    check(
        "The buffer comes back rewound to index 0",
        up_buffer.tell() == 0,
        f"position {up_buffer.tell()}",
    )
    check(
        "The PNG is a sane size to attach to a message",
        5_000 < len(up_buffer.getvalue()) < 2_000_000,
    )

    up_green, up_red = count_hues(up_buffer)
    down_green, down_red = count_hues(render_chart("TSLA", falling, "1m", "1d"))
    check("An up period draws a green line", up_green > up_red, f"green={up_green}, red={up_red}")
    check("A down period draws a red line", down_red > down_green, f"green={down_green}, red={down_red}")

    check(
        "A perfectly flat series still renders",
        render_chart("FLAT", price_frame([100.0] * 30), "1d", "2m").getvalue()[:4]
        == b"\x89PNG",
    )

    # --- market-closure gaps must not become diagonal lines
    #
    # A 1Y daily series has ~55 multi-day gaps. Plotted against timestamps, each
    # one is drawn as a straight diagonal from one close to the next — a line
    # that never happened. Plotted against bar position, a price step is one bar
    # wide, so it reads as a near-vertical jump.
    #
    # The two cases are separable by measuring how far the line travels
    # vertically within a single pixel column: a near-vertical step does a whole
    # move in one column, a diagonal does a sliver per column across hundreds.
    def tallest_vertical_span(buffer):
        buffer.seek(0)
        with Image.open(buffer) as image:
            pixels = np.asarray(image.convert("RGB")).astype(int)
        red, green = pixels[:, :, 0], pixels[:, :, 1]
        mask = (green > 120) & (green > red + 60)
        tallest = 0
        for column in range(mask.shape[1]):
            rows = np.flatnonzero(mask[:, column])
            if rows.size > 1:
                tallest = max(tallest, int(rows[-1] - rows[0]))
        return tallest

    def step_frame(before=200, after=200, gap_hours=120):
        first = pd.date_range("2026-01-05 09:30", periods=before, freq="1h")
        second = pd.date_range(
            first[-1] + pd.Timedelta(hours=gap_hours), periods=after, freq="1h"
        )
        close = [100.0] * before + [140.0] * after
        return pd.DataFrame(
            {
                "Open": close,
                "High": close,
                "Low": close,
                "Close": close,
                "Volume": [1.0] * len(close),
            },
            index=first.append(second),
        )

    step_span = tallest_vertical_span(
        render_chart("STEP", step_frame(), "1mo", "1h", timeframe("1M"))
    )
    check(
        "A five-day market closure does not become a diagonal",
        step_span > 100,
        f"step climbs {step_span}px within one column (a datetime axis gives ~5px)",
    )

    for label, payload in (
        ("empty history", pd.DataFrame()),
        ("history with no Close column", pd.DataFrame({"Open": [1.0, 2.0]})),
        ("a single data point", price_frame([100.0])),
    ):
        try:
            render_chart("X", payload, "1d", "2m")
            check(f"Refuses {label}", False, "it rendered anyway")
        except ChartUnavailable:
            check(f"Refuses {label}", True)
        except Exception as error:  # noqa: BLE001
            check(f"Refuses {label}", False, f"{type(error).__name__}: {error}")

    # --- extended hours, the session trim, and the previous-close baseline
    def session_frame():
        """Wednesday's extended session, then Thursday's pre-market."""
        stamps = pd.to_datetime(
            [
                "2026-10-14 04:00",
                "2026-10-14 09:30",
                "2026-10-14 15:58",
                "2026-10-14 19:58",
                "2026-10-15 04:00",
                "2026-10-15 04:02",
            ]
        )
        close = [340.0, 341.0, 341.27, 341.9, 341.5, 341.58]
        return pd.DataFrame(
            {
                "Open": close,
                "High": close,
                "Low": close,
                "Close": close,
                "Volume": [1.0] * len(close),
            },
            index=stamps,
        )

    frame = session_frame()
    check(
        "The previous close is the last regular-hours close",
        previous_session_close(frame) == 341.27,
        f"{previous_session_close(frame)} — not the 341.90 after-hours bar",
    )
    trimmed = trim_to_last_session(frame)
    check(
        "Trimming keeps only the most recent session",
        len(trimmed) == 2 and list(trimmed.index) == list(frame.index[4:]),
        f"{len(trimmed)} of {len(frame)} bars",
    )
    check(
        "A series with no earlier session has no previous close",
        previous_session_close(trimmed) is None,
        "returns None rather than inventing one",
    )

    session_index = pd.to_datetime(
        [
            "2026-10-12 09:30",
            "2026-10-12 10:00",
            "2026-10-13 09:30",
            "2026-10-13 16:00",
            "2026-10-14 09:30",
        ]
    )
    check(
        "Day ticks land on the first bar of each session",
        day_start_positions(session_index) == [0, 2, 4],
        str(day_start_positions(session_index)),
    )
    check(
        "A single-day series gets one day tick",
        day_start_positions(session_index[:2]) == [0],
    )

    # The baseline must actually drive the comparison, not merely be accepted.
    below = count_hues(render_chart("B", frame, "5d", "2m", None, 100.0))
    above = count_hues(render_chart("B", frame, "5d", "2m", None, 400.0))
    check(
        "A baseline below the last price draws green",
        below[0] > below[1],
        f"green={below[0]}, red={below[1]}",
    )
    check(
        "A baseline above the last price draws red",
        above[1] > above[0],
        f"green={above[0]}, red={above[1]}",
    )

    # --- the interactive view
    view = StockChartView("AAPL", discord.Embed(title="quote"), DEFAULT_TIMEFRAME)
    buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
    check("The view has five buttons", len(buttons) == 5, f"{len(buttons)}")
    check(
        "Buttons read 1D, 1W, 1M, 3M, 1Y",
        [button.label for button in buttons] == ["1D", "1W", "1M", "3M", "1Y"],
        ", ".join(str(button.label) for button in buttons),
    )
    check(
        "Only the active timeframe is highlighted",
        [button.style for button in buttons]
        == [discord.ButtonStyle.primary] + [discord.ButtonStyle.secondary] * 4,
        ", ".join(str(button.style) for button in buttons),
    )
    check("The view expires after 180 seconds", view.timeout == 180, str(view.timeout))

    view.active = "3M"
    view.sync_buttons()
    moved = [item for item in view.children if isinstance(item, discord.ui.Button)]
    check(
        "The highlight follows the selected timeframe",
        [button.style for button in moved]
        == [
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.secondary,
            discord.ButtonStyle.primary,
            discord.ButtonStyle.secondary,
        ],
    )

    check(
        "The chart embed points at the attachment",
        view.chart_embed().image.url == f"attachment://{CHART_FILENAME}",
        view.chart_embed().image.url,
    )
    check(
        "The base embed is left imageless, so redraws start clean",
        view.base_embed.image.url is None,
    )

    # --- the quote embed keeps everything it had, plus Open
    quote = lookup_cog._quote_embed(
        "AAPL",
        {
            "longName": "Apple Inc.",
            "currency": "USD",
            "currentPrice": 189.5,
            "open": 188.0,
            "previousClose": 187.2,
            "dayHigh": 190.1,
            "dayLow": 187.9,
            "volume": 51_000_000,
            "marketCap": 2_950_000_000_000,
            "fullExchangeName": "NasdaqGS",
        },
    )
    check(
        "The quote embed keeps every original field and adds Open",
        [field.name for field in quote.fields]
        == [
            "Price",
            "Open",
            "Daily change",
            "Previous close",
            "Day range",
            "Volume",
            "Market cap",
        ],
        ", ".join(field.name for field in quote.fields),
    )
    check(
        "Every quote field fits Discord's cap",
        all(len(field.value) <= 1024 and len(field.name) <= 256 for field in quote.fields),
    )

    # ------------------------------------------------------------- test posts
    section("test-post labelling")

    test_briefing = ScheduledBriefings._build_embed(monday, friday, sample, is_test=True)
    check("Briefing test post is labelled", "[TEST]" in test_briefing.title)
    check(
        "Briefing test post says so in the footer",
        "TEST POST" in (test_briefing.footer.text or ""),
    )

    test_alert = MacroAlerts._alert_embed("CPIAUCSL", cpi_alert_metrics, is_test=True)
    real_alert = MacroAlerts._alert_embed("CPIAUCSL", cpi_alert_metrics)
    check(
        "Alert test post is labelled",
        test_alert.title.startswith("[TEST] "),
        test_alert.title,
    )
    check(
        "Alert test post warns the cache is unchanged",
        "cache unchanged" in (test_alert.footer.text or ""),
    )
    check(
        "Test post is visually distinct from a real alert",
        test_alert.color.value != real_alert.color.value,
    )
    check(
        "The TEST tag leaves the fields untouched",
        [field.name for field in test_alert.fields]
        == [field.name for field in real_alert.fields]
        and [field.value for field in test_alert.fields]
        == [field.value for field in real_alert.fields],
        ", ".join(field.name for field in test_alert.fields),
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

    # ---------------------------------------------------- duplicate suppression
    section("duplicate suppression")

    import tempfile
    from pathlib import Path as PathClass

    import cogs.earnings_tracker as tracker_module
    import cogs.macro_alerts as macro_module
    from cogs.earnings_tracker import EarningsTracker
    from cogs.macro_alerts import MacroAlerts

    class RecordingChannel:
        """Stands in for a Discord channel and counts what gets posted."""

        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs.get("embed"))

    recording = RecordingChannel()

    async def resolve_to_recording_channel(client, channel_id):
        return recording

    # --- macro release alerts, polled every 15 minutes -----------------------
    with tempfile.TemporaryDirectory() as tmp:
        # Redirect the on-disk cache so the real one is never touched.
        macro_module.STATE_PATH = PathClass(tmp) / "macro_state.json"
        macro_module.resolve_channel = resolve_to_recording_channel

        september = {
            "date": date(2026, 9, 1),
            "components": [
                {
                    "label": "Core PCE",
                    "short": "Core PCE",
                    "value": 130.46,
                    "mom": 0.25,
                    "prior_mom": 0.13,
                    "yoy": 3.01,
                    "prior_yoy": 2.98,
                    "annualized": None,
                    "prior_annualized": None,
                }
            ],
        }
        october = {**september, "date": date(2026, 10, 1)}

        async def returns_september(series_id):
            return september

        async def returns_october(series_id):
            return october

        async def no_metadata(series_id):
            # Stubbed so posting never reaches FRED for real.
            return None

        alerts = MacroAlerts(bot=None)
        alerts._fetch_metrics = returns_september
        alerts._fetch_series_info = no_metadata

        recording.sent.clear()
        await alerts._check_for_releases()
        check(
            "A first run seeds instead of announcing history",
            not recording.sent,
            f"{len(recording.sent)} message(s)",
        )

        alerts._fetch_metrics = returns_october
        await alerts._check_for_releases()
        after_new = len(recording.sent)
        check(
            "A new period posts exactly once per series that published",
            after_new == len(MONITORED_SERIES),
            f"{after_new} message(s) for {len(MONITORED_SERIES)} series",
        )

        # This is the user-visible worry: polling every 15 minutes must not mean
        # posting every 15 minutes.
        for _ in range(3):
            await alerts._check_for_releases()
        check(
            "Three further polls of the same release add nothing",
            len(recording.sent) == after_new,
            f"{len(recording.sent)} message(s) after 4 polls, still {after_new}",
        )

        restarted = MacroAlerts(bot=None)
        restarted._fetch_metrics = returns_october
        restarted._fetch_series_info = no_metadata
        recording.sent.clear()
        await restarted._check_for_releases()
        check(
            "A restart does not replay an announced release",
            not recording.sent,
            f"{len(recording.sent)} message(s) after restart",
        )

    # --- tracked earnings alerts, checked daily -----------------------------
    with tempfile.TemporaryDirectory() as tmp:
        tracker_module.NOTIFIED_PATH = PathClass(tmp) / "earnings_notified.json"
        tracker_module.resolve_channel = resolve_to_recording_channel

        async def calendar_with_result(day):
            return [
                {
                    "symbol": "AAPL",
                    "date": day.isoformat(),
                    "hour": "amc",
                    "quarter": 3,
                    "year": 2026,
                    "epsActual": 1.91,
                    "epsEstimate": 1.93,
                }
            ]

        tracker = EarningsTracker(bot=None)
        tracker._watchlist = {"AAPL": {"added": "2026-01-01"}}
        tracker._fetch_calendar = calendar_with_result

        recording.sent.clear()
        for _ in range(3):
            await tracker._check_today()
        check(
            "A tracked result is posted once, not on every check",
            len(recording.sent) == 1,
            f"{len(recording.sent)} message(s) after 3 checks",
        )

        restarted_tracker = EarningsTracker(bot=None)
        restarted_tracker._watchlist = {"AAPL": {"added": "2026-01-01"}}
        restarted_tracker._fetch_calendar = calendar_with_result
        recording.sent.clear()
        await restarted_tracker._check_today()
        check(
            "A restart does not repeat a tracked earnings alert",
            not recording.sent,
            f"{len(recording.sent)} message(s) after restart",
        )

    # --- posting-frequency guard --------------------------------------------
    # Only three loops exist, and each is gated. If a fourth appears, this
    # fails and someone has to decide whether it can post on every tick.
    check(
        "Exactly three background loops are registered",
        len(loops) == 3,
        ", ".join(name for name, _ in loops),
    )
    check(
        "No loop posts on a short interval",
        all(
            (loop.minutes is None or loop.minutes >= 15)
            for _, loop in loops
        ),
        "the fastest loop is the 15-minute macro poll, which posts only on new data",
    )

    # --------------------------------------------------- live fetch, opt-in
    if live:
        section("live timeframe fetch (network)")
        # Everything above proves the *drawing*. This proves the period/interval
        # pairs are ones Yahoo actually serves — which no offline check can.
        # Kept behind a flag so the default run stays fast and works anywhere.
        for entry in TIMEFRAMES:
            label = f"{entry.label} ({entry.period}/{entry.interval}) fetches and renders"
            try:
                buffer = await asyncio.to_thread(
                    generate_stock_chart,
                    "AAPL",
                    entry.period,
                    entry.interval,
                    entry.label,
                )
                payload = buffer.getvalue()
                check(
                    label,
                    payload[:8] == b"\x89PNG\r\n\x1a\n" and len(payload) > 5_000,
                    f"{len(payload) / 1024:.0f} KB",
                )
            except Exception as error:  # noqa: BLE001 - reporting the failure is the point
                check(label, False, f"{type(error).__name__}: {error}")

        # The 1D chart is only right if Yahoo actually serves extended hours —
        # the offline suite can only prove we asked for them.
        import yfinance as yf

        extended = await asyncio.to_thread(
            lambda: yf.Ticker("AAPL").history(
                period="5d", interval="2m", prepost=True
            )
        )
        before = sum(1 for stamp in extended.index if stamp.time() < dtime(9, 30))
        after = sum(1 for stamp in extended.index if stamp.time() >= dtime(16, 0))
        check(
            "Yahoo returns extended-hours bars",
            before > 0 and after > 0,
            f"{before} pre-market, {after} after-hours bars",
        )
        check(
            "A previous close is available to measure 1D against",
            previous_session_close(extended) is not None,
            str(previous_session_close(extended)),
        )
        regular = await asyncio.to_thread(
            lambda: yf.Ticker("AAPL").history(
                period="5d", interval="2m", prepost=False
            )
        )
        check(
            "Regular hours are a strict subset of extended",
            len(regular) < len(extended),
            f"{len(regular)} regular vs {len(extended)} extended bars",
        )

    # ----------------------------------------------------------------- summary
    section("summary")
    failed = RESULTS.count(False)
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(live="--live" in sys.argv)))
