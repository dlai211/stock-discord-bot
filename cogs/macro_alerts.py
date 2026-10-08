"""Watches FRED for freshly published macro data and posts an alert.

FRED has no push API, so this polls on an interval and compares the newest
observation date against a cache on disk. The cache is what makes a restart
safe: the bot only ever announces a period it has not announced before.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord.ext import commands, tasks
from fredapi import Fred

import config
from utils import (
    format_number,
    format_signed,
    resolve_channel,
    retry_async,
    run_blocking,
)

log = logging.getLogger(__name__)

POLL_MINUTES = 15

MONITORED_SERIES: dict[str, dict[str, Any]] = {
    "CPIAUCSL": {
        "label": "CPI (All Urban Consumers)",
        "category": "Inflation — Consumer Price Index",
        "periods_per_year": 12,
    },
    "PCEPILFE": {
        "label": "Core PCE Price Index",
        "category": "Inflation — Core PCE (excl. food & energy)",
        "periods_per_year": 12,
    },
    "GDPC1": {
        "label": "Real Gross Domestic Product",
        "category": "Growth — Real GDP",
        "periods_per_year": 4,
    },
}

# Minutes past midnight US/Eastern. These label an alert with the window it
# landed in; the on-disk cache — not the window — is what prevents duplicates.
#
# Only the 08:30 window can currently produce an alert, because none of the
# three monitored series is a rate decision. To make the FOMC window functional,
# add DFEDTARU (fed funds target range, upper limit) to MONITORED_SERIES.
RELEASE_WINDOWS = (
    (8 * 60 + 25, 9 * 60 + 30, "08:30 ET window (CPI / PCE / GDP)"),
    (13 * 60 + 55, 14 * 60 + 45, "14:00 ET window (FOMC)"),
)

STATE_PATH = config.PROJECT_ROOT / "state" / "macro_state.json"


def _load_state() -> dict[str, str]:
    """Read the seen-observation-date cache, tolerating a missing or corrupt file."""
    try:
        with STATE_PATH.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning(
            "Could not read %s; starting from an empty cache", STATE_PATH, exc_info=True
        )
        return {}

    if not isinstance(data, dict):
        log.warning("%s did not contain an object; starting from an empty cache", STATE_PATH)
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _save_state(state: dict[str, str]) -> None:
    """Write the cache atomically so a crash mid-write cannot corrupt it."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = STATE_PATH.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
        temporary.replace(STATE_PATH)
    except OSError:
        log.warning("Could not write %s", STATE_PATH, exc_info=True)


