"""Stock chart rendering for `/stock`.

Matplotlib is pinned to the Agg backend *before* pyplot is imported. A headless
Linux host has no display server, and importing pyplot while a GUI backend is
selected raises at import time — which would stop the bot from starting at all,
not merely break the chart.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from datetime import time as dtime
from typing import NamedTuple

import matplotlib

matplotlib.use("Agg")  # must precede the pyplot import below

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import yfinance as yf  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, to_rgba  # noqa: E402
from matplotlib.patches import PathPatch  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402

from utils import format_money  # noqa: E402

log = logging.getLogger(__name__)

CHART_FILENAME = "chart.png"

# Discord's own dark surface, so the image sits flush inside an embed.
BACKGROUND = "#2f3136"
UP_COLOR = "#00ff88"
DOWN_COLOR = "#ff3344"
AXIS_COLOR = "#b9bbbe"
TEXT_COLOR = "#ffffff"

# X-axis labels to aim for. The brief asks for four to six; five fits this
# figure width without the labels colliding.
TICK_COUNT = 5
DEFAULT_TICK_FORMAT = "%b %d"
# A day-tick timeframe never needs more than this before the labels collide.
MAX_DAY_TICKS = 7

# Bars stamped at or before this belong to the regular session. Used to find the
# official close, as opposed to the 20:00 end of after-hours trading.
REGULAR_CLOSE = dtime(16, 0)

# Where the period's return is measured from.
BASELINE_FIRST_BAR = "first_bar"
BASELINE_PREVIOUS_CLOSE = "previous_close"

# Each button click is a Yahoo request. Caching briefly stops someone flicking
# through the timeframes from tripping the rate limit.
CACHE_TTL_SECONDS = 45
CACHE_MAX_ENTRIES = 64

# pyplot keeps global figure state, so two renders on different threads can
# tread on each other. Charts are small and infrequent; serialising is cheap.
_render_lock = threading.Lock()
_cache: dict[tuple[str, str, str, bool], tuple[float, bytes]] = {}


class Timeframe(NamedTuple):
    """One timeframe button: what to fetch, and how to draw it."""

    label: str
    period: str
    interval: str
    tick_format: str
    prepost: bool = False
    day_ticks: bool = False
    session_only: bool = False
    baseline: str = BASELINE_FIRST_BAR
    fallback_interval: str | None = None


TIMEFRAMES: tuple[Timeframe, ...] = (
    # Fetched over 5 days, not 1: the chart is trimmed to the latest session,
    # and the wider window is what makes the previous close available to measure
    # the return against. See BASELINE_PREVIOUS_CLOSE below.
    Timeframe(
        "1D",
        "5d",
        "2m",
        "%H:%M",
        prepost=True,
        session_only=True,
        baseline=BASELINE_PREVIOUS_CLOSE,
    ),
    # One tick per trading day, not five evenly spaced ones — the sessions are
    # the natural divisions of a week.
    Timeframe("1W", "5d", "5m", "%a", prepost=True, day_ticks=True),
    # Hourly bars, not daily. A month of daily closes is ~21 bare points, which
    # reads as a zigzag; a month of hourly bars is ~150 and reads as a curve.
    Timeframe("1M", "1mo", "1h", "%b %d"),
    Timeframe("3M", "3mo", "1h", "%b %d", fallback_interval="1d"),
    Timeframe("1Y", "1y", "1d", "%b '%y"),
)
DEFAULT_TIMEFRAME = "1D"


class ChartUnavailable(Exception):
    """The symbol has no usable price history for the requested window."""


def timeframe(label: str) -> Timeframe | None:
    """The definition behind a button label."""
    for entry in TIMEFRAMES:
        if entry.label == label:
            return entry
    return None


def timeframe_for(period: str, interval: str) -> Timeframe | None:
    """The definition matching a (period, interval) pair."""
    for entry in TIMEFRAMES:
        if (entry.period, entry.interval) == (period, interval):
            return entry
    return None


def timeframe_args(label: str) -> tuple[str, str]:
    """(period, interval) for a button label."""
    entry = timeframe(label)
    if entry is None:
        raise KeyError(label)
    return entry.period, entry.interval


def label_for(period: str, interval: str) -> str:
    """The button label matching a (period, interval) pair, for titling."""
    entry = timeframe_for(period, interval)
    return entry.label if entry else period


def tick_positions(count: int, wanted: int = TICK_COUNT) -> list[int]:
    """Evenly spaced bar positions for the x-axis, both ends included.

    Positions, not timestamps: the axis is indexed by bar, so ticks land evenly
    whatever gaps the calendar holds.
    """
    if count <= 0:
        return []
    if count <= wanted:
        return list(range(count))
    return sorted({int(round(value)) for value in np.linspace(0, count - 1, wanted)})


def day_start_positions(index) -> list[int]:
    """Bar position where each calendar day begins."""
    positions: list[int] = []
    seen = None
    for position, stamp in enumerate(index):
        day = stamp.date()
        if day != seen:
            positions.append(position)
            seen = day
    return positions


def trim_to_last_session(frame):
    """Keep only the bars belonging to the most recent trading day.

    1D is fetched over a wider window so the previous close is available; the
    line itself should show a single session, which is what "1D" means.
    """
    if frame is None or frame.empty:
        return frame
    last_day = frame.index[-1].date()
    return frame[[stamp.date() == last_day for stamp in frame.index]]


def previous_session_close(frame) -> float | None:
    """The last regular-hours close before the most recent session.

    Robinhood measures the 1D change against this, not against the first bar of
    the extended session. The two differ because with extended hours enabled the
    first bar is the 04:00 pre-market, so measuring from it understates the move
    by a small amount every day.
    """
    if frame is None or frame.empty:
        return None
    last_day = frame.index[-1].date()
    earlier = [
        position
        for position, stamp in enumerate(frame.index)
        if stamp.date() < last_day and stamp.time() <= REGULAR_CLOSE
    ]
    if not earlier:
        return None
    return float(frame["Close"].iloc[earlier[-1]])


def generate_stock_chart(
    symbol: str, period: str, interval: str, label: str | None = None
) -> io.BytesIO:
    """Fetch history for `symbol` and render it as a PNG.

    Returns a BytesIO positioned at index 0. Raises ChartUnavailable when there
    is nothing worth plotting. Blocking — call it via `utils.run_blocking()`.

    `label` names the button this chart belongs to and decides the title, the
    tick format, whether extended hours are included, and where the return is
    measured from. Without it those are inferred from period/interval.
    """
    entry = timeframe(label) if label else timeframe_for(period, interval)
    prepost = entry.prepost if entry else False
    key = (symbol.upper(), period, interval, prepost)

    cached = _cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return io.BytesIO(cached[1])

    history = _fetch(symbol, period, interval, prepost)

    # Measured before trimming, because the previous close lives on the day the
    # trim removes.
    baseline = None
    if entry is not None and entry.baseline == BASELINE_PREVIOUS_CLOSE:
        baseline = previous_session_close(history)

    if entry is not None and entry.session_only:
        history = trim_to_last_session(history)

    try:
        buffer = render_chart(symbol, history, period, interval, entry, baseline)
    except ChartUnavailable:
        fallback = entry.fallback_interval if entry else None
        if fallback is None:
            raise
        # Yahoo has no intraday bars this far back for some symbols and none at
        # all for thinly traded ones. Daily closes beat no chart.
        log.info(
            "No %s bars for %s over %s — retrying at %s",
            interval,
            symbol,
            period,
            fallback,
        )
        history = _fetch(symbol, period, fallback, prepost)
        buffer = render_chart(symbol, history, period, fallback, entry, None)

    png = buffer.getvalue()

    if len(_cache) >= CACHE_MAX_ENTRIES:
        # Drop the oldest entry rather than maintaining a full LRU.
        oldest = min(_cache, key=lambda existing: _cache[existing][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic(), png)

    return io.BytesIO(png)


def _fetch(symbol: str, period: str, interval: str, prepost: bool):
    return yf.Ticker(symbol).history(
        period=period, interval=interval, prepost=prepost
    )


def render_chart(
    symbol: str,
    history,
    period: str,
    interval: str,
    entry: Timeframe | None = None,
    baseline_price: float | None = None,
) -> io.BytesIO:
    """Draw a chart from an already-fetched price history.

    Split out from the fetch so it can be exercised offline, with a synthetic
    frame, by `verify.py`. `baseline_price` overrides the default of measuring
    from the period's opening bar.
    """
    if history is None or getattr(history, "empty", True):
        raise ChartUnavailable(f"No {period}/{interval} price history for {symbol}")
    if "Close" not in history:
        raise ChartUnavailable(f"Price history for {symbol} has no close prices")

    closes = history["Close"].dropna()
    if len(closes) < 2:
        raise ChartUnavailable(f"Not enough {period} data points for {symbol}")

    if baseline_price is not None:
        start = float(baseline_price)
    else:
        # Otherwise the period's own opening price is the reference point.
        opens = history["Open"].dropna() if "Open" in history else closes.iloc[:0]
        start = float(opens.iloc[0]) if len(opens) else float(closes.iloc[0])

    latest = float(closes.iloc[-1])
    change = latest - start
    percent = (change / start * 100) if start else 0.0
    color = UP_COLOR if latest >= start else DOWN_COLOR

    values = closes.to_numpy(dtype=float)

    # Plotted against bar position rather than timestamps. A datetime axis draws
    # a straight diagonal across every weekend and overnight close — a 1Y daily
    # series has ~55 such gaps, each one a line that never happened.
    positions = np.arange(len(values), dtype=float)

    lowest = float(values.min())
    highest = float(values.max())
    if highest <= lowest:
        # A flat series would collapse the y-axis to zero height.
        highest = lowest + max(abs(lowest) * 0.01, 1.0)
    span = highest - lowest
    floor = lowest - span * 0.08
    ceiling = highest + span * 0.08
    left = positions[0] - 0.5
    right = positions[-1] + 0.5

    title_label = entry.label if entry else label_for(period, interval)
    tick_format = entry.tick_format if entry else DEFAULT_TICK_FORMAT

    with _render_lock:
        figure, axes = plt.subplots(figsize=(9, 3.6), dpi=140)
        figure.patch.set_facecolor(BACKGROUND)
        axes.set_facecolor(BACKGROUND)

        _fill_gradient(axes, positions, values, floor, ceiling - floor, left, right, color)
        axes.plot(positions, values, color=color, linewidth=1.6, zorder=3)

        # Set after the fill: imshow rescales the axes to its own extent.
        axes.set_xlim(left, right)
        axes.set_ylim(floor, ceiling)

        ticks = tick_positions(len(values))
        if entry is not None and entry.day_ticks:
            day_starts = day_start_positions(closes.index)
            # A 5d window holds five sessions; past seven the labels would
            # collide, so fall back to even spacing if it is ever wider.
            if 2 <= len(day_starts) <= MAX_DAY_TICKS:
                ticks = day_starts
        axes.set_xticks(ticks)
        axes.set_xticklabels(
            [closes.index[int(position)].strftime(tick_format) for position in ticks]
        )

        axes.grid(True, linestyle="--", alpha=0.2, color=TEXT_COLOR, linewidth=0.6)
        axes.set_axisbelow(True)
        for spine in axes.spines.values():
            spine.set_visible(False)
        axes.tick_params(colors=AXIS_COLOR, labelsize=8, length=0)

        # matplotlib reads a pair of '$' as mathtext, so the currency symbol has
        # to be escaped or the subtitle renders as italic maths.
        subtitle = f"{format_money(latest)}  ({change:+,.2f}, {percent:+.2f}%)"
        escaped = subtitle.replace("$", "\\$")
        axes.set_title(
            f"{symbol.upper()}  ·  {title_label}\n{escaped}",
            color=TEXT_COLOR,
            fontsize=11,
            loc="left",
            pad=10,
        )

        buffer = io.BytesIO()
        figure.savefig(
            buffer,
            format="png",
            facecolor=figure.get_facecolor(),
            bbox_inches="tight",
            pad_inches=0.25,
        )
        plt.close(figure)

    buffer.seek(0)
    return buffer


def _fill_gradient(
    axes,
    positions,
    values,
    baseline: float,
    height: float,
    left: float,
    right: float,
    color: str,
) -> None:
    """Draw a faint fade beneath the price line.

    Painted as one vertical ramp, clipped to the polygon under the curve.
    Stacking fill_between() bands is the obvious alternative, but every band
    boundary is an alpha step, and wherever the curve rises above that boundary
    the step shows as a hard horizontal seam — very visible on a short series.
    """
    # Close the polygon: along the curve, then back along the baseline.
    vertices = list(zip(positions, values))
    vertices += [(positions[-1], baseline), (positions[0], baseline)]
    clip = PathPatch(
        MplPath(vertices), facecolor="none", edgecolor="none", transform=axes.transData
    )
    axes.add_patch(clip)

    fade = LinearSegmentedColormap.from_list(
        "chart_fade", [to_rgba(color, 0.0), to_rgba(color, 0.22)]
    )
    image = axes.imshow(
        np.linspace(0.0, 1.0, 256).reshape(-1, 1),
        extent=[left, right, baseline, baseline + height],
        aspect="auto",
        origin="lower",
        cmap=fade,
        zorder=2,
    )
    image.set_clip_path(clip)
