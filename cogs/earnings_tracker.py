"""A user-managed earnings watch list with one daily check.

Tickers live in `state/tracked_earnings.json`. Every evening the bot asks
Finnhub which of them reported that day and posts the result once an actual
EPS has been published.
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
from utils import (
    format_compact,
    format_number,
    format_signed,
    load_json,
    resolve_channel,
    retry_async,
    run_blocking,
    save_json,
)

log = logging.getLogger(__name__)

CHECK_HOUR_ET = 17
CHECK_MINUTE_ET = 30

WATCHLIST_PATH = config.PROJECT_ROOT / "state" / "tracked_earnings.json"
NOTIFIED_PATH = config.PROJECT_ROOT / "state" / "earnings_notified.json"

# Dedupe entries older than this are dropped whenever the cache is rewritten.
NOTIFIED_RETENTION_DAYS = 45

TIMING_LABELS = {
    "bmo": "before open",
    "amc": "after close",
    "dmh": "during hours",
}


def _load_watchlist() -> dict[str, dict[str, Any]]:
    """The watch list as {symbol: {"added": "YYYY-MM-DD"}}."""
    data = load_json(WATCHLIST_PATH, {})
    if not isinstance(data, dict):
        log.warning("%s did not contain an object; starting empty", WATCHLIST_PATH)
        return {}
    return {
        str(symbol).upper(): metadata
        for symbol, metadata in data.items()
        if isinstance(metadata, dict)
    }


def _load_notified() -> dict[str, str]:
    """Alerts already sent, keyed "{SYMBOL}:{YYYY-MM-DD}" -> date sent."""
    data = load_json(NOTIFIED_PATH, {})
    if not isinstance(data, dict):
        log.warning("%s did not contain an object; starting empty", NOTIFIED_PATH)
        return {}
    return {str(key): str(value) for key, value in data.items()}


class EarningsTracker(commands.Cog):
    """Tracks a watch list of tickers and reports their earnings results."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._watchlist = _load_watchlist()
        self._notified = _load_notified()

    async def cog_load(self) -> None:
        if not config.FINNHUB_KEY:
            log.warning(
                "FINNHUB_KEY is not set — the earnings tracker's daily check is disabled"
            )
            return
        self.daily_check.start()

    async def cog_unload(self) -> None:
        self.daily_check.cancel()

    # ----------------------------------------------------------------- commands

    @app_commands.command(
        name="track_earnings",
        description="Add a ticker to the earnings watch list.",
    )
    @app_commands.describe(ticker="Ticker symbol, e.g. AAPL")
    async def track_earnings(
        self, interaction: discord.Interaction, ticker: str
    ) -> None:
        symbol = ticker.strip().upper()
        if not symbol or len(symbol) > 12 or any(
            character.isspace() for character in symbol
        ):
            await interaction.response.send_message(
                f"`{ticker.strip()}` doesn't look like a ticker symbol.", ephemeral=True
            )
            return

        if symbol in self._watchlist:
            await interaction.response.send_message(
                f"`{symbol}` is already on the watch list.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            name = await retry_async(
                self._resolve_symbol,
                symbol,
                attempts=2,
                base_delay=1.5,
                label=f"Finnhub lookup:{symbol}",
            )
        except Exception:
            log.exception("Symbol lookup failed for %s", symbol)
            await interaction.followup.send(
                "Couldn't reach Finnhub to verify that ticker. Try again in a moment.",
                ephemeral=True,
            )
            return

        if name is None:
            await interaction.followup.send(
                f"Finnhub doesn't recognise `{symbol}`. Check the spelling — it has "
                "to match the symbol Finnhub uses.",
                ephemeral=True,
            )
            return

        self._watchlist[symbol] = {"added": datetime.now(timezone.utc).date().isoformat()}
        if not save_json(WATCHLIST_PATH, self._watchlist):
            del self._watchlist[symbol]
            await interaction.followup.send(
                "Couldn't write the watch list file. Check the bot logs.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Tracking **{name}** (`{symbol}`). I'll check at "
            f"{CHECK_HOUR_ET}:{CHECK_MINUTE_ET:02d} US/Eastern each day for a report and "
            "post the result once the actual EPS is out.",
            ephemeral=True,
        )

    @app_commands.command(
        name="untrack_earnings",
        description="Remove a ticker from the earnings watch list.",
    )
    @app_commands.describe(ticker="Ticker symbol to stop tracking")
    async def untrack_earnings(
        self, interaction: discord.Interaction, ticker: str
    ) -> None:
        symbol = ticker.strip().upper()
        if symbol not in self._watchlist:
            await interaction.response.send_message(
                f"`{symbol}` isn't on the watch list.", ephemeral=True
            )
            return

        metadata = self._watchlist.pop(symbol)
        if not save_json(WATCHLIST_PATH, self._watchlist):
            # Put it back so the in-memory list still matches the file on disk.
            self._watchlist[symbol] = metadata
            await interaction.response.send_message(
                "Couldn't write the watch list file, so nothing was removed. "
                "Check the bot logs.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Stopped tracking `{symbol}`.", ephemeral=True
        )

    @app_commands.command(
        name="list_earnings",
        description="Show the earnings watch list.",
    )
    async def list_earnings(self, interaction: discord.Interaction) -> None:
        if not self._watchlist:
            await interaction.response.send_message(
                "The earnings watch list is empty. Add a ticker with "
                "`/track_earnings`.",
                ephemeral=True,
            )
            return

        lines = [
            f"**{symbol}** · added {self._watchlist[symbol].get('added', 'unknown')}"
            for symbol in sorted(self._watchlist)
        ]
        embed = discord.Embed(
            title=f"Earnings watch list ({len(lines)})",
            description="\n".join(lines),
            color=discord.Color(0x5865F2),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(
            text=f"Checked daily at {CHECK_HOUR_ET}:{CHECK_MINUTE_ET:02d} US/Eastern"
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------------------------------------------------------------- daily check

    @tasks.loop(
        time=dtime(hour=CHECK_HOUR_ET, minute=CHECK_MINUTE_ET, tzinfo=config.EASTERN)
    )
    async def daily_check(self) -> None:
        try:
            await self._check_today()
        except Exception:
            # An exception escaping a tasks.loop stops it permanently.
            log.exception("Earnings check failed; the schedule will keep running")

    @daily_check.before_loop
    async def _wait_until_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug(
                "Discord is not connected yet; the earnings check will wait for "
                "its next fire time"
            )

    async def _check_today(self) -> None:
        if not self._watchlist:
            log.info("Earnings watch list is empty; nothing to check")
            return

        today = datetime.now(config.EASTERN).date()

        channel = await resolve_channel(self.bot, config.TARGET_CHANNEL_ID)
        if channel is None:
            log.error(
                "TARGET_CHANNEL_ID (%s) could not be resolved; skipping the earnings check",
                config.TARGET_CHANNEL_ID,
            )
            return

        try:
            calendar = await retry_async(
                self._fetch_calendar,
                today,
                attempts=3,
                base_delay=5.0,
                label="Finnhub earnings calendar",
            )
        except Exception:
            log.exception("Could not fetch today's earnings calendar")
            return

        reporting = [
            entry
            for entry in calendar
            if str(entry.get("symbol") or "").upper() in self._watchlist
        ]
        if not reporting:
            log.info("None of the watched tickers report on %s", today)
            return

        posted = False
        for entry in reporting:
            symbol = str(entry["symbol"]).upper()
            key = f"{symbol}:{today.isoformat()}"
            if key in self._notified:
                continue

            if entry.get("epsActual") is None:
                # Finnhub has not published the print yet. There is only one run
                # per day, so a number that lands after this point is missed.
                log.info(
                    "%s reported on %s but no actual EPS has been published yet — "
                    "nothing posted",
                    symbol,
                    today,
                )
                continue

            try:
                await channel.send(embed=self._result_embed(symbol, entry, today))
            except discord.HTTPException:
                # Leave the dedupe key unwritten so a later run retries it.
                log.exception("Could not post the %s earnings alert", symbol)
                continue

            self._notified[key] = today.isoformat()
            posted = True
            log.info("Posted the earnings result for %s (%s)", symbol, today)

        if posted:
            self._prune_notified()
            save_json(NOTIFIED_PATH, self._notified)

    @staticmethod
    async def _fetch_calendar(day: date) -> list[dict[str, Any]]:
        """Every company Finnhub lists as reporting on `day`."""
        client = finnhub.Client(api_key=config.FINNHUB_KEY)
        payload = await run_blocking(
            client.earnings_calendar,
            _from=day.isoformat(),
            to=day.isoformat(),
            symbol="",
            international=False,
        )
        if not isinstance(payload, dict):
            return []
        return payload.get("earningsCalendar") or []

    @staticmethod
    async def _resolve_symbol(symbol: str) -> str | None:
        """Confirm Finnhub knows the symbol, returning its display name."""
        client = finnhub.Client(api_key=config.FINNHUB_KEY)
        payload = await run_blocking(client.symbol_lookup, symbol)
        if not isinstance(payload, dict):
            return None
        # Search is fuzzy, so accept an exact symbol match only — otherwise a
        # typo silently tracks the wrong company.
        for result in payload.get("result") or []:
            if str(result.get("symbol") or "").upper() == symbol:
                return result.get("description") or symbol
        return None

    def _prune_notified(self) -> None:
        """Drop dedupe keys older than the retention window."""
        cutoff = (
            datetime.now(timezone.utc).date() - timedelta(days=NOTIFIED_RETENTION_DAYS)
        ).isoformat()
        self._notified = {
            key: value for key, value in self._notified.items() if value >= cutoff
        }

    # --------------------------------------------------------------- rendering

    @classmethod
    def _result_embed(
        cls, symbol: str, entry: dict[str, Any], day: date
    ) -> discord.Embed:
        actual = entry.get("epsActual")
        estimate = entry.get("epsEstimate")
        timing = TIMING_LABELS.get(str(entry.get("hour") or "").lower(), "")

        color = discord.Color(0xF1C40F)
        surprise_text = "N/A"
        if isinstance(actual, (int, float)) and isinstance(estimate, (int, float)):
            surprise = actual - estimate
            color = (
                discord.Color(0x2ECC71) if surprise >= 0 else discord.Color(0xE74C3C)
            )
            surprise_text = format_signed(surprise, 2)
            if estimate:
                surprise_text += f" ({format_signed(surprise / abs(estimate) * 100, 1, '%')})"

        embed = discord.Embed(
            title=f"Earnings: {symbol}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.description = (
            f"Reported {timing} on {day:%B} {day.day}, {day:%Y}."
            if timing
            else f"Reported on {day:%B} {day.day}, {day:%Y}."
        )

        if entry.get("quarter") is not None and entry.get("year") is not None:
            embed.add_field(
                name="Period", value=f"Q{entry['quarter']} {entry['year']}", inline=True
            )
        embed.add_field(name="Actual EPS", value=format_number(actual, 2), inline=True)
        embed.add_field(name="Consensus EPS", value=format_number(estimate, 2), inline=True)
        embed.add_field(name="Surprise", value=surprise_text, inline=True)

        if entry.get("revenueActual") is not None:
            embed.add_field(
                name="Revenue (actual)",
                value=format_compact(entry["revenueActual"]),
                inline=True,
            )
        if entry.get("revenueEstimate") is not None:
            embed.add_field(
                name="Revenue (consensus)",
                value=format_compact(entry["revenueEstimate"]),
                inline=True,
            )

        embed.set_footer(text="Source: Finnhub earnings calendar · times are US/Eastern")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EarningsTracker(bot))
