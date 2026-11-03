"""Time helpers: ONE place decides what "today" is for day_id and for view names.

request_ts is stored in UTC (the existing convention); day_id is the calendar date of that
moment in the *application* time zone (config: app.timezone), so a request made at 01:30 local
time on the 27th is recorded as the 27th, not the 26th.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# Who calls it: chatbot_api.py (app_timezone once at startup; utc_now, to_day_id and to_utc_naive
#   when a report request is created). day_id also ends up in the view name (naming.py).
# What it calls: only the standard library (datetime, zoneinfo).
# "aware" datetime = has a time zone attached; "naive" datetime = no zone (just numbers).
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import logging
from datetime import date, datetime, timezone, tzinfo

log = logging.getLogger("chatbot.clock")   # -> logs/backend_billing.log


# Current time as an aware UTC datetime. chatbot_api.py uses it as its default clock
# (tests can inject a fake clock instead).
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


# Reads config app.timezone (e.g. "Asia/Kolkata") and returns a tzinfo object for it.
# zoneinfo needs the IANA time-zone database; Windows does not ship one, so the `tzdata`
# pip package provides it. If it is missing we warn and use UTC instead of crashing.
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


# The calendar date of `moment` in the application time zone. A naive input is assumed to be UTC.
def to_day_id(moment: datetime, tz: tzinfo) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz).date()


def to_utc_naive(moment: datetime) -> datetime:
    """MySQL DATETIME columns hold UTC without a zone."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(timezone.utc).replace(tzinfo=None)
