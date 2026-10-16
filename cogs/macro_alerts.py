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
    format_signed,
    load_json,
    parse_iso_date,
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
        "label": "US Consumer Price Index (CPI)",
        "periods_per_year": 12,
        "emoji": "\U0001f4ca",
        "title": "US Consumer Price Index (CPI)",
        "agency": "Bureau of Labor Statistics",
        "color": 0xF1C40F,
        # Only used when FRED's own units string cannot be read.
        "fallback_units": "Index 1982-1984=100",
        "release_url": "https://www.bls.gov/news.release/cpi.nr0.htm",
        "release_link": "Read Full BLS Release",
        # BLS publishes headline and core in the same release, so one alert
        # carries both rather than two firing at the same moment. MoM comes from
        # the seasonally adjusted series and the 12-month change from the not
        # seasonally adjusted one, which is how BLS itself reports them.
        "components": (
            {
                "label": "Headline CPI",
                "short": "Headline",
                "sa_id": "CPIAUCSL",
                "nsa_id": "CPIAUCNS",
            },
            {
                "label": "Core CPI",
                "short": "Core",
                "sa_id": "CPILFESL",
                "nsa_id": "CPILFENS",
            },
        ),
    },
    "PCEPILFE": {
        "label": "Core PCE Price Index",
        "periods_per_year": 12,
        "emoji": "\U0001f4ca",
        "title": "US Core PCE Price Index (Fed's Preferred Gauge)",
        "agency": "BEA",
        "color": 0x9B59B6,
        "fallback_units": "Index 2017=100",
        "components": (
            {
                "label": "Core PCE",
                "short": "Core PCE",
                "sa_id": "PCEPILFE",
                "nsa_id": None,
            },
        ),
    },
    "GDPC1": {
        "label": "Real Gross Domestic Product",
        "periods_per_year": 4,
        "emoji": "\U0001f4c8",
        "title": "US Real Gross Domestic Product (GDP)",
        "agency": "BEA",
        "color": 0x3498DB,
        "fallback_units": "Billions of Chained 2017 Dollars",
        "components": (
            {
                "label": "Real GDP",
                "short": "Real GDP",
                "sa_id": "GDPC1",
                "nsa_id": None,
            },
        ),
    },
}


def series_lookback_days(periods_per_year: int) -> int:
    """Enough history to reach a year back with room to spare."""
    return int(365 * (12 / periods_per_year)) + 180


def shift_months(stamp, months: int) -> tuple[int, int]:
    """The (year, month) that is `months` before `stamp`."""
    total = stamp.year * 12 + (stamp.month - 1) - months
    return total // 12, total % 12 + 1


def value_months_back(clean, months: int) -> float | None:
    """The observation exactly `months` earlier, matched on the calendar.

    Positional indexing — `iloc[-1 - months]` — silently breaks when the series
    has a hole. CPIAUCSL is missing October 2025, so its 13th-from-last
    observation is August 2025: **13** months back rather than 12, which
    overstated year-over-year inflation by about a third of a point. Matching on
    the date the value is supposed to represent is immune to that.
    """
    if clean is None or len(clean) == 0:
        return None
    target = shift_months(clean.index[-1], months)
    for stamp, value in reversed(list(clean.items())):
        position = (stamp.year, stamp.month)
        if position == target:
            return float(value)
        if position < target:
            # Walked past it, so the month is genuinely absent.
            return None
    return None


def rate_metrics(clean, periods_per_year: int) -> dict[str, Any] | None:
    """Percentage rates of change for an index series, newest observation last.

    Markets trade these, not the index level: MoM and YoY for the inflation
    series, plus BEA's annualised quarter-on-quarter rate for GDP.

    Returns None rather than a partial figure when a needed month is absent — an
    alert someone trades off should not carry a number that is only half
    derivable.
    """
    step = 12 // periods_per_year
    if clean is None or len(clean) < 2:
        return None

    latest = float(clean.iloc[-1])
    previous = value_months_back(clean, step)
    prev_previous = value_months_back(clean, step * 2)
    year_ago = value_months_back(clean, 12)
    prior_year_ago = value_months_back(clean, 12 + step)

    if None in (previous, prev_previous, year_ago, prior_year_ago):
        return None
    # Ratios against a zero or negative level are meaningless.
    if min(latest, previous, prev_previous, year_ago, prior_year_ago) <= 0:
        return None

    metrics: dict[str, Any] = {
        "date": clean.index[-1].date(),
        "value": latest,
        "previous_value": previous,
        "mom": (latest / previous - 1) * 100,
        "prior_mom": (previous / prev_previous - 1) * 100,
        "yoy": (latest / year_ago - 1) * 100,
        "prior_yoy": (previous / prior_year_ago - 1) * 100,
        "annualized": None,
        "prior_annualized": None,
    }

    if periods_per_year == 4:
        # BEA's standard annualisation of a quarterly change.
        metrics["annualized"] = ((latest / previous) ** 4 - 1) * 100
        metrics["prior_annualized"] = ((previous / prev_previous) ** 4 - 1) * 100

    return metrics


