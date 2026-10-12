"""Small helpers shared by more than one cog.

Two things live here: number formatting for embeds, and the async plumbing that
keeps blocking network calls off the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import discord

import config

log = logging.getLogger(__name__)

T = TypeVar("T")

CURRENCY_SYMBOLS = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "CNY": "¥",
    "INR": "₹",
    "CAD": "C$",
    "AUD": "A$",
    "HKD": "HK$",
    "KRW": "₩",
    "BRL": "R$",
    "CHF": "CHF ",
}


def format_compact(value: Any) -> str:
    """Format a large number for an embed: 2845000000000 -> '2.85T'."""
    number = _to_float(value)
    if number is None:
        return "N/A"
    for suffix, threshold in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(number) >= threshold:
            return f"{number / threshold:,.2f}{suffix}"
    if number.is_integer():
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def format_money(value: Any, currency: str = "USD") -> str:
    """Format a price with its currency symbol: 189.5, 'USD' -> '$189.50'."""
    number = _to_float(value)
    if number is None:
        return "N/A"
    code = (currency or "USD").upper()
    symbol = CURRENCY_SYMBOLS.get(code)
    if symbol:
        return f"{symbol}{number:,.2f}"
    return f"{number:,.2f} {code}"


def format_number(value: Any, decimals: int = 2) -> str:
    """Format a plain number, or 'N/A' when it is missing."""
    number = _to_float(value)
    if number is None:
        return "N/A"
    return f"{number:,.{decimals}f}"


def format_signed(value: Any, decimals: int = 2, suffix: str = "") -> str:
    """Format a number with an explicit sign: '+0.31%', '-12.0 bps'."""
    number = _to_float(value)
    if number is None:
        return "N/A"
    return f"{number:+,.{decimals}f}{suffix}"


def to_pacific_clock(hour: int, minute: int, reference: date | None = None) -> str:
    """Render a US/Eastern time of day as a US/Pacific clock string, e.g. '05:30'.

    Times in this project are defined on the Eastern clock because that is what
    the exchanges and the release schedule use; this converts for display. The
    conversion goes through the timezone database against a real date rather
    than subtracting a fixed three hours, so the correct standard or daylight
    offset is applied and the EST/EDT and PST/PDT transitions are handled for
    us.
    """
    anchor_day = reference or datetime.now(config.PACIFIC).date()
    eastern = datetime.combine(
        anchor_day, dtime(hour=hour, minute=minute), tzinfo=config.EASTERN
    )
    return f"{eastern.astimezone(config.PACIFIC):%H:%M}"


# Reporting slots, defined on the US/Eastern clock the exchange runs on. The
# Pacific range shown in the label is derived from them, never typed in.
TIMING_WINDOWS = {
    "bmo": ("Pre-Market", 8, 30, 9, 30),
    "amc": ("Post-Market", 16, 0, 17, 0),
    "dmh": ("Market Hours", 9, 30, 16, 0),
}


def parse_iso_date(value: Any) -> date | None:
    """Parse a YYYY-MM-DD string, returning None when it is anything else.

    Both Finnhub and FRED hand back dates as plain strings, and both occasionally
    leave one blank, so callers can treat a bad value as simply missing.
    """
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def timing_label(code: str) -> str:
    """Human-readable reporting slot, e.g. 'Pre-Market (~05:30-06:30 PT)'.

    Returns "" for an unknown or missing code, so callers can simply drop the
    part rather than having to special-case it.
    """
    window = TIMING_WINDOWS.get((code or "").lower())
    if window is None:
        return ""
    name, start_hour, start_minute, end_hour, end_minute = window
    start = to_pacific_clock(start_hour, start_minute)
    end = to_pacific_clock(end_hour, end_minute)
    return f"{name} (~{start}-{end} PT)"


async def run_blocking(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking call (yfinance, fredapi, finnhub) in a worker thread.

    Those libraries do synchronous HTTP under the hood. Calling them directly
    from a coroutine would freeze every other command until the request
    finished, which on a 24/7 bot means dropped heartbeats.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


async def resolve_channel(client: discord.Client, channel_id: int | None) -> discord.abc.Messageable | None:
    """Look up a channel by id, falling back to an API call if it isn't cached.

    Scheduled tasks often run right after a restart, when the channel cache is
    still cold. get_channel() returns None there, which would silently drop the
    post, so fall back to fetch_channel().
    """
    if channel_id is None:
        return None
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await client.fetch_channel(channel_id)
    except discord.HTTPException:
        log.warning("Channel %s could not be resolved", channel_id, exc_info=True)
        return None


async def retry_async(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    attempts: int = 3,
    base_delay: float = 2.0,
    label: str = "request",
    give_up_on: tuple[type[BaseException], ...] = (),
    **kwargs: Any,
) -> T:
    """Await `func(*args, **kwargs)`, retrying transient failures with backoff.

    Delays double each time: base_delay, 2x, 4x, ... Exceptions listed in
    `give_up_on` are re-raised immediately because retrying cannot help
    (an unknown ticker will still be unknown on the second try).
    """
    attempts = max(1, attempts)
    last_error: BaseException | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await func(*args, **kwargs)
        except give_up_on:
            raise
        except Exception as error:  # noqa: BLE001 - deliberately broad, this is a retry wrapper
            last_error = error
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            log.warning(
                "%s failed (attempt %d/%d): %s — retrying in %.1fs",
                label,
                attempt,
                attempts,
                error,
                delay,
            )
            await asyncio.sleep(delay)

    assert last_error is not None  # loop ran at least once, so this is always set
    raise last_error


def _to_float(value: Any) -> float | None:
    """Best-effort float conversion; None for anything unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # NaN slips through float() and would render as the literal text 'nan'.
    if number != number:  # noqa: PLR0124 - NaN check without importing math
        return None
    return number


def load_json(path: Path, default: T) -> T:
    """Read a JSON file, falling back to `default` when it is missing or corrupt.

    A bot that cannot start because a cache file got truncated is worse than one
    that starts empty, so every failure here is non-fatal.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError):
        log.warning("Could not read %s; using the default", path, exc_info=True)
        return default


def save_json(path: Path, payload: Any) -> bool:
    """Write JSON atomically, returning False if it could not be saved.

    The data goes to a temporary file first and is then moved into place, so an
    interrupted write can never leave a half-written file behind.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        temporary.replace(path)
        return True
    except OSError:
        log.warning("Could not write %s", path, exc_info=True)
        return False
