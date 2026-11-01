"""View naming convention:  vw_<description>_<YYYYMMDD>_<session>[_<suffix>]

    vw_refunds_by_reason_20261027_f5ba1970c8cf

* description - short, from the report title (lowercase letters, digits, underscores only)
* YYYYMMDD    - the request's day_id (application time zone)
* session     - the first 12 letters/digits of the session id
* suffix      - only added when the plain name is already taken (first 4+ characters of request_id)

MySQL limits identifiers to 64 characters, so the description is shortened first; the date,
session and suffix are always kept. Names are validated before they are ever put in SQL.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime

from .util import slugify

MAX_VIEW_NAME_LEN = 64
SESSION_TOKEN_LEN = 12
VIEW_NAME_RE = re.compile(r"^vw_[a-z0-9_]+$")


class InvalidViewName(ValueError):
    pass


def validate_view_name(name) -> str:
    """Return `name` if it is safe to use as a view identifier, else raise."""
    if not isinstance(name, str) or len(name) > MAX_VIEW_NAME_LEN or not VIEW_NAME_RE.fullmatch(name):
        raise InvalidViewName("Unsafe view name: must be vw_ + lowercase letters, digits and underscores "
                              f"(max {MAX_VIEW_NAME_LEN} characters).")
    return name


def session_token(session_id) -> str:
    token = re.sub(r"[^a-z0-9]", "", str(session_id or "").lower())[:SESSION_TOKEN_LEN]
    return token or "nosession"


def build_view_name(description, day, session_id, suffix: str | None = None) -> str:
    if isinstance(day, (date, datetime)):
        day_part = day.strftime("%Y%m%d")
    else:
        day_part = re.sub(r"\D", "", str(day))[:8]
    tail = f"_{day_part}_{session_token(session_id)}"
    clean_suffix = re.sub(r"[^a-z0-9]", "", str(suffix or "").lower())[:12]
    if clean_suffix:
        tail += f"_{clean_suffix}"
    room = MAX_VIEW_NAME_LEN - len("vw_") - len(tail)
    return validate_view_name(f"vw_{slugify(description, max_len=room, fallback='report')}{tail}")


def unique_view_name(description, day, session_id, request_id, taken) -> str:
    """The plain name if it is free, otherwise the same name plus a request-derived suffix.

    `taken(name) -> bool` reports names already used (this session's reports, the database)."""
    candidate = build_view_name(description, day, session_id)
    if not taken(candidate):
        return candidate
    rid = re.sub(r"[^a-z0-9]", "", str(request_id or "").lower())
    for length in (4, 6, 8):
        candidate = build_view_name(description, day, session_id, suffix=rid[:length] or uuid.uuid4().hex[:length])
        if not taken(candidate):
            return candidate
    return build_view_name(description, day, session_id, suffix=uuid.uuid4().hex[:8])
