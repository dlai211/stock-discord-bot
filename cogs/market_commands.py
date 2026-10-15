"""On-demand slash commands: /stock and /macro."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
import finnhub
import yfinance as yf
from discord import app_commands
from discord.ext import commands
from fredapi import Fred

import config
from charting import (
    CHART_FILENAME,
    DEFAULT_TIMEFRAME,
    TIMEFRAMES,
    ChartUnavailable,
    generate_stock_chart,
    timeframe_args,
)
from utils import (
    format_compact,
    format_money,
    format_number,
    format_signed,
    parse_iso_date,
    retry_async,
    run_blocking,
    timing_label,
)

log = logging.getLogger(__name__)

COLOR_UP = discord.Color(0x2ECC71)
COLOR_DOWN = discord.Color(0xE74C3C)
COLOR_NEUTRAL = discord.Color(0x5865F2)
COLOR_ERROR = discord.Color(0xED4245)

# How long the timeframe buttons stay live after a chart is posted.
CHART_VIEW_TIMEOUT = 180

FRED_SERIES = {
    "FEDFUNDS": "Effective Federal Funds Rate",
    "DGS10": "10-Year Treasury Constant Maturity",
    "DGS2": "2-Year Treasury Constant Maturity",
}

# Two years is plenty of history for a "latest reading" view and keeps the
# FRED payload small.
FRED_LOOKBACK_DAYS = 730

# How far ahead to look for a company's next scheduled report. Companies report
# quarterly, so this comfortably covers the next one.
EARNINGS_LOOKAHEAD_DAYS = 200

# Reported quarters to pull: the newest is shown in detail, the rest as a
# one-line history.
REPORTED_QUARTERS = 4

# CPI is the headline inflation series. The year-over-year rate compares the
# newest monthly observation against the one 12 months earlier, so the window
# spans 13 observations.
CPI_SERIES_ID = "CPIAUCSL"
CPI_YOY_PERIODS = 12


class LookupFailed(Exception):
    """The provider answered, but has nothing for the requested symbol."""


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first value in `mapping` that isn't None.

    Yahoo renames its quote keys between API revisions, so every field is read
    through a list of candidate names.
    """
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return None


class TimeframeButton(discord.ui.Button):
    """One timeframe button; the style marks which one is showing."""

    def __init__(self, label: str, period: str, interval: str, active: bool) -> None:
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.primary
                if active
                else discord.ButtonStyle.secondary
            ),
        )
        self.period = period
        self.interval = interval

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, StockChartView):
            await view.show_timeframe(
                interaction, self.label or "", self.period, self.interval
            )


class StockChartView(discord.ui.View):
    """The 1D / 1W / 1M / 1Y buttons underneath a chart."""

    def __init__(
        self,
        symbol: str,
        base_embed: discord.Embed,
        active: str = DEFAULT_TIMEFRAME,
    ) -> None:
        super().__init__(timeout=CHART_VIEW_TIMEOUT)
        self.symbol = symbol
        # Kept without an image so each redraw starts from a clean copy.
        self.base_embed = base_embed
        self.active = active
        self.sync_buttons()

    def sync_buttons(self) -> None:
        self.clear_items()
        for label, period, interval in TIMEFRAMES:
            self.add_item(
                TimeframeButton(label, period, interval, active=label == self.active)
            )

    def chart_embed(self) -> discord.Embed:
        embed = self.base_embed.copy()
        embed.set_image(url=f"attachment://{CHART_FILENAME}")
        return embed

    async def show_timeframe(
        self,
        interaction: discord.Interaction,
        label: str,
        period: str,
        interval: str,
    ) -> None:
        # Deferred before anything slow: Discord only gives a component
        # interaction three seconds to be acknowledged, and a chart render plus
        # a Yahoo round trip will often exceed that.
        await interaction.response.defer()

        try:
            buffer = await run_blocking(
                generate_stock_chart, self.symbol, period, interval
            )
        except ChartUnavailable as error:
            await interaction.followup.send(
                f"No {label} chart for `{self.symbol}` — {error}", ephemeral=True
            )
            return
        except Exception:
            log.exception("Chart render failed for %s (%s)", self.symbol, period)
            await interaction.followup.send(
                "Couldn't build that chart. Check the bot logs.", ephemeral=True
            )
            return

        self.active = label
        self.sync_buttons()

        await interaction.edit_original_response(
            embed=self.chart_embed(),
            attachments=[discord.File(buffer, filename=CHART_FILENAME)],
            view=self,
        )

    async def on_timeout(self) -> None:
        # Discord leaves expired buttons looking clickable, so grey them out.
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            log.debug("Could not grey out expired chart buttons", exc_info=True)


