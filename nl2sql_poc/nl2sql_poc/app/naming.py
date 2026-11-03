"""View naming convention:  vw_<description>_<YYYYMMDD>_<request>[_<suffix>]

    vw_refunds_by_reason_20261027_3fa91c07b2de

* description - short, from the report title (lowercase letters, digits, underscores only)
* YYYYMMDD    - the request's day_id (application time zone)
* request     - the request_id (12 lowercase hex characters, the metadata table's primary key)
* suffix      - only added when the plain name is already taken (4-8 random hex characters)

Because request_id is unique, the plain name is normally unique too; the suffix only covers a
second view for the same request (e.g. a retried manual report) or a name taken in a race.

MySQL limits identifiers to 64 characters, so the description is shortened first; the date,
request and suffix are always kept. Names are validated before they are ever put in SQL.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# Who calls it: chatbot_api.py calls new_request_id() when a report request starts and
#   unique_view_name() to pick the MySQL view name for a new report; report_store.py calls
#   validate_view_name() before any SQL that contains the name.
# What it calls: util.slugify (turns a title into lowercase_with_underscores).
# Why so strict? A view name has to be written INTO the SQL text (CREATE VIEW `name` ...) since
#   identifiers cannot be passed as parameters. Allowing only [a-z0-9_] makes injection impossible.
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import re
import uuid
from datetime import date, datetime

from .util import slugify

MAX_VIEW_NAME_LEN = 64          # MySQL's identifier length limit
REQUEST_ID_LEN = 12             # length of a new request_id (and of its part in the view name)
# "vw_" followed by one or more lowercase letters, digits or underscores - and nothing else.
VIEW_NAME_RE = re.compile(r"^vw_[a-z0-9_]+$")


class InvalidViewName(ValueError):
    pass


# The single safety gate for view names; returns the name unchanged or raises InvalidViewName.
def validate_view_name(name) -> str:
    """Return `name` if it is safe to use as a view identifier, else raise."""
    if not isinstance(name, str) or len(name) > MAX_VIEW_NAME_LEN or not VIEW_NAME_RE.fullmatch(name):
        raise InvalidViewName("Unsafe view name: must be vw_ + lowercase letters, digits and underscores "
                              f"(max {MAX_VIEW_NAME_LEN} characters).")
    return name


# A new request_id: 12 random lowercase hex characters (48 bits) from a UUID. Short enough to
# put in view names, random enough that two requests practically never get the same id.
def new_request_id() -> str:
    return uuid.uuid4().hex[:REQUEST_ID_LEN]


# Request id -> only its letters/digits, lowercased, first 12. Older 32-character ids are cut
# to 12; an empty id gets random hex so the name still never collides by accident.
def request_token(request_id) -> str:
    token = re.sub(r"[^a-z0-9]", "", str(request_id or "").lower())[:REQUEST_ID_LEN]
    return token or uuid.uuid4().hex[:REQUEST_ID_LEN]


# Assembles one candidate name. The fixed tail (_date_request[_suffix]) is built first; the
# description gets whatever room is left under 64 characters. Always validated before return.
def build_view_name(description, day, request_id, suffix: str | None = None) -> str:
    if isinstance(day, (date, datetime)):
        day_part = day.strftime("%Y%m%d")
    else:
        # A string like "2026-10-27": keep only the digits (\D = any non-digit) -> "20261027".
        day_part = re.sub(r"\D", "", str(day))[:8]
    tail = f"_{day_part}_{request_token(request_id)}"
    clean_suffix = re.sub(r"[^a-z0-9]", "", str(suffix or "").lower())[:12]
    if clean_suffix:
        tail += f"_{clean_suffix}"
    room = MAX_VIEW_NAME_LEN - len("vw_") - len(tail)
    return validate_view_name(f"vw_{slugify(description, max_len=room, fallback='report')}{tail}")


def unique_view_name(description, day, request_id, taken) -> str:
    """The plain name if it is free, otherwise the same name plus a random suffix.

    `taken(name) -> bool` reports names already used (this session's reports, the database)."""
    # 1st choice: the plain name (already unique per request, since request_id is the PK).
    candidate = build_view_name(description, day, request_id)
    if not taken(candidate):
        return candidate
    # 2nd: add 4, then 6, then 8 random hex characters.
    # Last resort: 8 random hex characters, returned without checking `taken`.
    for length in (4, 6, 8):
        candidate = build_view_name(description, day, request_id, suffix=uuid.uuid4().hex[:length])
        if not taken(candidate):
            return candidate
    return build_view_name(description, day, request_id, suffix=uuid.uuid4().hex[:8])
