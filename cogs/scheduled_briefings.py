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
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import resolve_channel, retry_async, run_blocking

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
MAX_PER_DAY = 12
MAX_TOTAL = 25

TIMING_LABELS = {
    "bmo": "before open",
    "amc": "after close",
    "dmh": "during hours",
}


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
            await channel.send(
                embed=self._build_embed(monday, friday, calendar, is_test=is_test)
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

    # ---------------------------------------------------------------- rendering

    @classmethod
    def _build_embed(
        cls,
        monday: date,
        friday: date,
        entries: list[dict[str, Any]],
        *,
        is_test: bool = False,
    ) -> discord.Embed:
        test_note = " · TEST POST" if is_test else ""
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
            embed.set_footer(text=f"Source: Finnhub earnings calendar{test_note}")
            return embed

        watched = sum(
            1
            for entry in entries
            if str(entry.get("symbol") or "").strip().upper() in HIGH_INTEREST_RANK
        )
        embed.description = (
            f"**{total}** companies report Monday–Friday, **{watched}** on the "
            "high-interest list. ⭐ marks the high-interest names."
        )

        remaining = MAX_TOTAL
        for offset in range(5):
            day = monday + timedelta(days=offset)
            day_entries = sorted(
                by_day.get(day.isoformat(), []), key=cls._sort_key
            )
            if not day_entries:
                continue

            take = min(len(day_entries), MAX_PER_DAY, remaining)
            lines = [cls._format_entry(entry) for entry in day_entries[:take]]
            hidden = len(day_entries) - take
            if hidden > 0:
                lines.append(f"*…and {hidden} more*")

            embed.add_field(
                name=f"{day:%A} · {day:%b} {day.day}",
                value="\n".join(lines),
                inline=False,
            )

            remaining -= take
            if remaining <= 0:
                break

        embed.set_footer(
            text=f"Source: Finnhub earnings calendar · times are US/Eastern{test_note}"
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
        symbol = str(entry.get("symbol") or "").strip().upper()
        star = "⭐ " if symbol in HIGH_INTEREST_RANK else ""

        quarter = ""
        if entry.get("quarter") is not None and entry.get("year") is not None:
            quarter = f"Q{entry['quarter']} {entry['year']}"

        estimate = entry.get("epsEstimate")
        eps = f"EPS est {estimate:,.2f}" if isinstance(estimate, (int, float)) else ""

        timing = TIMING_LABELS.get(str(entry.get("hour") or "").lower(), "")

        details = " · ".join(part for part in (quarter, eps, timing) if part)
        return f"{star}**{symbol}**" + (f" — {details}" if details else "")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ScheduledBriefings(bot))
