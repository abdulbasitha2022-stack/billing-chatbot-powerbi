
"""Database-backed role permissions for the Billing AI Assistant.

The role_permissions table is the only source of truth. Each row is
(role, resource_type, resource_name, allowed) where resource_type is
  'table'  -> resource_name is a table name
  'column' -> resource_name is 'table.column' (or 'table.*' for every column of the table)
A role may read a table only with an explicit allowed table row; anything missing is denied
(fail closed). A column of a permitted table inherits the table grant unless a column row
('table.column', else 'table.*') says otherwise.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# What it does: answers "may role X read table T / column C?" using rules stored in the MySQL
#   table role_permissions. The rules are kept in a Python dict and re-read from MySQL when
#   they are older than access.refresh_seconds (config, default 60), so new or changed rows
#   (e.g. a newly confidential column) apply without restarting the backend.
# Who calls it: chatbot_api.py creates the AccessController (AccessController.from_engine) when
#   the service starts. guardrails.py asks it about every table/column in a query; pipeline.py
#   uses it to hide denied tables/columns from the LLM prompt and to refuse requests for them
#   early (Pipeline.restricted_requested, via denied_resources).
# What it calls: SQLAlchemy (only to run one SELECT on role_permissions).
# Note: a NEW table or column must also exist in db.allowed_tables / the live schema, which is
#   discovered at startup; only the permission rules themselves are refreshed while running.
# ----------------------------------------------------------------------------------------------

import logging
import threading
import time

# SQLAlchemy is a database toolkit. `text()` wraps a plain SQL string so SQLAlchemy can run it.
from sqlalchemy import text

log = logging.getLogger(__name__)   # "app.access" -> logs/backend_billing.log


# Converts the `allowed` column of role_permissions into a real True/False. Different MySQL
# column types come back as different Python types (bool, int, bytes for BIT(1), str), so each
# one is handled. Unknown values become False, i.e. "deny" - the safe choice.
def _allowed_flag(value):
    """True / False from the stored flag. Anything unrecognised counts as a denial."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, (bytes, bytearray)):            # MySQL BIT(1)
        return value in (b"\x01", b"1")
    return str(value).strip().lower() in ("1", "true")