# Direction markers. U+FE0E forces text presentation, which keeps every marker a
# single column wide inside a monospace code block — without it the pause glyph
# renders as a double-width emoji and the table's alignment drifts.
TEXT_PRESENTATION = "︎"
STATUS_ACCELERATED = f"▲{TEXT_PRESENTATION} Accelerated"
STATUS_UPTICK = f"▲{TEXT_PRESENTATION} Slight uptick"
STATUS_INLINE = f"⏸{TEXT_PRESENTATION} In line with prior"
STATUS_COOLDOWN = f"▼{TEXT_PRESENTATION} Slight cooldown"
STATUS_COOLED = f"▼{TEXT_PRESENTATION} Cooled"


# The table has to fit a phone without scrolling sideways, so each status also
# carries a short form. The long wording stays the default, and is what any
# non-tabular rendering would use.
STATUS_SHORT = {
    STATUS_ACCELERATED: f"▲{TEXT_PRESENTATION} Accel",
    STATUS_UPTICK: f"▲{TEXT_PRESENTATION} Uptick",
    STATUS_INLINE: f"⏸{TEXT_PRESENTATION} Flat",
    STATUS_COOLDOWN: f"▼{TEXT_PRESENTATION} Cooling",
    STATUS_COOLED: f"▼{TEXT_PRESENTATION} Cooled",
}


def status_for(actual: float, prior: float, *, short: bool = False) -> str:
    """Plain-language direction, from the rounded one-decimal rates.

    Compared in integer tenths. `0.6 - 0.4` is `0.19999999999999998` in binary
    floating point, so a direct `diff >= 0.2` test would fail on precisely the
    boundary this rule is written around — headline MoM at +0.6% against +0.4%
    prior would be labelled a slight uptick instead of an acceleration.
    """
    diff = round(actual * 10) - round(prior * 10)
    if diff >= 2:
        label = STATUS_ACCELERATED
    elif diff == 1:
        label = STATUS_UPTICK
    elif diff == 0:
        label = STATUS_INLINE
    elif diff == -1:
        label = STATUS_COOLDOWN
    else:
        label = STATUS_COOLED
    return STATUS_SHORT[label] if short else label


def base_period(units: str, fallback: str) -> str:
    """'Index 1982-1984=100' -> 'Base 1982-1984=100'."""
    text = (units or "").strip() or fallback
    return f"Base {text[6:]}" if text.lower().startswith("index ") else text


# Column widths for the glance table, chosen so the widest row stays inside the
# roughly 35 monospace columns a phone shows before a code block scrolls
# sideways. A flat four-row table with the full status wording ran to 62.
TABLE_METRIC_WIDTH = 10
TABLE_VALUE_WIDTH = 7
TABLE_TREND_GAP = 3


def glance_table(components: list[dict[str, Any]]) -> str:
    """The 'Numbers at a Glance' monospace table.

    Grouped by measure rather than flattened, so headline and core stay visually
    separate and "CPI" is not repeated on every row. Each group shows the
    12-month change above the one-month change, matching the order BLS uses in
    its own summary, and rates carry the single decimal the release quotes.
    """
    lines = [
        f"{'Metric':<{TABLE_METRIC_WIDTH}}"
        f"{'Actual':>{TABLE_VALUE_WIDTH}}"
        f"{'Prior':>{TABLE_VALUE_WIDTH}}"
        f"{' ' * TABLE_TREND_GAP}Trend"
    ]
    for component in components:
        lines.append(component["short"])
        for metric, suffix in (("yoy", "YoY"), ("mom", "MoM")):
            actual = component[metric]
            prior = component["prior_" + metric]
            # Month-on-month is conventionally signed; the 12-month rate is not.
            shown = f"{actual:+.1f}%" if metric == "mom" else f"{actual:.1f}%"
            shown_prior = f"{prior:+.1f}%" if metric == "mom" else f"{prior:.1f}%"
            lines.append(
                f"{'  ' + suffix:<{TABLE_METRIC_WIDTH}}"
                f"{shown:>{TABLE_VALUE_WIDTH}}"
                f"{shown_prior:>{TABLE_VALUE_WIDTH}}"
                f"{' ' * TABLE_TREND_GAP}"
                f"{status_for(actual, prior, short=True)}"
            )
    return "```\n" + "\n".join(lines) + "\n```"


