"""Central configuration for the Discord finance bot.

Every environment variable is read here and nowhere else, so there is exactly
one place that knows how `.env` is shaped. Cogs import the names they need.

The .env file itself is never read into logs, never printed, and is listed in
.gitignore.
"""

from __future__ import annotations

import os
from datetime import tzinfo
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"

# Loads .env into os.environ. Existing environment variables win, which is what
# you want when a hosting provider injects the values directly.
load_dotenv(ENV_PATH)


def _read(name: str) -> str:
    """Return an environment variable as a trimmed string, or "" if unset."""
    return (os.getenv(name) or "").strip()


DISCORD_TOKEN = _read("DISCORD_TOKEN")
FINNHUB_KEY = _read("FINNHUB_KEY")
FRED_API_KEY = _read("FRED_API_KEY")

# TARGET_CHANNEL_ID arrives as text; convert it once so cogs get an int.
_raw_channel_id = _read("TARGET_CHANNEL_ID")
TARGET_CHANNEL_ID: int | None = int(_raw_channel_id) if _raw_channel_id.isdigit() else None

# Scheduling timezone. `zoneinfo` is stdlib but on Windows it has no system tz
# database to read, so it depends on the `tzdata` package listed in
# requirements.txt. pytz is kept as a fallback in case that package is missing.
try:
    from zoneinfo import ZoneInfo

    EASTERN: tzinfo = ZoneInfo("America/New_York")
    TIMEZONE_BACKEND = "zoneinfo"
except Exception:  # pragma: no cover - only on a machine with no tz database
    import pytz

    EASTERN = pytz.timezone("America/New_York")
    TIMEZONE_BACKEND = "pytz"

REQUIRED_VARIABLES = {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "FINNHUB_KEY": FINNHUB_KEY,
    "TARGET_CHANNEL_ID": _raw_channel_id,
    "FRED_API_KEY": FRED_API_KEY,
}


def missing_variables() -> list[str]:
    """Names of the required variables that are absent or blank."""
    return [name for name, value in REQUIRED_VARIABLES.items() if not value]
