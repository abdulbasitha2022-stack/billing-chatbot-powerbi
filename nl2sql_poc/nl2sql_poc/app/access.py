
"""Database-backed role permissions for the Billing AI Assistant.

The role_permissions table is the only source of truth. Each row is
(role, resource_type, resource_name, allowed) where resource_type is
  'table'  -> resource_name is a table name
  'column' -> resource_name is 'table.column' (or 'table.*' for every column of the table)
A role may read a table only with an explicit allowed table row; anything missing is denied
(fail closed). A column of a permitted table inherits the table grant unless a column row
('table.column', else 'table.*') says otherwise.
"""

import re

from sqlalchemy import text


def _allowed_flag(value):
    """True / False from the stored flag. Anything unrecognised counts as a denial."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value == 1
    if isinstance(value, (bytes, bytearray)):            # MySQL BIT(1)
        return value in (b"\x01", b"1")
    return str(value).strip().lower() in ("1", "true")


class AccessController:
    ROLE_ALIASES = {
        "ceo": "ceo", "chief executive officer": "ceo",
        "developer": "developer", "software developer": "developer",
        "analyst": "analyst", "finance analyst": "analyst",
    }

    def __init__(self, permissions):
        self.permissions = {}
        for row in permissions:
            try:
                role = self.normalize_role(row["role"])
                kind = str(row["resource_type"]).strip().lower()
                name = str(row["resource_name"]).strip().lower()
                allowed = _allowed_flag(row["allowed"])
            except (KeyError, TypeError):
                continue                                  # unusable row: grants nothing
            if role is None or kind not in ("table", "column") or not name:
                continue
            if kind == "column" and name.count(".") != 1:
                continue
            key = (role, kind, name)
            # Conflicting duplicates (e.g. a role spelled two ways): the denial wins.
            self.permissions[key] = allowed and self.permissions.get(key, True)

    @classmethod
    def from_engine(cls, engine):
        """Load permission rules from MySQL."""
        with engine.connect() as conn:
            rows = conn.execute(
                text("""
                    SELECT role, resource_type, resource_name, allowed
                    FROM role_permissions
                """)
            ).mappings().all()

        return cls(rows)

    def can_access_table(self, role, table_name):
        role = self.normalize_role(role)
        if not role or not isinstance(table_name, str):
            return False
        # Every role, CEO included, needs an explicit table grant; missing rules are denied.
        return self.permissions.get((role, "table", table_name.strip().lower())) is True

    def table_denied(self, role, table_name):
        """True when role_permissions explicitly denies this table to the role."""
        role = self.normalize_role(role)
        return bool(role) and self.permissions.get(
            (role, "table", str(table_name).strip().lower())) is False

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

    def permitted_columns(self, role, table_name, columns):
        """The subset of the discovered columns of a table that the role may read."""
        return [c for c in columns if self.can_access_column(role, table_name, c)]

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
        terms = {str(k).strip().lower(): v for k, v in (terms or {}).items()}
        for resource in sorted(tables) + sorted(denied_columns):
            table, _, column = resource.partition(".")
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
            for term in found:
                if re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?:s|es)?(?![a-z0-9_])", haystack):
                    return resource
        return None

    @classmethod
    def normalize_role(cls, role):
        if not isinstance(role, str) or not role.strip():
            return None
        return cls.ROLE_ALIASES.get(role.strip().casefold())