# Holds every permission rule in memory and answers permission questions quickly.
class AccessController:
    # Accepted spellings of each role (compared case-insensitively) -> the canonical role name.
    # Any role not listed here is "unknown" and gets no access at all.
    ROLE_ALIASES = {
        "ceo": "ceo", "chief executive officer": "ceo",
        "developer": "developer", "software developer": "developer",
        "analyst": "analyst", "finance analyst": "analyst",
    }

    # Input: an iterable of rows (dict-like) from role_permissions.
    # Builds self.permissions: {(role, "table"|"column", name): True/False}.
    # Bad rows are skipped (counted, then logged once) instead of crashing startup.
    def __init__(self, permissions):
        self.permissions = self._parse(permissions)
        # Set by from_engine(): where to re-read the rules from, and how often.
        self._engine, self._refresh_seconds, self._loaded_at = None, None, time.monotonic()
        self._refresh_lock = threading.Lock()

    # Turns role_permissions rows into {(role, "table"|"column", name): True/False}.
    @classmethod
    def _parse(cls, permissions):
        rules = {}
        skipped = 0
        for row in permissions:
            # KeyError = a column is missing from the row; TypeError = the row is not dict-like.
            try:
                role = cls.normalize_role(row["role"])
                kind = str(row["resource_type"]).strip().lower()
                name = str(row["resource_name"]).strip().lower()
                allowed = _allowed_flag(row["allowed"])
            except (KeyError, TypeError):
                skipped += 1
                continue                                  # unusable row: grants nothing
            if role is None or kind not in ("table", "column") or not name:
                skipped += 1
                continue
            # A column rule must look exactly like "table.column" (one dot).
            if kind == "column" and name.count(".") != 1:
                skipped += 1
                continue
            key = (role, kind, name)
            # Conflicting duplicates (e.g. a role spelled two ways): the denial wins.
            rules[key] = allowed and rules.get(key, True)
        if skipped:
            log.warning("Ignored %d unusable role_permissions row(s) (unknown role, type or name)", skipped)
        return rules

    # Reads every row of role_permissions through the SQLAlchemy engine of the read-only query
    # connection (the billing_readonly account from config db.url).
    @staticmethod
    def _load_rows(engine):
        with engine.connect() as conn:
            return conn.execute(
                text("""
                    SELECT role, resource_type, resource_name, allowed
                    FROM role_permissions
                """)
            ).mappings().all()          # .mappings() -> each row behaves like a dict

    # Alternative constructor: loads the rules from MySQL and remembers the engine, so the rules
    # are re-read every `refresh_seconds` (see refresh_if_stale). Called once by chatbot_api.py.
    @classmethod
    def from_engine(cls, engine, refresh_seconds=60):
        """Load permission rules from MySQL."""
        # If the table is missing or the DB is down, log the full traceback and re-raise:
        # starting without permissions would be unsafe, so startup must fail loudly.
        try:
            rows = cls._load_rows(engine)
        except Exception:
            log.exception("Could not load role_permissions")
            raise

        controller = cls(rows)
        controller._engine, controller._refresh_seconds = engine, max(0.0, float(refresh_seconds))
        log.info("Loaded %d role permission rule(s) for %d role(s); re-read every %g s",
                 len(controller.permissions), len({role for role, _, _ in controller.permissions}),
                 controller._refresh_seconds)
        return controller

    # Re-read role_permissions when the loaded rules are older than refresh_seconds. Called at
    # the start of every permission question, so a change in the table applies to the next
    # request. If MySQL cannot be reached the previous rules stay in force (logged); the rules
    # are replaced in one assignment, so a question never sees a half-loaded set.
    def refresh_if_stale(self):
        if self._engine is None or time.monotonic() - self._loaded_at < self._refresh_seconds:
            return
        with self._refresh_lock:
            if time.monotonic() - self._loaded_at < self._refresh_seconds:
                return                        # another thread refreshed while we waited
            try:
                rules = self._parse(self._load_rows(self._engine))
            except Exception:
                log.exception("Could not re-read role_permissions; keeping the previous rules")
            else:
                if rules != self.permissions:
                    log.info("role_permissions changed: now %d rule(s)", len(rules))
                self.permissions = rules
            self._loaded_at = time.monotonic()

    # True only if there is an explicit "allowed" table rule for this role. Used by guardrails.py
    # for every table in a query and by tables_for_role() to build the role's schema.
    def can_access_table(self, role, table_name):
        self.refresh_if_stale()
        role = self.normalize_role(role)
        if not role or not isinstance(table_name, str):
            return False
        # Every role, CEO included, needs an explicit table grant; missing rules are denied.
        return self.permissions.get((role, "table", table_name.strip().lower())) is True

    # Different from "not can_access_table": this is True only for an explicit allowed=0 row.
    # guardrails.py uses it to answer "not authorized" (a real denial) instead of
    # "table not available" (which would invite the LLM to retry with another table).
    def table_denied(self, role, table_name):
        """True when role_permissions explicitly denies this table to the role."""
        self.refresh_if_stale()
        role = self.normalize_role(role)
        return bool(role) and self.permissions.get(
            (role, "table", str(table_name).strip().lower())) is False

    # May the role read this column? column_name "*" means "SELECT * / t.*" on the table.
    # Order of checks: table grant first, then a specific "table.column" rule, then a
    # "table.*" rule, and if neither exists the column simply inherits the table grant.
    def can_access_column(self, role, table_name, column_name):
        if not self.can_access_table(role, table_name):
            return False
        role = self.normalize_role(role)
        table = table_name.strip().lower()
        column = column_name.strip().lower()

        # A wildcard is allowed only when no column of the table is denied to the role.
        if column == "*":
            return not any(
                r == role and kind == "column" and name.startswith(f"{table}.") and not allowed
                for (r, kind, name), allowed in self.permissions.items())

        # Explicit column rule, then the table-wide column rule, then the table grant.
        rule = self.permissions.get((role, "column", f"{table}.{column}"))
        if rule is None:
            rule = self.permissions.get((role, "column", f"{table}.*"))
        return True if rule is None else rule

    # Filters a list of column names down to the readable ones (a convenience helper).
    def permitted_columns(self, role, table_name, columns):
        """The subset of the discovered columns of a table that the role may read."""
        return [c for c in columns if self.can_access_column(role, table_name, c)]

    # Lists what this role may NOT read: a set of denied table names, and a set of denied
    # "table.column" names (columns of already-denied tables are left out, the table covers them).
    # Used by pipeline.py (restricted_note, restricted_requested).
    def denied_resources(self, role, allowed_tables, columns=None):
        """(tables, "table.column" names) of the configured data this role may not read.
        columns: optional {table: [column, ...]} discovered from the database."""
        self.refresh_if_stale()
        tables = {t.strip().lower() for t in allowed_tables if not self.can_access_table(role, t)}
        norm = self.normalize_role(role)
        denied = {name for (r, kind, name), allowed in self.permissions.items()
                  if r == norm and kind == "column" and not allowed and not name.endswith(".*")}
        for table, cols in (columns or {}).items():
            if table.lower() not in tables:
                denied |= {f"{table.lower()}.{c.lower()}" for c in cols
                           if not self.can_access_column(role, table, c)}
        return tables, {c for c in denied if c.split(".", 1)[0] not in tables}

    # "Finance Analyst" / " CEO " -> "analyst" / "ceo"; anything unrecognised -> None.
    # casefold() is a stronger lower() for case-insensitive comparison.
    @classmethod
    def normalize_role(cls, role):
        if not isinstance(role, str) or not role.strip():
            return None
        return cls.ROLE_ALIASES.get(role.strip().casefold())
