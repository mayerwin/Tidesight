"""Time-zone helpers: site-local dates for UTC millisecond timestamps."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Optional

log = logging.getLogger("tidesight.enrich")

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


@lru_cache(maxsize=256)
def zone(name: Optional[str]) -> tzinfo:
    """The tzinfo for an IANA name; UTC (with a warning) when it is unknown or unavailable."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # unknown zone or no tz database on this host
            pass
    log.warning("time zone %r unavailable, using UTC", name)
    return timezone.utc


def _local(ms: int, tz_name: Optional[str]) -> datetime:
    try:
        return datetime.fromtimestamp(ms / 1000.0, zone(tz_name))
    except (OverflowError, OSError, ValueError):  # outside what datetime handles: the epoch
        return datetime.fromtimestamp(0, timezone.utc)


def local_day(ms: int, tz_name: Optional[str]) -> str:
    """YYYY-MM-DD of a UTC millisecond timestamp in the given time zone."""
    return _local(ms, tz_name).strftime("%Y-%m-%d")


def local_hour(ms: int, tz_name: Optional[str]) -> int:
    """Hour of the day (0-23) of a UTC millisecond timestamp in the given time zone."""
    return _local(ms, tz_name).hour


def day_bounds(day: str, tz_name: Optional[str]) -> tuple:
    """(start_ms, end_ms) of a local day, end exclusive."""
    tz = zone(tz_name)
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz)
    end = start + timedelta(days=1)
    end = datetime(end.year, end.month, end.day, tzinfo=tz)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
