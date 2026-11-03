
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
#   table role_permissions. The rules are loaded ONCE, at startup, into a Python dict.
# Who calls it: chatbot_api.py creates the AccessController (AccessController.from_engine) when
#   the service starts. guardrails.py asks it about every table/column in a query; pipeline.py
#   uses it to hide denied tables/columns from the LLM prompt and to refuse requests early
#   (denied_reference) before any AI call is made.
# What it calls: SQLAlchemy (only to run one SELECT on role_permissions) and Python's `re`.
# Note: rules are cached in memory, so editing role_permissions needs a backend restart.
# ----------------------------------------------------------------------------------------------

import logging
import re

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
        self.permissions = {}
        skipped = 0
        for row in permissions:
            # KeyError = a column is missing from the row; TypeError = the row is not dict-like.
            try:
                role = self.normalize_role(row["role"])
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
            self.permissions[key] = allowed and self.permissions.get(key, True)
        if skipped:
            log.warning("Ignored %d unusable role_permissions row(s) (unknown role, type or name)", skipped)

    # Alternative constructor: reads role_permissions through the SQLAlchemy engine of the
    # read-only query connection (the billing_readonly account from config db.url) and
    # returns a ready AccessController. Called once by chatbot_api.py at startup.
    @classmethod
    def from_engine(cls, engine):
        """Load permission rules from MySQL."""
        # If the table is missing or the DB is down, log the full traceback and re-raise:
        # starting without permissions would be unsafe, so startup must fail loudly.
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text("""
                        SELECT role, resource_type, resource_name, allowed
                        FROM role_permissions
                    """)
                ).mappings().all()          # .mappings() -> each row behaves like a dict
        except Exception:
            log.exception("Could not load role_permissions")
            raise

        controller = cls(rows)
        log.info("Loaded %d role permission rule(s) for %d role(s)", len(controller.permissions),
                 len({role for role, _, _ in controller.permissions}))
        return controller

    # True only if there is an explicit "allowed" table rule for this role. Used by guardrails.py
    # for every table in a query and by tables_for_role() to build the role's schema.
    def can_access_table(self, role, table_name):
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
    # Used by denied_reference() below.
    def denied_resources(self, role, allowed_tables, columns=None):
        """(tables, "table.column" names) of the configured data this role may not read.
        columns: optional {table: [column, ...]} discovered from the database."""
        tables = {t.strip().lower() for t in allowed_tables if not self.can_access_table(role, t)}
        norm = self.normalize_role(role)
        denied = {name for (r, kind, name), allowed in self.permissions.items()
                  if r == norm and kind == "column" and not allowed and not name.endswith(".*")}
        for table, cols in (columns or {}).items():
            if table.lower() not in tables:
                denied |= {f"{table.lower()}.{c.lower()}" for c in cols
                           if not self.can_access_column(role, table, c)}
        return tables, {c for c in denied if c.split(".", 1)[0] not in tables}

    # EARLY REFUSAL. Scans the user's words (texts) for the name of anything this role may not
    # read, so the chatbot can say "not authorized" straight away - without sending the question
    # to the LLM at all. Called via pipeline.denied_reference() from conversation.py and pipeline.py.
    # Returns the denied resource name (e.g. "customers.email") or None.
    # It is a keyword match, so it is a convenience; the real enforcement is still guardrails.py.
    def denied_reference(self, role, allowed_tables, *texts, columns=None, terms=None):
        """The first denied table/column the texts ask for, else None. Runs before any LLM call.
        columns: discovered schema {table: [column, ...]}; terms: optional everyday words for a
        resource ({"table" or "table.column": [phrase, ...]}) from configuration."""
        haystack = " ".join(t for t in texts if t).lower()
        if not haystack.strip():
            return None
        columns = {t.lower(): [c.lower() for c in cols] for t, cols in (columns or {}).items()}
        tables, denied_columns = self.denied_resources(role, allowed_tables, columns)
        # Column names the role may read somewhere; a bare name shared with one of them is too
        # ambiguous to count as a request for the denied one.
        readable = {c for t, cols in columns.items() if t not in tables
                    for c in cols if self.can_access_column(role, t, c)}
        # terms = config access.resource_terms: extra everyday words for a resource.
        terms = {str(k).strip().lower(): v for k, v in (terms or {}).items()}
        for resource in sorted(tables) + sorted(denied_columns):
            table, _, column = resource.partition(".")   # "customers.email" -> ("customers", "email")
            # `found` = every phrase that should count as asking for this resource, e.g.
            # "customers.email", "customers email", "customer email", "email" (if unambiguous).
            found = {resource, resource.replace("_", " ")}
            if column:
                found |= {f"{t} {column}".replace("_", " ") for t in (table, table.rstrip("s"))}
                if column not in readable and ("_" in column or column in columns.get(table, ())):
                    found |= {column, column.replace("_", " ")}
            else:
                found.add(table.replace("_", " ").rstrip("s"))
                found |= {w for c in columns.get(table, ()) if "_" in c and c not in readable
                          for w in (c, c.replace("_", " "))}
            found |= {str(t).strip().lower() for t in terms.get(resource, ()) if str(t).strip()}
            # Regex: the term as a whole word, optionally followed by "s"/"es" (plurals).
            # (?<![a-z0-9_]) and (?![a-z0-9_]) mean "no letter/digit/underscore right before or
            # after", so "email" does not match inside "emails_sent_count". re.escape makes any
            # special characters in the term literal.
            for term in found:
                if re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?:s|es)?(?![a-z0-9_])", haystack):
                    log.warning("Access denied before any AI call: role %s asked for %s (matched %r)",
                                role, resource, term)
                    return resource
        return None

    # "Finance Analyst" / " CEO " -> "analyst" / "ceo"; anything unrecognised -> None.
    # casefold() is a stronger lower() for case-insensitive comparison.
    @classmethod
    def normalize_role(cls, role):
        if not isinstance(role, str) or not role.strip():
            return None
        return cls.ROLE_ALIASES.get(role.strip().casefold())
