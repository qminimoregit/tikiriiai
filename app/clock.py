"""The one place the app reads the clock, so tests can pin it.

The database stores naive UTC datetimes; times shown to people or the model are in the company's timezone.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    """Current time as naive UTC (what the database stores)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_local(dt_utc: datetime, tz: ZoneInfo) -> datetime:
    """A naive UTC datetime as an aware datetime in the company's timezone."""
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(tz)


def to_utc(local: datetime, tz: ZoneInfo) -> datetime:
    """A naive datetime on the company's wall clock as naive UTC (what the database stores)."""
    return local.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def safe_zone(name: str, fallback: str = "Asia/Colombo") -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - a bad name in the DB must not break calls
        return ZoneInfo(fallback)
