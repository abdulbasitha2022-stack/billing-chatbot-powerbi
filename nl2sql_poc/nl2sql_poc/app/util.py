"""Small shared helpers (JSON-safe conversion, number formatting)."""
# FILE OVERVIEW (for learners): tiny, dependency-free helpers used across the backend, e.g.
#   json_safe by audit.py / chatbot_api.py / conversation.py, slugify by naming.py, and
#   fmt_number / humanize / is_number by conversation.py when replies describe report results.
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal


def json_safe(value):
    """Convert DB values (Decimal, date, bytes, ...) into JSON-serialisable Python values."""
    # json.dumps() cannot handle Decimal, date or bytes, which MySQL drivers return. This walks
    # lists/dicts recursively and converts each value; anything unknown becomes its str().
    # Note: Decimal -> float can lose tiny precision, which is fine for display.
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


# bool is excluded on purpose: in Python True/False are also ints, but they are not numbers here.
def is_number(value) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def fmt_number(value, column: str = "") -> str:
    """Human-friendly number: $ for *_usd columns, thousands separators otherwise."""
    number = float(value)
    # Note: this checks for "usd" anywhere in the name, not only at the end.
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
    # NFKD splits accented letters into letter + accent ("é" -> "e" + "´"); encoding to ASCII
    # with "ignore" then drops the accents and any other non-ASCII character.
    ascii_text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    # Every run of characters that is not a-z / 0-9 becomes a single "_".
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    if len(slug) > max_len:
        cut = slug[:max_len]
        if slug[max_len] != "_":
            cut = cut.rsplit("_", 1)[0] or cut      # drop a half-cut last word
        slug = cut.strip("_")
    return slug or fallback
