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
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope


class GuardrailError(ValueError):
    """The SQL was rejected. The message is safe to show to the user / send back to the model."""


# Node types that must never appear anywhere in the parsed tree. Built with getattr so the
# module keeps working across sqlglot versions that rename a class.
_FORBIDDEN_NAMES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Merge", "Command",
    "TruncateTable", "Copy", "Grant", "Revoke", "Set", "Use", "Pragma", "Transaction",
    "Commit", "Rollback", "LoadData", "Kill", "Analyze",
]
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# Set operations (UNION / INTERSECT / EXCEPT). Newer sqlglot: SetOperation; older: Union.
_SET_OP = getattr(exp, "SetOperation", exp.Union)

BLOCKED_FUNCTION_PREFIXES = (
    "pg_", "lo_", "dblink", "set_config", "query_to_xml", "table_to_xml",
    "cursor_to_xml", "database_to_xml", "schema_to_xml", "xpath",
)


@dataclass
class Validated:
    sql: str                 # normalised SQL in the target dialect, with LIMIT enforced
    tables: list             # base tables the query reads


def tables_for_role(allowed_tables, role, access=None) -> set:
    """Lower-cased subset of allowed_tables the role may read. Unknown role or no access
    controller -> empty set (fail closed)."""
    if not role or access is None or not access.normalize_role(role):
        return set()
    return {t.lower() for t in allowed_tables if access.can_access_table(role, t)}


def validate_sql(
    sql: str,
    dialect: str,
    allowed_tables,
    max_rows: int,
    role: str = None,
    access=None,
) -> Validated:
    """Return normalized, limit-capped SQL after validating access."""
    if not role or access is None or not access.normalize_role(role):
        raise GuardrailError(
            "Your role is not authorized to access this information."
        )
    allowed = {t.lower() for t in allowed_tables}

    try:
        statements = [
            s for s in sqlglot.parse(sql, read=dialect)
            if s is not None
        ]
    except sqlglot.errors.ParseError as e:
        raise GuardrailError(
            f"SQL could not be parsed: {str(e).splitlines()[0]}"
        )

    if len(statements) != 1:
        raise GuardrailError("Exactly one SQL statement is allowed.")

    tree = statements[0]

    if not isinstance(tree, (exp.Select, _SET_OP)):
        raise GuardrailError("Only SELECT queries are allowed.")

    bad = next(tree.find_all(*FORBIDDEN_NODES), None) if FORBIDDEN_NODES else None
    if bad is not None:
        raise GuardrailError(
            f"Statement type not allowed: {type(bad).__name__.upper()}."
        )

    for node in tree.find_all(exp.Select):
        if node.args.get("into"):
            raise GuardrailError("SELECT ... INTO is not allowed.")
        if node.args.get("locks"):
            raise GuardrailError(
                "Row-locking clauses (FOR UPDATE / FOR SHARE) are not allowed."
            )

    cte_names = {
        c.alias_or_name.lower() for c in tree.find_all(exp.CTE)
    }

    tables = []
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Func):
            raise GuardrailError("Table-valued functions are not allowed.")

        name = t.name.lower()

        if not t.db and name in cte_names:
            continue

        if t.db and t.db.lower() not in ("public",):
            raise GuardrailError(
                f"Schema-qualified table '{t.db}.{t.name}' is not allowed."
            )

        # A table role_permissions denies to this role is a denial, not a typo to retry.
        if access.table_denied(role, name):
            raise GuardrailError(
                "Your role is not authorized to access this information."
            )

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

    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower().startswith(BLOCKED_FUNCTION_PREFIXES):
            raise GuardrailError(
                f"Function '{fn.name}' is not allowed."
            )

    def resolve(scope, alias):
        # A correlated subquery may name a table of an enclosing query.
        while scope is not None:
            if alias in scope.sources:
                return scope.sources[alias]
            scope = scope.parent
        return None

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
            own = [
                c for c in scope.expression.find_all(exp.Column)
                if c.find_ancestor(exp.Select, _SET_OP) is scope.expression
            ]
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
    limit = tree.args.get("limit")

    if limit is None:
        tree = tree.limit(max_rows)
    else:
        try:
            n = int(limit.expression.name)
        except (ValueError, AttributeError, TypeError):
            n = None

        if n is None or n > max_rows:
            tree = tree.limit(max_rows)

    return Validated(
        sql=tree.sql(dialect=dialect),
        tables=sorted(set(tables)),
    )
