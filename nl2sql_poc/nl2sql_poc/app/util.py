"""Small shared helpers (JSON-safe conversion, number formatting)."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


def json_safe(value):
    """Convert DB values (Decimal, date, bytes, ...) into JSON-serialisable Python values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (tuple, list)):
        return [json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    return str(value)


def is_number(value) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def fmt_number(value, column: str = "") -> str:
    """Human-friendly number: $ for *_usd columns, thousands separators otherwise."""
    number = float(value)
    if "usd" in column.lower():
        return f"${number:,.2f}"
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.2f}"


def humanize(column: str) -> str:
    """revenue_usd -> 'revenue', month_start -> 'month start'."""
    text = column.replace("_", " ").strip()
    if text.lower().endswith(" usd"):
        text = text[:-4]
    return text or column


def slugify(text, max_len: int = 40, fallback: str = "report") -> str:
    """'Refunds by Reason - 2026' -> 'refunds_by_reason_2026' (ASCII, snake_case, cut at a word)."""
    import re
    import unicodedata
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    if len(slug) > max_len:
        cut = slug[:max_len]
        if slug[max_len] != "_":
            cut = cut.rsplit("_", 1)[0] or cut      # drop a half-cut last word
        slug = cut.strip("_")
    return slug or fallback
