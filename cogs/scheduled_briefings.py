"""The Monday morning earnings briefing.

Runs as a `discord.ext.tasks` loop on the bot's own event loop, so it fires
without any external scheduler.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any

import discord
import finnhub
import requests
import yfinance as yf
from discord import app_commands
from discord.ext import commands, tasks

import config
from cogs.earnings_tracker import load_watchlist
from utils import (
    parse_iso_date,
    resolve_channel,
    retry_async,
    run_blocking,
    timing_label,
    to_pacific_clock,
)

log = logging.getLogger(__name__)

BRIEFING_HOUR_ET = 8

# Names readers are most likely to act on. Anything here is shown first and
# marked with a star; everything else fills the remaining space.
HIGH_INTEREST = (
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "META", "TSLA",
    "AMD", "AVGO", "NFLX", "ORCL", "CRM", "ADBE", "INTC", "QCOM", "MU",
    "TSM", "JPM", "BAC", "GS", "MS", "V", "MA", "XOM", "CVX", "WMT",
    "COST", "DIS", "BA", "UBER", "COIN", "PLTR", "SMCI", "ARM", "SHOP", "SNOW",
)
HIGH_INTEREST_RANK = {ticker: index for index, ticker in enumerate(HIGH_INTEREST)}

# Discord caps a field value at 1024 characters, so the listing is bounded
# twice: per day and across the whole embed.
# The listing is filtered rather than capped, so no per-day line limit is needed
# any more. This is a hard character ceiling for one day's field, leaving
# headroom inside Discord's 1024 limit; it only binds on a freak day.
DAY_FIELD_BUDGET = 1000

# Tier-1 US releases, matched against FRED's *exact* release name. Substring
# matching would also catch "Sticky Price CPI", "Gross Domestic Product by
# State" and "Trimmed Mean PCE Inflation Rate", none of which is the headline
# print. Fields are (FRED release name, display label, hour ET, minute ET).
#
# FRED publishes release dates but no times, so each entry carries the fixed
# time the issuing agency uses — BLS and BEA both publish at 08:30 ET. The
# Pacific rendering is derived from that.
#
# FOMC rate decisions are deliberately absent. FRED's only FOMC release is
# "FOMC Press Release", which carries a date for *every* day of the year (365 in
# 2026) because it also covers routine speeches and statements, so it cannot
# pick out a rate decision. The fed funds target series (DFEDTARU and friends)
# resolve to that same release, and a future meeting day cannot be derived from
# anything FRED publishes. Adding FOMC back needs a dated meeting calendar, not
# a data feed.
TIER1_RELEASES = (
    ("consumer price index", "CPI Inflation", 8, 30),
    ("personal income and outlays", "Core PCE Price Index", 8, 30),
    ("gross domestic product", "Real GDP", 8, 30),
    ("employment situation", "Non-Farm Payrolls", 8, 30),
)

# FRED's release-dates endpoint, used rather than Finnhub's /calendar/economic,
# which returns HTTP 403 on a free-tier key.
FRED_RELEASES_URL = "https://api.stlouisfed.org/fred/releases/dates"

# The Monday market snapshot. Each entry is (yfinance symbol, display label,
# value kind); the kind decides how the number is rendered.
SNAPSHOT_GROUPS: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Crypto",
        (
            ("BTC-USD", "BTC", "usd"),
            ("ETH-USD", "ETH", "usd"),
            # Yahoo lists Hyperliquid under a numeric-suffixed symbol; plain
            # "HYPE-USD" returns no data at all.
            ("HYPE32196-USD", "HYPE", "usd"),
            ("XMR-USD", "XMR", "usd"),
        ),
    ),
    (
        "Equities",
        (
            ("SPY", "SP500", "usd"),
            ("QQQ", "QQQ", "usd"),
            # URTH (MSCI World, developed markets) rather than VT, so it does
            # not overlap with the emerging-markets line below.
            ("URTH", "World", "usd"),
            ("EEM", "EEM", "usd"),
        ),
    ),
    (
        "Bonds",
        (
            ("HYG", "HYG", "usd"),
            # ^TNX already quotes the yield in percent: 4.23 means 4.23%.
            ("^TNX", "US 10Y Yield", "percent"),
        ),
    ),
    (
        "Commodities",
        (
            ("CL=F", "Crude Oil", "usd"),
            ("GC=F", "Gold", "usd"),
            ("SI=F", "Silver", "usd"),
        ),
    ),
    (
        "Currencies",
        (
            ("EURUSD=X", "EUR/USD", "rate"),
            ("JPY=X", "USD/JPY", "rate"),
            ("AUDUSD=X", "AUD/USD", "rate"),
            ("CHF=X", "USD/CHF", "rate"),
        ),
    ),
)

# Discord caps a field value at 1024 characters, and a code block cannot be
# split across two fields, so whole groups are packed into blocks under this.
SNAPSHOT_FIELD_BUDGET = 980


def _format_snapshot_value(value: float | None, kind: str) -> str:
    if value is None:
        return "N/A"
    if kind == "percent":
        return f"{value:.2f}%"
    if kind == "rate":
        # Majors are quoted to four decimals, but a pair like USD/JPY trades in
        # the hundreds and is conventionally shown to two.
        return f"{value:,.4f}" if abs(value) < 50 else f"{value:,.2f}"
    # Dollar-quoted assets: sub-dollar coins need more precision than indices do.
    decimals = 2 if abs(value) >= 1 else 6
    return f"${value:,.{decimals}f}"


def _format_snapshot_change(percent: float | None) -> str:
    return "N/A" if percent is None else f"{percent:+.2f}%"


def _symbol_of(entry: dict[str, Any]) -> str:
    return str(entry.get("symbol") or "").strip().upper()


def _is_high_interest(entry: dict[str, Any]) -> bool:
    return _symbol_of(entry) in HIGH_INTEREST_RANK


def _is_listed(entry: dict[str, Any], tracked: set[str]) -> bool:
    """Whether a company earns a line in the briefing.

    Only high-interest names and tickers the user explicitly tracks. Everything
    else is left out — the calendar is mostly uncovered micro-caps, and listing
    them buried the handful of names that actually matter.
    """
    symbol = _symbol_of(entry)
    return symbol in HIGH_INTEREST_RANK or symbol in tracked


def _tracked_symbols() -> set[str]:
    """Symbols followed with /track_earnings, re-read so new ones are picked up."""
    try:
        return set(load_watchlist())
    except Exception:  # noqa: BLE001 - a broken watch list must not stop the briefing
        log.warning("Could not read the tracked earnings list", exc_info=True)
        return set()


def tier1_match(release_name: str) -> tuple[str, int, int] | None:
    """Map an exact FRED release name to (label, hour ET, minute ET)."""
    lowered = (release_name or "").strip().lower()
    for name, label, hour, minute in TIER1_RELEASES:
        if lowered == name:
            return label, hour, minute
    return None


def _fetch_release_dates_sync(monday: date, friday: date) -> list[dict[str, Any]]:
    """FRED release dates inside the window. Blocking — call via run_blocking."""
    response = requests.get(
        FRED_RELEASES_URL,
        params={
            "api_key": config.FRED_API_KEY,
            "file_type": "json",
            "realtime_start": monday.isoformat(),
            "realtime_end": friday.isoformat(),
            # A future release has no observations yet, so FRED omits it unless
            # this is set.
            "include_release_dates_with_no_data": "true",
            "limit": 1000,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return []
    return payload.get("release_dates") or []


class BriefingNotSent(Exception):
    """The briefing could not be produced. The message is safe to show a user."""


class ScheduledBriefings(commands.Cog):
    """Posts a look-ahead at the week's earnings every Monday morning."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # The tz-aware time is what keeps this at 08:00 Eastern through the March
    # and November DST changes; discord.py recomputes the UTC offset each day.
    @tasks.loop(time=dtime(hour=BRIEFING_HOUR_ET, minute=0, tzinfo=config.EASTERN))
    async def weekly_briefing(self) -> None:
        # The loop fires daily at 08:00 ET; the briefing itself is Mondays only.
        today = datetime.now(config.EASTERN).date()
        if today.weekday() != 0:
            return

        try:
            await self._post_briefing(today, today + timedelta(days=4))
        except BriefingNotSent as error:
            log.error("Weekly briefing not sent: %s", error)
        except Exception:
            # An exception escaping a tasks.loop stops it permanently, so this
            # catch is what keeps next Monday working.
            log.exception("Weekly briefing failed; the schedule will keep running")

    @weekly_briefing.before_loop
    async def _wait_until_ready(self) -> None:
        """Hold the loop until Discord is connected.

        `wait_until_ready()` raises RuntimeError when the client was never
        logged in. In production the wait always succeeds; the handler covers
        the offline case (verify.py loads cogs without connecting) and, more
        usefully, stops a startup race from killing the loop permanently.
        """
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("Discord is not connected yet; the briefing loop will wait for its next fire time")

    async def cog_load(self) -> None:
        self.weekly_briefing.start()

    async def cog_unload(self) -> None:
        self.weekly_briefing.cancel()

    # ----------------------------------------------------------------- commands

    @app_commands.command(
        name="test_briefing",
        description="Send the weekly earnings briefing now, without waiting for Monday.",
    )
    async def test_briefing(self, interaction: discord.Interaction) -> None:
        # Ephemeral so only the caller sees the confirmation; the briefing itself
        # goes to the target channel like the real one.
        await interaction.response.defer(thinking=True, ephemeral=True)

        monday, friday = self._briefing_week(datetime.now(config.EASTERN).date())
        try:
            await self._post_briefing(monday, friday, is_test=True)
        except BriefingNotSent as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception:
            log.exception("Test briefing failed")
            await interaction.followup.send(
                "Something went wrong building the briefing. Check the bot logs.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Posted a test briefing for {monday:%b} {monday.day} – "
            f"{friday:%b} {friday.day} to <#{config.TARGET_CHANNEL_ID}>.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ posting

    @staticmethod
    def _briefing_week(today: date) -> tuple[date, date]:
        """The Monday–Friday window to report on.

        Normally the week containing `today`. On a weekend that week is already
        over, so roll forward to the coming week — otherwise a manual test run
        on a Saturday could only ever report "no earnings".
        """
        monday = today - timedelta(days=today.weekday())
        if today.weekday() >= 5:
            monday += timedelta(days=7)
        return monday, monday + timedelta(days=4)

    async def _post_briefing(
        self, monday: date, friday: date, *, is_test: bool = False
    ) -> None:
        """Build and post the briefing, raising BriefingNotSent if it cannot go out.

        The two callers want different failure handling — the scheduled loop logs
        and moves on, the slash command tells the user what went wrong — so every
        failure surfaces as one exception type with a user-safe message.
        """
        channel = await resolve_channel(self.bot, config.TARGET_CHANNEL_ID)
        if channel is None:
            raise BriefingNotSent(
                f"Couldn't resolve channel `{config.TARGET_CHANNEL_ID}`. Check "
                "`TARGET_CHANNEL_ID`, and that the bot can see that channel."
            )

        try:
            calendar = await retry_async(
                self._fetch_calendar,
                monday,
                friday,
                attempts=3,
                base_delay=5.0,
                label="Finnhub earnings calendar",
            )
        except Exception as error:
            raise BriefingNotSent(
                "Couldn't reach the Finnhub earnings calendar. Check the bot logs "
                "for the underlying error."
            ) from error

        try:
            snapshot = await retry_async(
                self._fetch_snapshot,
                attempts=2,
                base_delay=2.0,
                label="market snapshot",
            )
        except Exception:
            # The snapshot is context, not the point of the briefing, so a Yahoo
            # outage must not cost the week's post. Missing quotes render "N/A".
            log.exception("Market snapshot unavailable; posting the briefing without it")
            snapshot = {}

        try:
            macro_events = await retry_async(
                self._fetch_macro_calendar,
                monday,
                friday,
                attempts=2,
                base_delay=3.0,
                label="FRED release calendar",
            )
        except Exception:
            # None rather than []: the section must be able to say "unavailable"
            # instead of claiming nothing is scheduled this week.
            log.exception("Macro calendar unavailable; posting the briefing without it")
            macro_events = None

        try:
            await channel.send(
                embed=self._build_embed(
                    monday,
                    friday,
                    calendar,
                    snapshot,
                    macro_events,
                    _tracked_symbols(),
                    is_test=is_test,
                )
            )
        except discord.HTTPException as error:
            raise BriefingNotSent(
                "Discord rejected the message. Check the bot has **Send Messages** "
                "and **Embed Links** in that channel."
            ) from error

        log.info(
            "%s the weekly earnings briefing for %s",
            "Test-posted" if is_test else "Posted",
            monday.isoformat(),
        )

    @staticmethod
    async def _fetch_calendar(monday: date, friday: date) -> list[dict[str, Any]]:
        """Earnings scheduled between `monday` and `friday` inclusive."""
        client = finnhub.Client(api_key=config.FINNHUB_KEY)
        payload = await run_blocking(
            client.earnings_calendar,
            _from=monday.isoformat(),
            to=friday.isoformat(),
            symbol="",
            international=False,
        )
        if not isinstance(payload, dict):
            return []
        return payload.get("earningsCalendar") or []

    # ----------------------------------------------------------- market snapshot

    @classmethod
    async def _fetch_snapshot(cls) -> dict[str, tuple[float | None, float | None]]:
        """Latest price and previous close for every tracked asset."""
        tickers = [ticker for _, assets in SNAPSHOT_GROUPS for ticker, _, _ in assets]
        return await run_blocking(cls._fetch_snapshot_sync, tickers)

    @staticmethod
    def _fetch_snapshot_sync(
        tickers: list[str],
    ) -> dict[str, tuple[float | None, float | None]]:
        """One batched yfinance request for all assets, not one call per ticker.

        A request per ticker would mean seventeen round trips; the batched
        download returns every series in a single frame.
        """
        frame = yf.download(
            tickers=tickers,
            period="5d",
            interval="1d",
            auto_adjust=False,  # raw closes, so the daily change is not skewed by dividends
            progress=False,
            threads=True,
        )
        if frame is None or getattr(frame, "empty", True) or "Close" not in frame:
            return {}

        close = frame["Close"]
        if getattr(close, "columns", None) is None:
            # yfinance drops the outer column level when there is only one ticker.
            close = close.to_frame(name=tickers[0])

        quotes: dict[str, tuple[float | None, float | None]] = {}
        for ticker in tickers:
            if ticker not in close.columns:
                quotes[ticker] = (None, None)  # symbol delisted or unavailable
                continue
            series = close[ticker].dropna()
            if series.empty:
                quotes[ticker] = (None, None)
                continue
            latest = float(series.iloc[-1])
            previous = float(series.iloc[-2]) if len(series) > 1 else None
            quotes[ticker] = (latest, previous)
        return quotes

    @staticmethod
    def _snapshot_blocks(
        snapshot: dict[str, tuple[float | None, float | None]],
    ) -> list[str]:
        """Render the snapshot as one or more code blocks.

        Whole groups are packed into each block, because a code block cannot be
        split across two embed fields without breaking the fence.
        """
        rendered_groups: list[str] = []
        for group_name, assets in SNAPSHOT_GROUPS:
            lines = [group_name]
            for index, (ticker, label, kind) in enumerate(assets):
                branch = "└" if index == len(assets) - 1 else "├"
                price, previous = snapshot.get(ticker, (None, None))
                percent = (
                    (price - previous) / previous * 100
                    if price is not None and previous
                    else None
                )
                lines.append(
                    f"{branch} {label}: {_format_snapshot_value(price, kind)} "
                    f"| {_format_snapshot_change(percent)}"
                )
            rendered_groups.append("\n".join(lines))

        blocks: list[str] = []
        current: list[str] = []
        current_length = 0
        for group in rendered_groups:
            added = len(group) + (1 if current else 0)
            if current and current_length + added > SNAPSHOT_FIELD_BUDGET:
                blocks.append("```\n" + "\n".join(current) + "\n```")
                current, current_length, added = [], 0, len(group)
            current.append(group)
            current_length += added
        if current:
            blocks.append("```\n" + "\n".join(current) + "\n```")
        return blocks

    # ---------------------------------------------------------- macro calendar

    @classmethod
    async def _fetch_macro_calendar(
        cls, monday: date, friday: date
    ) -> list[dict[str, Any]]:
        """Tier-1 US releases scheduled between `monday` and `friday`."""
        raw = await run_blocking(_fetch_release_dates_sync, monday, friday)

        events: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            match = tier1_match(str(entry.get("release_name") or ""))
            if match is None:
                continue
            label, hour, minute = match
            day = parse_iso_date(entry.get("date"))
            if day is None or not monday <= day <= friday:
                continue
            events.append(
                {
                    "date": day,
                    "label": label,
                    # Anchored to the release's own date, so the standard or
                    # daylight offset applied is the right one.
                    "clock": to_pacific_clock(hour, minute, day),
                }
            )

        events.sort(key=lambda event: (event["date"], event["clock"]))
        return events

    @staticmethod
    def _macro_field(events: list[dict[str, Any]] | None) -> str:
        """The macro section body. `None` means the fetch failed, not that the
        week is empty — those two must not look the same."""
        if events is None:
            return "*Unavailable — couldn't reach FRED's release calendar.*"
        if not events:
            return "*No major tier-1 macro releases scheduled this week.*"
        return "\n".join(
            f"{event['date']:%A, %b} {event['date'].day} · {event['label']} "
            f"· {event['clock']} PT"
            for event in events
        )

    # ---------------------------------------------------------------- rendering

    @classmethod
    def _build_embed(
        cls,
        monday: date,
        friday: date,
        entries: list[dict[str, Any]],
        snapshot: dict[str, tuple[float | None, float | None]] | None = None,
        macro_events: list[dict[str, Any]] | None = None,
        tracked: set[str] | None = None,
        *,
        is_test: bool = False,
    ) -> discord.Embed:
        test_note = " · TEST POST" if is_test else ""
        tracked = tracked or set()
        by_day: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            day = entry.get("date")
            symbol = str(entry.get("symbol") or "").strip().upper()
            if not day or not symbol:
                continue
            by_day.setdefault(day, []).append(entry)

        embed = discord.Embed(
            title=(
                f"{'[TEST] ' if is_test else ''}\U0001f4c5 Week Ahead: Earnings — "
                f"{monday:%b} {monday.day} to {friday:%b} {friday.day}, {friday.year}"
            ),
            # Amber marks a test post so it is never mistaken for the real Monday run.
            color=discord.Color(0xF1C40F) if is_test else discord.Color(0x5865F2),
            timestamp=datetime.now(timezone.utc),
        )

        total = sum(len(day_entries) for day_entries in by_day.values())
        if total == 0:
            embed.description = "No earnings scheduled this week."
        else:
            embed.description = (
                f"**{total}** companies report Monday–Friday.\n"
                "*Listed: ⭐ high-interest names, plus anything you track with "
                "`/track_earnings`. Everything else is left out to keep this "
                "readable.*"
            )

            week = [monday + timedelta(days=offset) for offset in range(5)]
            reporting_days = [day for day in week if day.isoformat() in by_day]

            for day in reporting_days:
                day_entries = sorted(
                    by_day.get(day.isoformat(), []), key=cls._sort_key
                )
                lines, dropped = cls._render_day(
                    [entry for entry in day_entries if _is_listed(entry, tracked)]
                )

                if not lines:
                    # The day has reports, but none worth listing. Say so rather
                    # than dropping the day silently — a vanished day is
                    # indistinguishable from a bug.
                    lines = ["*No high-interest or tracked companies.*"]
                elif dropped:
                    lines.append(f"*…and {dropped} more*")

                embed.add_field(
                    name=f"{day:%A} · {day:%b} {day.day}",
                    value="\n".join(lines),
                    inline=False,
                )

        embed.add_field(
            name="\U0001f4c5 Macro Economic Calendar This Week",
            value=cls._macro_field(macro_events),
            inline=False,
        )

        # The snapshot goes last: the earnings detail is the point of the post,
        # so the week's actual news should be the first thing read.
        for index, block in enumerate(cls._snapshot_blocks(snapshot or {})):
            embed.add_field(
                name=(
                    "\U0001f4ca Market Snapshot (Monday Open)"
                    if index == 0
                    else "\U0001f4ca Market Snapshot (continued)"
                ),
                value=block,
                inline=False,
            )

        embed.set_footer(
            text=(
                "Sources: Finnhub earnings calendar, Yahoo Finance snapshot "
                f"· times are US/Pacific{test_note}"
            )
        )
        return embed

    @staticmethod
    def _sort_key(entry: dict[str, Any]) -> tuple[int, int, str]:
        """High-interest names first, then everything else alphabetically."""
        symbol = str(entry.get("symbol") or "").strip().upper()
        if symbol in HIGH_INTEREST_RANK:
            return (0, HIGH_INTEREST_RANK[symbol], symbol)
        return (1, 0, symbol)

    @staticmethod
    def _format_entry(entry: dict[str, Any]) -> str:
        """One company on one line, e.g.

        '⭐ JPM · Q3 · EPS est $5.92 · Pre-Market (~05:30-06:30 PT)'

        The year is left out of the quarter — the title already carries the date
        range — and every optional part is dropped when the API leaves it null.
        """
        symbol = _symbol_of(entry)
        parts = ["⭐ " + symbol if _is_high_interest(entry) else symbol]

        if entry.get("quarter") is not None:
            parts.append(f"Q{entry['quarter']}")

        estimate = entry.get("epsEstimate")
        if isinstance(estimate, (int, float)):
            # Finnhub does not say which currency an estimate is in. The vast
            # majority of names on this calendar report in USD.
            parts.append(f"EPS est ${estimate:,.2f}")

        timing = timing_label(str(entry.get("hour") or ""))
        if timing:
            parts.append(timing)

        return " · ".join(parts)

    @classmethod
    def _render_day(cls, entries: list[dict[str, Any]]) -> tuple[list[str], int]:
        """Format one day's lines, and report how many could not fit the field."""
        lines: list[str] = []
        used = 0
        for index, entry in enumerate(entries):
            line = cls._format_entry(entry)
            if used + len(line) + 1 > DAY_FIELD_BUDGET:
                # Only reachable on a freak day — the listing is filtered down to
                # a handful of names before it gets here.
                return lines, len(entries) - index
            lines.append(line)
            used += len(line) + 1
        return lines, 0


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScheduledBriefings(bot))