class MarketCommands(commands.Cog):
    """Live market and macro lookups."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Neither constructor makes a network call, so this is safe at load time.
        self._fred = Fred(api_key=config.FRED_API_KEY) if config.FRED_API_KEY else None
        self._finnhub = (
            finnhub.Client(api_key=config.FINNHUB_KEY) if config.FINNHUB_KEY else None
        )

    # ------------------------------------------------------------- /stock

    @app_commands.command(name="stock", description="Latest quote for a stock ticker.")
    @app_commands.describe(ticker="Ticker symbol, e.g. AAPL or MSFT")
    async def stock(self, interaction: discord.Interaction, ticker: str) -> None:
        symbol = ticker.strip().upper()
        if not symbol or len(symbol) > 12 or any(character.isspace() for character in symbol):
            await interaction.response.send_message(
                f"`{ticker.strip()}` doesn't look like a ticker symbol.", ephemeral=True
            )
            return

        # Yahoo takes a second or two to answer, and Discord kills an
        # interaction that hasn't been acknowledged within 3 seconds.
        await interaction.response.defer(thinking=True)

        try:
            info = await retry_async(
                self._fetch_quote,
                symbol,
                attempts=2,
                base_delay=1.5,
                label=f"yfinance:{symbol}",
                give_up_on=(LookupFailed,),
            )
        except LookupFailed:
            await interaction.followup.send(
                embed=self._error_embed(
                    f"Unknown ticker: {symbol}",
                    f"`{symbol}` isn't a valid or currently listed ticker. Check the "
                    "spelling — for example `AAPL` or `MSFT`.",
                )
            )
            return
        except Exception:
            log.exception("Market data lookup failed for %s", symbol)
            await interaction.followup.send(
                embed=self._error_embed(
                    "Market data unavailable",
                    f"Couldn't reach the market data provider for `{symbol}`. "
                    "Try again in a moment.",
                )
            )
            return

        base_embed = self._quote_embed(symbol, info)

        try:
            buffer = await run_blocking(
                generate_stock_chart, symbol, *timeframe_args(DEFAULT_TIMEFRAME)
            )
        except Exception:
            # The chart is an extra; the quote is the point. A rendering or
            # Yahoo failure must not cost the whole command.
            log.exception("Chart render failed for %s; posting the quote alone", symbol)
            await interaction.followup.send(embed=base_embed)
            return

        view = StockChartView(symbol, base_embed, DEFAULT_TIMEFRAME)
        await interaction.followup.send(
            embed=view.chart_embed(),
            file=discord.File(buffer, filename=CHART_FILENAME),
            view=view,
        )

    @staticmethod
    async def _fetch_quote(symbol: str) -> dict[str, Any]:
        """Fetch a quote dict, or raise LookupFailed for an unknown symbol."""
        try:
            info = await run_blocking(lambda: yf.Ticker(symbol).info)
        except Exception as error:
            # Some yfinance versions raise for a bad symbol instead of returning
            # an empty stub. Translate that into the friendly path; anything
            # else (rate limits, network) should keep its retry.
            message = str(error).lower()
            if "delisted" in message or "no data found" in message:
                raise LookupFailed(symbol) from error
            raise

        if not isinstance(info, dict) or not info:
            raise LookupFailed(symbol)
        # Yahoo returns a near-empty stub rather than an error for delisted or
        # mistyped symbols, so a missing price is what actually signals "no
        # such ticker".
        if _first(info, "currentPrice", "regularMarketPrice", "previousClose") is None:
            raise LookupFailed(symbol)
        return info

    def _quote_embed(self, symbol: str, info: dict[str, Any]) -> discord.Embed:
        currency = (info.get("currency") or "USD").upper()
        name = info.get("longName") or info.get("shortName") or symbol
        exchange = info.get("fullExchangeName") or info.get("exchange")

        price = _first(info, "currentPrice", "regularMarketPrice", "previousClose")
        open_price = _first(info, "open", "regularMarketOpen")
        previous_close = _first(info, "previousClose", "regularMarketPreviousClose")
        day_high = _first(info, "dayHigh", "regularMarketDayHigh")
        day_low = _first(info, "dayLow", "regularMarketDayLow")
        volume = _first(info, "volume", "regularMarketVolume")
        market_cap = _first(info, "marketCap")

        color = COLOR_NEUTRAL
        change_text = "N/A"
        if price is not None and previous_close:
            change = price - previous_close
            percent = change / previous_close * 100
            arrow = "▲" if change > 0 else ("▼" if change < 0 else "▬")
            change_text = f"{arrow} **{change:+,.2f}** ({percent:+.2f}%)"
            if change > 0:
                color = COLOR_UP
            elif change < 0:
                color = COLOR_DOWN

        day_range = "N/A"
        if day_high is not None and day_low is not None:
            day_range = f"{format_money(day_low, currency)} – {format_money(day_high, currency)}"

        embed = discord.Embed(
            title=f"{name} ({symbol})",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        if exchange:
            embed.description = f"*{exchange}*"

        embed.add_field(name="Price", value=f"**{format_money(price, currency)}**", inline=True)
        embed.add_field(name="Open", value=format_money(open_price, currency), inline=True)
        embed.add_field(name="Daily change", value=change_text, inline=True)
        embed.add_field(name="Previous close", value=format_money(previous_close, currency), inline=True)
        embed.add_field(name="Day range", value=day_range, inline=True)
        embed.add_field(name="Volume", value=format_compact(volume), inline=True)
        embed.add_field(name="Market cap", value=format_compact(market_cap), inline=True)
        embed.set_footer(text="Yahoo Finance via yfinance · quotes may be delayed")
        return embed

    # ------------------------------------------------------------- /macro

    @app_commands.command(
        name="macro",
        description="Key US rates from FRED, plus the 10Y-2Y Treasury spread.",
    )
    async def macro(self, interaction: discord.Interaction) -> None:
        if self._fred is None:
            await interaction.response.send_message(
                "`FRED_API_KEY` isn't set, so macro data is unavailable. "
                "Add it to `.env` and restart the bot.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            observations, cpi = await retry_async(
                self._fetch_fred_series, attempts=3, base_delay=2.0, label="FRED"
            )
        except Exception:
            log.exception("FRED lookup failed")
            await interaction.followup.send(
                embed=self._error_embed(
                    "FRED unavailable",
                    "Couldn't reach the St. Louis Fed data service. Try again in a moment.",
                )
            )
            return

        await interaction.followup.send(embed=self._macro_embed(observations, cpi))

    async def _fetch_fred_series(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
        """Latest readings for the headline rates, plus CPI year-over-year."""
        start = (
            datetime.now(timezone.utc).date() - timedelta(days=FRED_LOOKBACK_DAYS)
        ).isoformat()

        observations: dict[str, dict[str, Any]] = {}
        cpi: dict[str, Any] | None = None

        for series_id in (*FRED_SERIES, CPI_SERIES_ID):
            frame = await run_blocking(
                self._fred.get_series, series_id, observation_start=start
            )
            # DGS10/DGS2 are business-day series and carry NaN on holidays, and
            # FRED pads monthly series with NaN for periods it has not published.
            clean = frame.dropna() if frame is not None else None
            if clean is None or clean.empty:
                log.warning("FRED series %s returned no usable observations", series_id)
                continue

            if series_id == CPI_SERIES_ID:
                cpi = self._cpi_year_over_year(clean)
                continue

            observations[series_id] = {
                "value": float(clean.iloc[-1]),
                "date": clean.index[-1].date(),
                "previous": float(clean.iloc[-2]) if len(clean) > 1 else None,
            }

        return observations, cpi

    @staticmethod
    def _cpi_year_over_year(series: Any) -> dict[str, Any] | None:
        """Year-over-year inflation across the latest 13 monthly observations.

        Returns None when FRED has not published a full 13-month window, which
        is the case early in a series' life or after a long outage.
        """
        window = series.tail(CPI_YOY_PERIODS + 1)
        if len(window) < CPI_YOY_PERIODS + 1:
            return None

        latest = float(window.iloc[-1])
        year_ago = float(window.iloc[0])
        if not year_ago:
            return None

        return {
            "latest": latest,
            "latest_date": window.index[-1].date(),
            "year_ago": year_ago,
            "year_ago_date": window.index[0].date(),
            "yoy_percent": (latest - year_ago) / year_ago * 100,
            "observations": int(len(window)),
        }

    @staticmethod
    def _macro_embed(
        observations: dict[str, dict[str, Any]],
        cpi: dict[str, Any] | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title="US Rates & Macro Snapshot",
            color=COLOR_NEUTRAL,
            timestamp=datetime.now(timezone.utc),
        )

        if not observations and cpi is None:
            embed.description = "FRED returned no observations for any tracked series."
            return embed

        for series_id, label in FRED_SERIES.items():
            entry = observations.get(series_id)
            if entry is None:
                embed.add_field(name=label, value="Unavailable", inline=False)
                continue
            embed.add_field(
                name=label,
                value=f"**{format_number(entry['value'], 2)}%** · {entry['date']:%b %d, %Y}",
                inline=False,
            )

        ten_year = observations.get("DGS10")
        two_year = observations.get("DGS2")
        if ten_year and two_year:
            spread_points = ten_year["value"] - two_year["value"]
            spread_basis_points = spread_points * 100
            suffix = " ⚠️ **inverted**" if spread_points < 0 else ""
            embed.add_field(
                name="10Y – 2Y spread",
                value=(
                    f"**{format_signed(spread_basis_points, 1, ' bps')}** "
                    f"({format_signed(spread_points, 2, ' pts')}){suffix}"
                ),
                inline=False,
            )
            if ten_year["date"] != two_year["date"]:
                embed.add_field(
                    name="Note",
                    value=(
                        "The two Treasury series have different latest observation "
                        "dates, so the spread compares readings from different days."
                    ),
                    inline=False,
                )

        if cpi is not None:
            embed.add_field(
                name="CPI Inflation (YoY)",
                value=(
                    f"**{format_signed(cpi['yoy_percent'], 2, '%')}** over 12 months\n"
                    f"{cpi['latest_date']:%B %Y} at {cpi['latest']:,.2f} vs "
                    f"{cpi['year_ago_date']:%B %Y} at {cpi['year_ago']:,.2f}\n"
                    f"*latest {cpi['observations']} observations of CPIAUCSL*"
                ),
                inline=False,
            )
        elif observations:
            embed.add_field(
                name="CPI Inflation (YoY)",
                value="Unavailable — FRED has not published a full 13-month window yet.",
                inline=False,
            )

        embed.set_footer(
            text=(
                "Source: FRED · FEDFUNDS is a monthly average; DGS10/DGS2 are daily; "
                "CPI is seasonally adjusted"
            )
        )
        return embed

    # ----------------------------------------------------------- /earnings

    @app_commands.command(
        name="earnings",
        description="Next scheduled earnings date, and the last reported quarter.",
    )
    @app_commands.describe(ticker="Ticker symbol, e.g. AAPL")
    async def earnings(self, interaction: discord.Interaction, ticker: str) -> None:
        symbol = ticker.strip().upper()
        if not symbol or len(symbol) > 12 or any(
            character.isspace() for character in symbol
        ):
            await interaction.response.send_message(
                f"`{ticker.strip()}` doesn't look like a ticker symbol.", ephemeral=True
            )
            return

        if self._finnhub is None:
            await interaction.response.send_message(
                "`FINNHUB_KEY` isn't set, so earnings data is unavailable. "
                "Add it to `.env` and restart the bot.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            upcoming = await retry_async(
                self._fetch_next_earnings,
                symbol,
                attempts=2,
                base_delay=2.0,
                label=f"Finnhub calendar:{symbol}",
            )
            reported = await retry_async(
                self._fetch_reported_quarters,
                symbol,
                attempts=2,
                base_delay=2.0,
                label=f"Finnhub earnings:{symbol}",
            )
        except Exception:
            log.exception("Earnings lookup failed for %s", symbol)
            await interaction.followup.send(
                embed=self._error_embed(
                    "Earnings data unavailable",
                    f"Couldn't reach Finnhub for `{symbol}`. Try again in a moment.",
                )
            )
            return

        if upcoming is None and not reported:
            await interaction.followup.send(
                embed=self._error_embed(
                    f"No earnings data: {symbol}",
                    f"Finnhub has no earnings records for `{symbol}`. Check the "
                    "spelling — it has to match the symbol Finnhub uses.",
                )
            )
            return

        await interaction.followup.send(
            embed=self._earnings_embed(symbol, upcoming, reported)
        )

    async def _fetch_next_earnings(self, symbol: str) -> dict[str, Any] | None:
        """The soonest scheduled report, or None if Finnhub lists none."""
        today = datetime.now(config.EASTERN).date()
        payload = await run_blocking(
            self._finnhub.earnings_calendar,
            _from=today.isoformat(),
            to=(today + timedelta(days=EARNINGS_LOOKAHEAD_DAYS)).isoformat(),
            symbol=symbol,
            international=False,
        )
        entries = (payload or {}).get("earningsCalendar") or []
        dated = [
            entry
            for entry in entries
            if isinstance(entry, dict) and parse_iso_date(entry.get("date"))
        ]
        if not dated:
            return None
        # Finnhub returns these furthest-out first, so take the minimum rather
        # than trusting the ordering.
        return min(dated, key=lambda entry: str(entry["date"]))

    async def _fetch_reported_quarters(self, symbol: str) -> list[dict[str, Any]]:
        """Past quarters, newest first (which is the order Finnhub returns)."""
        rows = await run_blocking(
            self._finnhub.company_earnings, symbol, REPORTED_QUARTERS
        )
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _earnings_embed(
        self,
        symbol: str,
        upcoming: dict[str, Any] | None,
        reported: list[dict[str, Any]],
    ) -> discord.Embed:
        surprise = reported[0].get("surprisePercent") if reported else None
        color = COLOR_NEUTRAL
        if isinstance(surprise, (int, float)):
            color = COLOR_UP if surprise >= 0 else COLOR_DOWN

        embed = discord.Embed(
            title=f"Earnings: {symbol}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Next report", value=self._next_report_text(upcoming), inline=False
        )
        embed.add_field(
            name="Last reported", value=self._last_report_text(reported), inline=False
        )
        embed.set_footer(text="Source: Finnhub · times are US/Pacific")
        return embed

    @staticmethod
    def _next_report_text(upcoming: dict[str, Any] | None) -> str:
        if upcoming is None:
            return "*No upcoming report on Finnhub's calendar.*"

        day = parse_iso_date(upcoming.get("date"))
        lines = [
            f"**{day:%A, %B} {day.day}, {day.year}**"
            if day
            else "**Date not published**"
        ]

        timing = timing_label(str(upcoming.get("hour") or ""))
        lines.append(timing or "*Session not yet confirmed*")

        figures = []
        estimate = upcoming.get("epsEstimate")
        revenue = upcoming.get("revenueEstimate")
        if isinstance(estimate, (int, float)):
            figures.append(f"EPS est {format_money(estimate)}")
        if isinstance(revenue, (int, float)):
            figures.append(f"revenue est ${format_compact(revenue)}")
        lines.append(" · ".join(figures) if figures else "*No consensus published yet*")

        return "\n".join(lines)

    @staticmethod
    def _last_report_text(reported: list[dict[str, Any]]) -> str:
        if not reported:
            return "*No reported quarters on record.*"

        latest = reported[0]
        quarter, year = latest.get("quarter"), latest.get("year")
        period = parse_iso_date(latest.get("period"))

        heading = f"**Q{quarter} {year}**" if quarter and year else "**Last quarter**"
        if period:
            heading += f" · quarter ended {period:%b} {period.day}, {period.year}"

        comparison = (
            f"Actual {format_money(latest.get('actual'))} "
            f"vs est {format_money(latest.get('estimate'))}"
        )
        surprise = latest.get("surprisePercent")
        if isinstance(surprise, (int, float)):
            comparison += f" · **{format_signed(surprise, 2, '%')}** surprise"

        lines = [heading, comparison]

        prior = [
            f"Q{row.get('quarter')} {format_money(row.get('actual'))}"
            for row in reported[1:]
            if row.get("quarter") and isinstance(row.get("actual"), (int, float))
        ]
        if prior:
            lines.append("*Prior: " + " · ".join(prior) + "*")

        return "\n".join(lines)

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _error_embed(title: str, description: str) -> discord.Embed:
        return discord.Embed(title=title, description=description, color=COLOR_ERROR)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MarketCommands(bot))
