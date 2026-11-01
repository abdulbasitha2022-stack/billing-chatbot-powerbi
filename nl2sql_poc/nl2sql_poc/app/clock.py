"""Time helpers: ONE place decides what "today" is for day_id and for view names.

request_ts is stored in UTC (the existing convention); day_id is the calendar date of that
moment in the *application* time zone (config: app.timezone), so a request made at 01:30 local
time on the 27th is recorded as the 27th, not the 26th.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone, tzinfo

log = logging.getLogger("chatbot.clock")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def app_timezone(cfg: dict) -> tzinfo:
    name = str(((cfg.get("app") or {}).get("timezone")) or "UTC")
    if name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:      # e.g. Windows without the `tzdata` package installed
        log.warning("Time zone %r is not available (pip install tzdata). Falling back to UTC.", name)
        return timezone.utc


def to_day_id(moment: datetime, tz: tzinfo) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def to_utc_naive(moment: datetime) -> datetime:
    """MySQL DATETIME columns hold UTC without a zone."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
