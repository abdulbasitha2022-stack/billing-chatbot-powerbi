"""
guardrails.py - the last line of defence between an LLM-written query and the database.

The model is asked to write read-only SQL, but we never rely on that. Every query is
parsed with sqlglot and must pass ALL of these checks before it is executed:

  1. exactly one statement
  2. the statement is a SELECT (or a UNION/INTERSECT/EXCEPT of SELECTs), CTEs allowed
  3. no write/DDL/admin node anywhere in the tree (catches "WITH x AS (DELETE ...)")
  4. no SELECT ... INTO, no FOR UPDATE / FOR SHARE locks
  5. only whitelisted tables (CTE names excluded); no table-valued functions
  6. no dangerous functions (pg_*, lo_*, dblink*, set_config, ...)
  7. a LIMIT is always present and never larger than max_rows
  8. the role may read every table and every column the query touches, in any clause or
     scope (role_permissions via AccessController); SELECT * only when no column is denied

The database role should ALSO be read-only (see schema.sql: poc_readonly). Two independent
layers is the point.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# What it does: checks one piece of SQL written by the LLM and either returns a safe,
#   normalised version of it (Validated) or raises GuardrailError explaining why it was refused.
# Who calls it:  app/pipeline.py (validate_sql + tables_for_role, right after the LLM writes SQL)
#   and chatbot_api.py (validate_sql again, just before the SQL is turned into a MySQL view,
#   and for the test-only "mock_sql" path).
# What it calls: the sqlglot library and the AccessController in app/access.py (role permissions).
#
# About sqlglot: it is a pure-Python SQL parser. Instead of looking at SQL as plain text (which
#   is easy to trick with comments, odd spacing or nesting), sqlglot turns it into a TREE of
#   Python objects - exp.Select, exp.Table, exp.Column, exp.Delete, ... We can then walk that
#   tree and ask precise questions such as "is there any DELETE node anywhere?" or "which
#   tables are read?". It can also print the tree back out as SQL for a chosen dialect (MySQL).
# In this project the database account used to run the query (billing_readonly, see
#   config_billing.yaml -> db.url) is the second, independent layer of protection.
# ----------------------------------------------------------------------------------------------
import logging
from dataclasses import dataclass

import sqlglot                                   # SQL parser: text -> tree of expression objects
from sqlglot import exp                          # the node classes (exp.Select, exp.Table, ...)
from sqlglot.optimizer.scope import traverse_scope  # walks each SELECT "scope" (main query,
                                                    # every subquery and CTE) one at a time

# Logger named "app.guardrails"; its lines end up in logs/backend_billing.log (see logging_setup.py).
log = logging.getLogger(__name__)


# Raised for every refusal. Because it subclasses ValueError, callers can catch it specifically.
# pipeline.py catches it: a "not authorized" message ends the request with a denial, any other
# message is sent back to the LLM so it can fix the SQL and try again. The message never
# contains secrets, so it is also safe to show to the user.
class GuardrailError(ValueError):
    """The SQL was rejected. The message is safe to show to the user / send back to the model."""


# Node types that must never appear anywhere in the parsed tree. Built with getattr so the
# module keeps working across sqlglot versions that rename a class.
_FORBIDDEN_NAMES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Merge", "Command",
    "TruncateTable", "Copy", "Grant", "Revoke", "Set", "Use", "Pragma", "Transaction",
    "Commit", "Rollback", "LoadData", "Kill", "Analyze",
]
# Turn the names above into the real sqlglot classes, silently skipping any that this installed
# sqlglot version does not have. Result: a tuple of classes we can pass to tree.find_all(...).
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# Set operations (UNION / INTERSECT / EXCEPT). Newer sqlglot: SetOperation; older: Union.
_SET_OP = getattr(exp, "SetOperation", exp.Union)

# Function names that can read files, open network connections to other servers or change
# server settings (mostly PostgreSQL ones, kept because the code also supports Postgres).
# Any function whose name STARTS with one of these is refused (rule 6).
BLOCKED_FUNCTION_PREFIXES = (
    "pg_", "lo_", "dblink", "set_config", "query_to_xml", "table_to_xml",
    "cursor_to_xml", "database_to_xml", "schema_to_xml", "xpath",
)


# The successful result of validate_sql(). A @dataclass is just a small class whose fields are
# declared below; Python writes __init__ for us. Callers use .sql to run / create the view.
@dataclass
class Validated:
    sql: str                 # normalised SQL in the target dialect, with LIMIT enforced
    tables: list             # base tables the query reads


# Which of the configured tables (db.allowed_tables) may this role read?
# Input: the configured table list, the user's role ("ceo", "Analyst", ...) and an AccessController.
# Output: a set of lower-case table names. Called by pipeline.py to build the role's schema
# prompt and the table whitelist it passes into validate_sql().
def tables_for_role(allowed_tables, role, access=None) -> set:
    """Lower-cased subset of allowed_tables the role may read. Unknown role or no access
    controller -> empty set (fail closed)."""
    if not role or access is None or not access.normalize_role(role):
        return set()
    return {t.lower() for t in allowed_tables if access.can_access_table(role, t)}


# PUBLIC ENTRY POINT. Thin wrapper around _validate() that only adds logging.
# Inputs: sql (the LLM's text), dialect (sqlglot name, "mysql" here), allowed_tables (whitelist),
#   max_rows (row cap from config db.max_rows), role and access (who is asking + their rules).
# Output: Validated(sql=..., tables=[...]) or a GuardrailError is raised.
def validate_sql(
    sql: str,
    dialect: str,
    allowed_tables,
    max_rows: int,
    role: str = None,
    access=None,
) -> Validated:
    """Return normalized, limit-capped SQL after validating access."""
    try:
        validated = _validate(sql, dialect, allowed_tables, max_rows, role, access)
    except GuardrailError as exc:
        # Record the refusal (WARNING level, so it is always in the log file), then re-raise
        # with a bare `raise` so the caller still receives the original GuardrailError.
        log.warning("SQL blocked by guardrails (role %s): %s", role or "-", exc)
        raise
    # DEBUG lines are only written when config logging.level is DEBUG.
    log.debug("SQL passed guardrails (role %s, tables %s)", role, ", ".join(validated.tables) or "-")
    return validated


# The real work. Each block below is one numbered rule from the module docstring. The first
# rule that fails raises GuardrailError and stops; if all pass we return Validated.
def _validate(sql, dialect, allowed_tables, max_rows, role, access) -> Validated:
    # Fail closed: no role, no access rules, or a role name we do not recognise -> refuse.
    if not role or access is None or not access.normalize_role(role):
        raise GuardrailError(
            "Your role is not authorized to access this information."
        )
    allowed = {t.lower() for t in allowed_tables}   # whitelist, lower-cased for easy comparison

    # Parse the text into sqlglot trees (one per ';'-separated statement). Empty statements
    # (e.g. a trailing ';') come back as None and are dropped. Unparseable SQL raises ParseError,
    # which we convert into a GuardrailError with only the first line of the message.
    try:
        statements = [
            s for s in sqlglot.parse(sql, read=dialect)
            if s is not None
        ]
    except sqlglot.errors.ParseError as e:
        raise GuardrailError(
            f"SQL could not be parsed: {str(e).splitlines()[0]}"
        )

    # Rule 1: one statement only. Stops "SELECT ...; DROP TABLE x" style stacking.
    if len(statements) != 1:
        raise GuardrailError("Exactly one SQL statement is allowed.")

    tree = statements[0]   # the root node of the parsed query

    # Rule 2: the root must be a SELECT or a UNION/INTERSECT/EXCEPT of SELECTs.
    if not isinstance(tree, (exp.Select, _SET_OP)):
        raise GuardrailError("Only SELECT queries are allowed.")

    # Rule 3: search the WHOLE tree (not just the root) for any write/DDL/admin node, so a
    # write hidden inside a CTE or subquery is caught too. next(..., None) = first match or None.
    bad = next(tree.find_all(*FORBIDDEN_NODES), None) if FORBIDDEN_NODES else None
    if bad is not None:
        raise GuardrailError(
            f"Statement type not allowed: {type(bad).__name__.upper()}."
        )

    # Rule 4: SELECT ... INTO writes the result into a table/file/variable, and FOR UPDATE /
    # FOR SHARE take row locks that could block other users - neither belongs in a report.
    for node in tree.find_all(exp.Select):
        if node.args.get("into"):
            raise GuardrailError("SELECT ... INTO is not allowed.")
        if node.args.get("locks"):
            raise GuardrailError(
                "Row-locking clauses (FOR UPDATE / FOR SHARE) are not allowed."
            )

    # Names defined by WITH x AS (...) - they look like tables in the tree but are not real
    # tables, so they are skipped by the whitelist check below.
    cte_names = {
        c.alias_or_name.lower() for c in tree.find_all(exp.CTE)
    }

    # Rule 5 + part of rule 8: every table reference in the query, at any depth.
    tables = []   # real base tables the query reads (returned in Validated.tables)
    for t in tree.find_all(exp.Table):
        # FROM some_function(...) - a function that returns rows could read anything.
        if isinstance(t.this, exp.Func):
            raise GuardrailError("Table-valued functions are not allowed.")

        name = t.name.lower()

        if not t.db and name in cte_names:
            continue

        # t.db is the "schema." / "database." prefix. In MySQL that prefix would be another
        # database (e.g. mysql.user), so any prefix other than "public" is refused.
        if t.db and t.db.lower() not in ("public",):
            raise GuardrailError(
                f"Schema-qualified table '{t.db}.{t.name}' is not allowed."
            )

        # A table role_permissions denies to this role is a denial, not a typo to retry.
        if access.table_denied(role, name):
            raise GuardrailError(
                "Your role is not authorized to access this information."
            )

        # Not on the whitelist (probably a made-up table name): the message lists the allowed
        # tables so the LLM can correct itself on the retry.
        if name not in allowed:
            raise GuardrailError(
                f"Table '{t.name}' is not available. Use only: "
                f"{', '.join(sorted(allowed))}."
            )

        # Enforce role-specific table permissions.
        if role is not None and access is not None:
            if not access.can_access_table(role, name):
                raise GuardrailError(
                    "Your role is not authorized to access this information."
                )

        tables.append(name)

    # Rule 6: sqlglot represents functions it does not know by name as exp.Anonymous.
    # str.startswith accepts a tuple, so this checks every blocked prefix at once.
    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower().startswith(BLOCKED_FUNCTION_PREFIXES):
            raise GuardrailError(
                f"Function '{fn.name}' is not allowed."
            )

    # Helper: find what a table alias (e.g. "c" in "customers c") points to, looking first in
    # the current scope and then in the enclosing scopes. Returns the source node or None.
    def resolve(scope, alias):
        # A correlated subquery may name a table of an enclosing query.
        while scope is not None:
            if alias in scope.sources:
                return scope.sources[alias]
            scope = scope.parent
        return None

    # Helper: a "SELECT *" or "t.*" on this table is only OK if the role may read the table AND
    # no single column of it is denied (otherwise * would leak the denied column).
    def check_star(table):
        if not access.can_access_table(role, table.name):
            raise GuardrailError(
                "Your role is not authorized to access this information."
            )
        if not access.can_access_column(role, table.name, "*"):
            # Not a denial: the model is asked to name the columns it needs.
            raise GuardrailError(
                f"SELECT * is not allowed on table '{table.name}'. "
                "List the needed columns explicitly, using only columns from the schema."
            )

    # Enforce column permissions across SELECT scopes, including
    # subqueries and CTEs.
    # Rule 8 (columns). A "scope" is one SELECT level: the main query, each subquery and each
    # CTE get their own scope. scope.sources maps alias -> what it reads from (a real exp.Table,
    # or another scope for a subquery/CTE). Only real tables are checked against permissions;
    # subqueries/CTEs are checked when their own scope comes round in this loop.
    if role is not None and access is not None:
        for scope in traverse_scope(tree):
            sources = scope.sources

            # JOIN ... USING (col) names columns of both joined tables; NATURAL JOIN
            # compares every shared column implicitly, so it is never allowed.
            for join in scope.expression.args.get("joins") or []:
                if str(join.args.get("method") or "").upper() == "NATURAL":
                    raise GuardrailError(
                        "NATURAL JOIN is not allowed. Join with an explicit ON condition."
                    )
                for ident in join.args.get("using") or []:
                    for source in sources.values():
                        if isinstance(source, exp.Table) and not access.can_access_column(
                            role, source.name, ident.name.lower()
                        ):
                            raise GuardrailError(
                                "Your role is not authorized to access this information."
                            )

            # A wildcard could expose restricted columns, e.g. customers.*.
            # COUNT(*) is not a wildcard projection and is not blocked here.
            for projection in scope.expression.expressions:
                is_wildcard = (
                    isinstance(projection, exp.Star)
                    or (
                        isinstance(projection, exp.Column)
                        and projection.is_star
                    )
                )

                if is_wildcard:
                    # "c.*" -> only table c is affected; a bare "*" -> every table in the scope.
                    source_alias = (
                        projection.table.lower()
                        if isinstance(projection, exp.Column)
                        and projection.table
                        else None
                    )

                    if source_alias:
                        source = sources.get(source_alias)
                        if isinstance(source, exp.Table):
                            check_star(source)
                        elif source is None:
                            raise GuardrailError(
                                "Your role is not authorized to access this information."
                            )
                    else:
                        for source in sources.values():
                            if isinstance(source, exp.Table):
                                check_star(source)

            # scope.columns leaves out some references (e.g. columns used only in HAVING),
            # so also check every column written directly in this scope; nested subqueries
            # and CTEs are checked in their own scopes.
            # "own" = columns whose nearest enclosing SELECT is this scope's SELECT.
            own = [
                c for c in scope.expression.find_all(exp.Column)
                if c.find_ancestor(exp.Select, _SET_OP) is scope.expression
            ]
            # Every column reference, in SELECT, WHERE, GROUP BY, ORDER BY, HAVING, JOIN ON ...
            for column in list(scope.columns) + own:
                column_name = column.name.lower()
                table_alias = column.table.lower() if column.table else None

                if table_alias:
                    source = resolve(scope, table_alias)

                    if source is None:
                        # Unknown qualifier: never executed, the model must fix it.
                        raise GuardrailError(
                            f"Column '{column.sql()}' does not refer to a table in the query."
                        )
                    if isinstance(source, exp.Table):
                        if not access.can_access_column(
                            role, source.name, column_name
                        ):
                            raise GuardrailError(
                                "Your role is not authorized to access "
                                "this information."
                            )

                else:
                    # An unqualified column may resolve to any direct table in
                    # this scope. Check all of them conservatively; this also
                    # avoids relying on SQL aliases or ambiguous join metadata.
                    for source in sources.values():
                        if isinstance(source, exp.Table) and not access.can_access_column(
                            role, source.name, column_name
                        ):
                            raise GuardrailError(
                                "Your role is not authorized to access "
                                "this information."
                            )

    # Add a LIMIT if missing; clamp excessive or nonnumeric limits.
    # Rule 7: this protects the database and Power BI from huge result sets.
    # tree.limit(n) returns a NEW tree with "LIMIT n" set (replacing any existing LIMIT).
    limit = tree.args.get("limit")

    if limit is None:
        tree = tree.limit(max_rows)
    else:
        # Read the number written after LIMIT. If it is not a plain integer (an expression,
        # a parameter, ...) int() or the attribute access fails, and we treat it as "unknown"
        # and replace it with max_rows rather than trusting it.
        try:
            n = int(limit.expression.name)
        except (ValueError, AttributeError, TypeError):
            n = None

        if n is None or n > max_rows:
            tree = tree.limit(max_rows)

    # Print the (possibly modified) tree back out as SQL text in the target dialect. This
    # re-generated text - not the LLM's original string - is what gets executed / saved.
    return Validated(
        sql=tree.sql(dialect=dialect),
        tables=sorted(set(tables)),
    )