class MacroAlerts(commands.Cog):
    """Announces a FRED series the first time a new period shows up."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._fred = Fred(api_key=config.FRED_API_KEY) if config.FRED_API_KEY else None
        self._state = _load_state()

    async def cog_load(self) -> None:
        if self._fred is None:
            log.warning("FRED_API_KEY is not set — macro release alerts are disabled")
            return
        self.poll_macro_releases.start()

    async def cog_unload(self) -> None:
        self.poll_macro_releases.cancel()

    @tasks.loop(minutes=POLL_MINUTES)
    async def poll_macro_releases(self) -> None:
        try:
            await self._check_for_releases()
        except Exception:
            # Nothing may escape: an unhandled exception stops a tasks.loop for good.
            log.exception("Macro poll failed; the loop will try again next interval")

    @poll_macro_releases.before_loop
    async def _wait_until_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            log.debug("Discord is not connected yet; the poll loop will wait for its first interval")

    # ------------------------------------------------------------------ polling

    async def _check_for_releases(self) -> None:
        channel = await resolve_channel(self.bot, config.TARGET_CHANNEL_ID)
        if channel is None:
            log.error(
                "TARGET_CHANNEL_ID (%s) could not be resolved; skipping this poll",
                config.TARGET_CHANNEL_ID,
            )
            return

        window = self._active_window()
        pending: list[tuple[str, dict[str, Any]]] = []
        seeded: list[str] = []

        for series_id in MONITORED_SERIES:
            try:
                observation = await retry_async(
                    self._fetch_latest,
                    series_id,
                    attempts=3,
                    base_delay=3.0,
                    label=f"FRED:{series_id}",
                )
            except Exception:
                # One dead series must not stop the others from being checked.
                log.exception(
                    "Could not read FRED series %s; will retry next poll", series_id
                )
                continue

            if observation is None:
                continue

            seen = self._state.get(series_id)
            if seen is None:
                # First sighting of this series: record where it stands without
                # announcing, so switching the bot on does not replay months of
                # old releases into the channel.
                self._state[series_id] = observation["date"].isoformat()
                seeded.append(series_id)
                continue

            # ISO dates compare correctly as plain strings.
            if observation["date"].isoformat() > seen:
                pending.append((series_id, observation))

        posted = False
        for series_id, observation in pending:
            try:
                await channel.send(
                    embed=self._alert_embed(series_id, observation, window)
                )
            except discord.HTTPException:
                # Cache stays untouched, so the next poll retries this release.
                log.exception("Could not post the %s alert; it will be retried", series_id)
                continue
            self._state[series_id] = observation["date"].isoformat()
            posted = True
            log.info("Posted macro alert: %s (%s)", series_id, observation["date"])

        if seeded or posted:
            _save_state(self._state)
        if seeded:
            log.info("Seeded the macro cache (no alerts sent) for: %s", ", ".join(seeded))

    async def _fetch_latest(self, series_id: str) -> dict[str, Any] | None:
        """Newest observation, plus the prior period and the year-ago reading."""
        periods_per_year = MONITORED_SERIES[series_id]["periods_per_year"]
        # Enough history for a year-over-year comparison plus one prior period.
        lookback_days = int(365 * (12 / periods_per_year)) + 120
        start = (
            datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
        ).isoformat()

        frame = await run_blocking(
            self._fred.get_series, series_id, observation_start=start
        )
        # FRED pads a series with NaN for periods it has not published yet.
        clean = frame.dropna() if frame is not None else None
        if clean is None or clean.empty:
            log.warning("FRED series %s returned no usable observations", series_id)
            return None

        previous_index = len(clean) - 2
        year_ago_index = len(clean) - 1 - periods_per_year
        return {
            "date": clean.index[-1].date(),
            "value": float(clean.iloc[-1]),
            "previous": float(clean.iloc[previous_index]) if previous_index >= 0 else None,
            "year_ago": float(clean.iloc[year_ago_index]) if year_ago_index >= 0 else None,
        }

    # ---------------------------------------------------------------- rendering

    @staticmethod
    def _active_window() -> str | None:
        """Label for the release window we are currently inside, if any."""
        now = datetime.now(config.EASTERN)
        minute_of_day = now.hour * 60 + now.minute
        for start, end, label in RELEASE_WINDOWS:
            if start <= minute_of_day <= end:
                return label
        return None

    @staticmethod
    def _period_label(observation_date: date, periods_per_year: int) -> str:
        if periods_per_year == 12:
            return f"{observation_date:%B %Y}"
        if periods_per_year == 4:
            return f"Q{(observation_date.month - 1) // 3 + 1} {observation_date.year}"
        return observation_date.isoformat()

    @classmethod
    def _alert_embed(
        cls,
        series_id: str,
        observation: dict[str, Any],
        window: str | None,
    ) -> discord.Embed:
        meta = MONITORED_SERIES[series_id]
        value = observation["value"]
        previous = observation["previous"]
        year_ago = observation["year_ago"]

        embed = discord.Embed(
            title=f"\U0001f4ca New data: {meta['label']}",
            color=discord.Color(0xF1C40F),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="Release", value=meta["category"], inline=False)
        embed.add_field(
            name="Period",
            value=cls._period_label(observation["date"], meta["periods_per_year"]),
            inline=True,
        )
        embed.add_field(name="New value", value=format_number(value, 2), inline=True)
        embed.add_field(
            name="Previous value", value=format_number(previous, 2), inline=True
        )

        if previous:
            change = value - previous
            percent = change / previous * 100
            embed.add_field(
                name="Change vs prior period",
                value=f"{format_signed(change, 2)} ({format_signed(percent, 2, '%')})",
                inline=False,
            )

        if year_ago:
            year_percent = (value - year_ago) / year_ago * 100
            embed.add_field(
                name="Change vs year ago",
                value=format_signed(year_percent, 2, "%"),
                inline=False,
            )

        embed.set_footer(
            text=(
                f"Source: FRED · {window}"
                if window
                else "Source: FRED · outside a standard release window"
            )
        )
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MacroAlerts(bot))
