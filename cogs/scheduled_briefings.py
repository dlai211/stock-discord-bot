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
            await self._post_briefing(today)
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

    # ------------------------------------------------------------------ posting

    async def _post_briefing(self, today: date) -> None:
        monday = today - timedelta(days=today.weekday())
        friday = monday + timedelta(days=4)

        channel = await resolve_channel(self.bot, config.TARGET_CHANNEL_ID)
        if channel is None:
            log.error(
                "TARGET_CHANNEL_ID (%s) could not be resolved; skipping this week's briefing",
                config.TARGET_CHANNEL_ID,
            )
            return

        try:
            calendar = await retry_async(
                self._fetch_calendar,
                monday,
                friday,
                attempts=3,
                base_delay=5.0,
                label="Finnhub earnings calendar",
            )
        except Exception:
            # Deliberately not re-raised: a provider outage should cost one
            # week's briefing, not the whole schedule.
            log.exception("Could not fetch the earnings calendar; skipping this week")
            return

        try:
            await channel.send(embed=self._build_embed(monday, friday, calendar))
            log.info("Posted the weekly earnings briefing for %s", monday.isoformat())
        except discord.HTTPException:
            log.exception(
                "Could not post the briefing to channel %s", config.TARGET_CHANNEL_ID
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
        cls, monday: date, friday: date, entries: list[dict[str, Any]]
    ) -> discord.Embed:
        by_day: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            day = entry.get("date")
            symbol = str(entry.get("symbol") or "").strip().upper()
            if not day or not symbol:
                continue
            by_day.setdefault(day, []).append(entry)

        embed = discord.Embed(
            title=(
                f"\U0001f4c5 Week Ahead: Earnings — "
                f"{monday:%b} {monday.day} to {friday:%b} {friday.day}, {friday.year}"
            ),
            color=discord.Color(0x5865F2),
            timestamp=datetime.now(timezone.utc),
        )

        total = sum(len(day_entries) for day_entries in by_day.values())
        if total == 0:
            embed.description = "No earnings scheduled this week."
            embed.set_footer(text="Source: Finnhub earnings calendar")
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
            text="Source: Finnhub earnings calendar · times are US/Eastern"
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
