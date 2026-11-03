"""Tells the user what date range the data covers, for requests that match no rows.

Instead of building a blank Power BI page for "revenue in 2022" (when the data starts in 2024),
the assistant says so and offers the years that do have data.

Only fixed statements are run, built from table/column names in the config (never from user
text), through the same read-only database connection as every other query.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# Who calls it: conversation.py creates a DataCoverage (from_config) and, when a report query
#   returns no rows, calls no_data_message() to build the reply text and quick-reply chips.
# What it calls: the DBProvider from db.py (execute_readonly, billing_readonly account).
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime

log = logging.getLogger("chatbot.coverage")   # -> logs/backend_billing.log

# Where to look for "how far back does the data go". Override with chat.coverage_sources in
# config_billing.yaml: [{table: charges, column: created_at, label: charges}, ...]
DEFAULT_SOURCES = [
    {"table": "charges", "column": "created_at", "label": "charges"},
    {"table": "invoices", "column": "created_at", "label": "invoices"},
    {"table": "refunds", "column": "created_at", "label": "refunds"},
]
# A plain SQL identifier: starts with a letter/underscore, then letters/digits/underscores.
# Table/column names from config are checked with this before being put into SQL text, because
# identifiers cannot be passed as query parameters.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# Converts whatever the database returned (datetime, date, string, None) into a date or None.
def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


# Looks up (and caches) the first/last date of data in a few tables.
class DataCoverage:
    # db: the DBProvider; sources: list of {table, column, label}; unsafe names are dropped.
    # cache_seconds: how long a looked-up range is reused before asking the database again.
    def __init__(self, db, sources=None, cache_seconds: int = 600):
        self.db = db
        self.sources = [s for s in (sources or DEFAULT_SOURCES)
                        if _IDENT.match(str(s.get("table", ""))) and _IDENT.match(str(s.get("column", "")))]
        self.cache_seconds = cache_seconds
        self._cache: dict = {}   # (table, column) -> (time looked up, range dict or None)

    # Builds a DataCoverage using config chat.coverage_sources if present, else DEFAULT_SOURCES.
    @classmethod
    def from_config(cls, db, cfg: dict) -> "DataCoverage":
        chat_cfg = cfg.get("chat", {}) or {}
        return cls(db, chat_cfg.get("coverage_sources"))

    # Returns {"label", "first", "last"} for one source, or None if unknown. Uses the cache
    # when the stored answer is younger than cache_seconds. Note that failures (None) are cached
    # too, so a broken source is not re-queried on every message.
    def _range(self, source: dict):
        key = (source["table"], source["column"])
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self.cache_seconds:
            return cached[1]
        # "<= now" because the sample data can contain future-dated rows (see the data dictionary).
        sql = (f"SELECT MIN({source['column']}) AS first_date, MAX({source['column']}) AS last_date "
               f"FROM {source['table']} WHERE {source['column']} <= CURRENT_TIMESTAMP")
        found = None
        # READ-ONLY query through db.py. This SQL is built by us (not by the LLM), so it does not
        # go through guardrails.py; it is safe because the names were checked with _IDENT.
        try:
            result = self.db.execute_readonly(sql, 1)
            if result.rows:
                first, last = _to_date(result.rows[0][0]), _to_date(result.rows[0][1])
                if first and last:
                    found = {"label": source.get("label") or source["table"], "first": first, "last": last}
        except Exception as exc:                 # never let this break a chat turn
            log.warning("Could not read the date range of %s: %s", source["table"], exc)
        self._cache[key] = (time.time(), found)
        return found

    def for_sql(self, sql: str | None) -> list:
        """Date ranges for the tables the query used (all known tables if none matched)."""
        # \b...\b = whole-word match of the table name inside the SQL text, case-insensitive (re.I).
        used = [s for s in self.sources
                if sql and re.search(rf"\b{re.escape(s['table'])}\b", sql, re.I)] or self.sources
        return [r for r in (self._range(s) for s in used) if r]


# date(2024, 3, 5) -> "Mar 2024"
def _month(d: date) -> str:
    return f"{d:%b %Y}"


# One sentence describing the ranges. If all tables share the same range it is said once,
# otherwise each table is listed with its own range.
def coverage_sentence(ranges: list) -> str:
    if not ranges:
        return ""
    if len({(r["first"], r["last"]) for r in ranges}) == 1:
        return f"The data I have covers {_month(ranges[0]['first'])} to {_month(ranges[0]['last'])}."
    parts = "; ".join(f"{r['label']}: {_month(r['first'])} to {_month(r['last'])}" for r in ranges)
    return f"The data I have covers {parts}."


def no_data_message(coverage: DataCoverage | None, sql: str | None):
    """(reply text, quick-reply chips) for a request whose query matched no rows."""
    ranges = coverage.for_sql(sql) if coverage else []
    text = "I ran your request, but it returned no rows, so I didn't create a page."
    sentence = coverage_sentence(ranges)
    if sentence:
        text += " " + sentence
    text += " Try a period inside that range, or fewer filters."
    # Chips = clickable suggestion buttons in the visual: one per year with data, at most the
    # last four years.
    chips = []
    if ranges:
        first_year = min(r["first"].year for r in ranges)
        last_year = max(r["last"].year for r in ranges)
        for year in range(max(first_year, last_year - 3), last_year + 1):
            chips.append({"label": f"Try {year}", "message": f"Same request, but for {year}"})
    return text, chips