def as_mapping(info: Any) -> dict[str, Any]:
    """Normalise FRED series metadata, which arrives as a pandas Series.

    `fredapi` hands back a Series rather than a dict, and a Series has no truth
    value — `info or {}` raises on it. Everything downstream takes a plain dict.
    """
    if info is None:
        return {}
    if isinstance(info, dict):
        return info
    try:
        return dict(info)
    except (TypeError, ValueError):
        return {}


def release_date(info: Any) -> date | None:
    """When FRED last published the series, which is its release date.

    `last_updated` looks like '2026-10-14 08:08:29-05'; only the date matters.
    """
    raw = str(as_mapping(info).get("last_updated") or "")
    return parse_iso_date(raw[:10]) if len(raw) >= 10 else None

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
            metrics = await retry_async(
                self._fetch_metrics,
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

        if metrics is None:
            await interaction.followup.send(
                f"FRED has too little history for `{series_id}` to state a rate.",
                ephemeral=True,
            )
            return

        info = await self._fetch_series_info(series_id)
        try:
            await channel.send(
                embed=self._alert_embed(series_id, metrics, info, is_test=True)
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
                metrics = await retry_async(
                    self._fetch_metrics,
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

            if metrics is None:
                continue

            seen = self._state.get(series_id)
            if seen is None:
                # First sighting of this series: record where it stands without
                # announcing, so switching the bot on does not replay months of
                # old releases into the channel.
                self._state[series_id] = metrics["date"].isoformat()
                seeded.append(series_id)
                continue

            # ISO dates compare correctly as plain strings.
            if metrics["date"].isoformat() > seen:
                pending.append((series_id, metrics))

        posted = False
        for series_id, metrics in pending:
            # Metadata is fetched here rather than during the poll, so the
            # 15-minute cycle does not pay for a lookup it will not use.
            info = await self._fetch_series_info(series_id)
            try:
                await channel.send(embed=self._alert_embed(series_id, metrics, info))
            except discord.HTTPException:
                # Cache stays untouched, so the next poll retries this release.
                log.exception("Could not post the %s alert; it will be retried", series_id)
                continue
            self._state[series_id] = metrics["date"].isoformat()
            posted = True
            log.info(
                "Posted macro alert: %s (%s)%s",
                series_id,
                metrics["date"],
                f" — {window}" if window else "",
            )

        if seeded or posted:
            _save_state(self._state)
        if seeded:
            log.info("Seeded the macro cache (no alerts sent) for: %s", ", ".join(seeded))

    async def _read_series(self, series_id: str, start: str):
        """One FRED series as a clean pandas Series, or None. Blocking."""
        frame = await run_blocking(
            self._fred.get_series, series_id, observation_start=start
        )
        # FRED pads a series with NaN for periods it has not published yet.
        clean = frame.dropna() if frame is not None else None
        if clean is None or clean.empty:
            log.warning("FRED series %s returned no usable observations", series_id)
            return None
        return clean

    async def _fetch_metrics(self, series_id: str) -> dict[str, Any] | None:
        """Rates of change for every component of a monitored release.

        CPI carries two components, because BLS publishes headline and core
        together. Everything else carries one.
        """
        meta = MONITORED_SERIES[series_id]
        periods_per_year = meta["periods_per_year"]
        start = (
            datetime.now(timezone.utc).date()
            - timedelta(days=series_lookback_days(periods_per_year))
        ).isoformat()

        components: list[dict[str, Any]] = []
        for spec in meta["components"]:
            mom_series = await self._read_series(spec["sa_id"], start)
            # Where there is a separate NSA series, the 12-month change comes
            # from it, as BLS reports it. Otherwise the SA series serves both.
            yoy_series = (
                await self._read_series(spec["nsa_id"], start)
                if spec.get("nsa_id")
                else mom_series
            )
            if mom_series is None or yoy_series is None:
                return None

            mom_rates = rate_metrics(mom_series, periods_per_year)
            yoy_rates = rate_metrics(yoy_series, periods_per_year)
            if mom_rates is None or yoy_rates is None:
                log.warning(
                    "Not enough clean history to state a rate for %s", spec["sa_id"]
                )
                return None

            components.append(
                {
                    "label": spec["label"],
                    "short": spec.get("short") or spec["label"],
                    "date": mom_rates["date"],
                    "value": mom_rates["value"],
                    "mom": mom_rates["mom"],
                    "prior_mom": mom_rates["prior_mom"],
                    "yoy": yoy_rates["yoy"],
                    "prior_yoy": yoy_rates["prior_yoy"],
                    "annualized": mom_rates["annualized"],
                    "prior_annualized": mom_rates["prior_annualized"],
                }
            )

        return {"date": components[0]["date"], "components": components}

    async def _fetch_series_info(self, series_id: str) -> dict[str, Any] | None:
        """FRED metadata: the authoritative units string, and the release date.

        Only called when an alert is actually going out, so the 15-minute poll
        does not pay for it.
        """
        try:
            raw = await run_blocking(self._fred.get_series_info, series_id)
        except Exception:
            # Cosmetic only — the embed falls back to its configured units.
            log.debug("Could not read FRED metadata for %s", series_id, exc_info=True)
            return None
        return as_mapping(raw)

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
        metrics: dict[str, Any],
        info: dict[str, Any] | None = None,
        *,
        is_test: bool = False,
    ) -> discord.Embed:
        """Build the alert.

        The fields lead with rates of change — what the market actually trades
        on — and keep the raw index level as context. A CPI alert reading
        "336.30" tells a reader nothing; "4.02% year over year, up from 3.71%"
        tells them what moved.
        """
        meta = MONITORED_SERIES[series_id]
        info = as_mapping(info)
        components = metrics["components"]
        period = cls._period_label(metrics["date"], meta["periods_per_year"])
        units = str(info.get("units") or "").strip() or meta["fallback_units"]

        embed = discord.Embed(
            title=(
                f"{'[TEST] ' if is_test else ''}{meta['emoji']} "
                f"{meta['title']} — {period}"
            ),
            # Grey marks a test post; real alerts take the series colour.
            color=discord.Color(0x95A5A6) if is_test else discord.Color(meta["color"]),
            timestamp=datetime.now(timezone.utc),
        )
        if meta.get("release_url"):
            # Turns the title into a link to the official release.
            embed.url = meta["release_url"]

        if len(components) > 1:
            cls._paired_fields(embed, components, meta, units)
        else:
            cls._single_fields(embed, components[0], meta, units)

        footer = f"Source: {meta['agency']} via FRED"
        released = release_date(info)
        if released:
            footer += f" • Released {released:%b} {released.day}, {released.year}"
        if is_test:
            footer += " · TEST POST, release cache unchanged"
        embed.set_footer(text=footer)
        return embed

    @staticmethod
    def _paired_fields(embed, components, meta, units) -> None:
        """Headline and core together, as a monospace table.

        A table rather than inline fields: four metrics as fields would wrap
        three-to-a-row in Discord and strand the fourth on a line of its own.
        """
        embed.add_field(
            name="The Numbers at a Glance",
            value=glance_table(components),
            inline=False,
        )
        levels = " | ".join(
            f"{component['short']} {component['value']:.2f}" for component in components
        )
        lines = [f"{levels} ({base_period(units, meta['fallback_units'])})"]
        if meta.get("release_url"):
            lines.append(
                f"[{meta.get('release_link', 'Read the full release')}]"
                f"({meta['release_url']})"
            )
        embed.add_field(
            name="Index Levels & Official Release",
            value="\n".join(lines),
            inline=False,
        )

    @staticmethod
    def _single_fields(embed, component, meta, units) -> None:
        """The one-component layout, used by Core PCE and Real GDP."""
        if component["annualized"] is not None:
            embed.add_field(
                name="Annualized QoQ (SAAR)",
                value=(
                    f"**{format_signed(component['annualized'], 1, '%')}**\n"
                    f"vs {format_signed(component['prior_annualized'], 1, '%')} "
                    "prior quarter"
                ),
                inline=True,
            )
            embed.add_field(
                name="YoY Growth",
                value=(
                    f"**{format_signed(component['yoy'], 2, '%')}**\n"
                    f"vs {format_signed(component['prior_yoy'], 2, '%')} prior year"
                ),
                inline=True,
            )
            embed.add_field(
                name="Real Output",
                value=f"${component['value']:,.1f}B\n{units}",
                inline=True,
            )
            return

        embed.add_field(
            name="MoM Rate",
            value=(
                f"**{format_signed(component['mom'], 2, '%')}**\n"
                f"vs {format_signed(component['prior_mom'], 2, '%')} prior month"
            ),
            inline=True,
        )
        embed.add_field(
            name="YoY Rate",
            value=(
                f"**{format_signed(component['yoy'], 2, '%')}**\n"
                f"vs {format_signed(component['prior_yoy'], 2, '%')} prior year"
            ),
            inline=True,
        )
        embed.add_field(
            name="Index Level",
            value=f"{component['value']:,.2f}\n{units}",
            inline=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MacroAlerts(bot))
