"""Watches FRED for freshly published macro data and posts an alert.

FRED has no push API, so this polls on an interval and compares the newest
observation date against a cache on disk. The cache is what makes a restart
safe: the bot only ever announces a period it has not announced before.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from fredapi import Fred

import config
from utils import (
    format_number,
    format_signed,
    load_json,
    resolve_channel,
    retry_async,
    run_blocking,
    save_json,
    to_pacific_clock,
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

# Release windows, as (start hour, start minute, end hour, end minute, name) on
# the US/Eastern clock — the clock the release schedule runs on. These label an
# alert with the window it landed in; the on-disk cache, not the window, is what
# prevents duplicate posts.
#
# The label shown to readers is derived in Pacific rather than typed in, so the
# two can never drift apart.
#
# Only the 08:30 window can currently produce an alert, because none of the
# three monitored series is a rate decision. To make the FOMC window functional,
# add DFEDTARU (fed funds target range, upper limit) to MONITORED_SERIES.
RELEASE_WINDOWS = (
    (8, 25, 9, 30, "CPI / PCE / GDP"),
    (13, 55, 14, 45, "FOMC"),
)


def window_bounds(window: tuple[int, int, int, int, str]) -> tuple[int, int]:
    """The window as (start, end) minutes past midnight US/Eastern."""
    start_hour, start_minute, end_hour, end_minute, _ = window
    return start_hour * 60 + start_minute, end_hour * 60 + end_minute


def release_window_label(window: tuple[int, int, int, int, str]) -> str:
    """Human-readable window, e.g. '05:25-06:30 PT window (CPI / PCE / GDP)'."""
    start_hour, start_minute, end_hour, end_minute, name = window
    start = to_pacific_clock(start_hour, start_minute)
    end = to_pacific_clock(end_hour, end_minute)
    return f"{start}-{end} PT window ({name})"

STATE_PATH = config.PROJECT_ROOT / "state" / "macro_state.json"


def _load_state() -> dict[str, str]:
    """Read the seen-observation-date cache, tolerating a missing or corrupt file."""
    data = load_json(STATE_PATH, {})
    if not isinstance(data, dict):
        log.warning(
            "%s did not contain an object; starting from an empty cache", STATE_PATH
        )
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _save_state(state: dict[str, str]) -> None:
    save_json(STATE_PATH, state)


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

    # ----------------------------------------------------------------- commands

    @app_commands.command(
        name="test_macro_alert",
        description="Post a sample release alert for one series, without waiting for a real print.",
    )
    @app_commands.describe(series="Which series to post a sample alert for")
    @app_commands.choices(
        series=[
            app_commands.Choice(name="CPI — CPIAUCSL", value="CPIAUCSL"),
            app_commands.Choice(name="Core PCE — PCEPILFE", value="PCEPILFE"),
            app_commands.Choice(name="Real GDP — GDPC1", value="GDPC1"),
        ]
    )
    async def test_macro_alert(
        self, interaction: discord.Interaction, series: app_commands.Choice[str]
    ) -> None:
        if self._fred is None:
            await interaction.response.send_message(
                "`FRED_API_KEY` isn't set, so there's nothing to fetch. Add it to "
                "`.env` and restart the bot.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        series_id = series.value

        channel = await resolve_channel(self.bot, config.TARGET_CHANNEL_ID)
        if channel is None:
            await interaction.followup.send(
                f"Couldn't resolve channel `{config.TARGET_CHANNEL_ID}`.", ephemeral=True
            )
            return

        try:
            observation = await retry_async(
                self._fetch_latest,
                series_id,
                attempts=3,
                base_delay=3.0,
                label=f"FRED:{series_id}",
            )
        except Exception:
            log.exception("Test alert could not read %s", series_id)
            await interaction.followup.send(
                f"Couldn't read `{series_id}` from FRED. Check the bot logs.",
                ephemeral=True,
            )
            return

        if observation is None:
            await interaction.followup.send(
                f"FRED returned no usable observations for `{series_id}`.", ephemeral=True
            )
            return

        try:
            await channel.send(
                embed=self._alert_embed(
                    series_id, observation, self._active_window(), is_test=True
                )
            )
        except discord.HTTPException:
            log.exception("Test alert could not be posted to the channel")
            await interaction.followup.send(
                "Discord rejected the message. Check the bot has **Send Messages** "
                "and **Embed Links** in that channel.",
                ephemeral=True,
            )
            return

        # self._state is deliberately left alone here. Recording the period as
        # seen would suppress the real alert when that release actually lands.
        await interaction.followup.send(
            f"Posted a test alert for `{series_id}` to <#{config.TARGET_CHANNEL_ID}>. "
            "The release cache was left untouched, so the real alert will still fire.",
            ephemeral=True,
        )

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
        for window in RELEASE_WINDOWS:
            start, end = window_bounds(window)
            if start <= minute_of_day <= end:
                return release_window_label(window)
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
        *,
        is_test: bool = False,
    ) -> discord.Embed:
        meta = MONITORED_SERIES[series_id]
        value = observation["value"]
        previous = observation["previous"]
        year_ago = observation["year_ago"]

        embed = discord.Embed(
            title=f"{'[TEST] ' if is_test else ''}\U0001f4ca New data: {meta['label']}",
            # Grey marks a test post; real alerts stay amber.
            color=discord.Color(0x95A5A6) if is_test else discord.Color(0xF1C40F),
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

        footer = (
            f"Source: FRED · {window}"
            if window
            else "Source: FRED · outside a standard release window"
        )
        if is_test:
            footer += " · TEST POST, release cache unchanged"
        embed.set_footer(text=footer)
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MacroAlerts(bot))
