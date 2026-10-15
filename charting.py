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

import matplotlib

matplotlib.use("Agg")  # must precede the pyplot import below

import matplotlib.dates as mdates  # noqa: E402
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

# (button label, yfinance period, yfinance interval)
TIMEFRAMES: tuple[tuple[str, str, str], ...] = (
    ("1D", "1d", "2m"),
    ("1W", "5d", "15m"),
    ("1M", "1mo", "1d"),
    ("1Y", "1y", "1d"),
)
DEFAULT_TIMEFRAME = "1D"

# Each button click is a Yahoo request. Caching briefly stops someone flicking
# through the four timeframes from tripping the rate limit.
CACHE_TTL_SECONDS = 45
CACHE_MAX_ENTRIES = 64

# pyplot keeps global figure state, so two renders on different threads can
# tread on each other. Charts are small and infrequent; serialising is cheap.
_render_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, bytes]] = {}


class ChartUnavailable(Exception):
    """The symbol has no usable price history for the requested window."""


def timeframe_args(label: str) -> tuple[str, str]:
    """(period, interval) for a button label."""
    for name, period, interval in TIMEFRAMES:
        if name == label:
            return period, interval
    raise KeyError(label)


def label_for(period: str, interval: str) -> str:
    """The button label matching a (period, interval) pair, for titling."""
    for name, known_period, known_interval in TIMEFRAMES:
        if (known_period, known_interval) == (period, interval):
            return name
    return period


def generate_stock_chart(symbol: str, period: str, interval: str) -> io.BytesIO:
    """Fetch history for `symbol` and render it as a PNG.

    Returns a BytesIO positioned at index 0. Raises ChartUnavailable when there
    is nothing worth plotting. Blocking — call it via `utils.run_blocking()`.
    """
    key = (symbol.upper(), period, interval)
    cached = _cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < CACHE_TTL_SECONDS:
        return io.BytesIO(cached[1])

    history = yf.Ticker(symbol).history(period=period, interval=interval)
    png = render_chart(symbol, history, period, interval).getvalue()

    if len(_cache) >= CACHE_MAX_ENTRIES:
        # Drop the oldest entry rather than maintaining a full LRU.
        oldest = min(_cache, key=lambda entry: _cache[entry][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.monotonic(), png)

    return io.BytesIO(png)


def render_chart(symbol: str, history, period: str, interval: str) -> io.BytesIO:
    """Draw a chart from an already-fetched price history.

    Split out from the fetch so it can be exercised offline, with a synthetic
    frame, by `verify.py`.
    """
    if history is None or getattr(history, "empty", True):
        raise ChartUnavailable(f"No {period}/{interval} price history for {symbol}")
    if "Close" not in history:
        raise ChartUnavailable(f"Price history for {symbol} has no close prices")

    closes = history["Close"].dropna()
    if len(closes) < 2:
        raise ChartUnavailable(f"Not enough {period} data points for {symbol}")

    # The brief colours the line green when the latest close is at or above the
    # period's opening price, so the first open is the reference point.
    opens = history["Open"].dropna() if "Open" in history else closes.iloc[:0]
    opening = float(opens.iloc[0]) if len(opens) else float(closes.iloc[0])
    latest = float(closes.iloc[-1])
    change = latest - opening
    percent = (change / opening * 100) if opening else 0.0
    color = UP_COLOR if latest >= opening else DOWN_COLOR

    values = closes.to_numpy(dtype=float)
    lowest = float(values.min())
    highest = float(values.max())
    if highest <= lowest:
        # A flat series would collapse the y-axis to zero height.
        highest = lowest + max(abs(lowest) * 0.01, 1.0)
    span = highest - lowest
    floor = lowest - span * 0.08
    ceiling = highest + span * 0.08

    with _render_lock:
        figure, axes = plt.subplots(figsize=(9, 3.6), dpi=140)
        figure.patch.set_facecolor(BACKGROUND)
        axes.set_facecolor(BACKGROUND)
        axes.set_ylim(floor, ceiling)

        _fill_gradient(axes, closes.index, values, floor, ceiling - floor, color)
        axes.plot(closes.index, values, color=color, linewidth=1.6, zorder=3)
        # Set after the fill: imshow rescales the axes to its own extent.
        axes.set_ylim(floor, ceiling)

        locator = mdates.AutoDateLocator(minticks=3, maxticks=6)
        axes.xaxis.set_major_locator(locator)
        axes.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))

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
            f"{symbol.upper()}  ·  {label_for(period, interval)}\n{escaped}",
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


def _fill_gradient(axes, index, values, baseline: float, height: float, color: str) -> None:
    """Draw a faint fade beneath the price line.

    Painted as one vertical ramp, clipped to the polygon under the curve.
    Stacking fill_between() bands is the obvious alternative, but every band
    boundary is an alpha step, and wherever the curve rises above that boundary
    the step shows as a hard horizontal seam — very visible on a short series
    like a 1M chart, where the line only has ~22 points.
    """
    x = mdates.date2num(index)

    # Close the polygon: along the curve, then back along the baseline.
    vertices = list(zip(x, values))
    vertices += [(x[-1], baseline), (x[0], baseline)]
    clip = PathPatch(
        MplPath(vertices), facecolor="none", edgecolor="none", transform=axes.transData
    )
    axes.add_patch(clip)

    fade = LinearSegmentedColormap.from_list(
        "chart_fade", [to_rgba(color, 0.0), to_rgba(color, 0.22)]
    )
    image = axes.imshow(
        np.linspace(0.0, 1.0, 256).reshape(-1, 1),
        extent=[x[0], x[-1], baseline, baseline + height],
        aspect="auto",
        origin="lower",
        cmap=fade,
        zorder=2,
    )
    image.set_clip_path(clip)
