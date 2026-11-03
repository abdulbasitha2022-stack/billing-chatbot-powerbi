"""
db.py - the pluggable database layer.

Same idea as llm.py: the pipeline only sees DBProvider (dialect, describe, execute_readonly).
`db.type` in config.yaml picks the implementation:

  postgres    -> PostgresProvider   (read-only transaction + statement timeout enforced)
  sqlalchemy  -> SQLAlchemyProvider (any other database that has a SQLAlchemy dialect;
                                     just change the URL, e.g. bigquery://project/dataset)

`dialect` is the sqlglot dialect name. It is used by the guardrails to parse the model's SQL and
by the prompt so the model writes the right flavour of SQL.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# What it does: owns the READ-ONLY connection to the billing database. It can (1) describe the
#   tables (column names + types) for the LLM prompt and (2) run an already-validated SELECT.
# Who calls it: build_db() is called by chatbot_api.py (create_app) and run.py. The returned
#   provider is used by pipeline.py (describe, table_columns, execute_readonly), coverage.py
#   (execute_readonly) and chatbot_api.py (its .engine is handed to AccessController).
# What it calls: SQLAlchemy, which in turn uses the pymysql driver for the "mysql+pymysql://" URL.
#
# Libraries in plain words:
#   * SQLAlchemy - a database toolkit. create_engine(url) makes an "engine": a factory that
#     opens connections and keeps a small pool of them for reuse. inspect() reads table
#     metadata. text() wraps a raw SQL string so it can be executed.
#   * pymysql - the pure-Python MySQL driver that actually talks to the MySQL server. You never
#     call it here directly; SQLAlchemy picks it because the URL starts with mysql+pymysql://.
# Which DB account? The one in config db.url / the DATABASE_URL env var - for billing that is
#   the read-only account billing_readonly. Writes (views, metadata) happen elsewhere, in
#   report_store.py, with a different account (view_builder).
# ----------------------------------------------------------------------------------------------
import logging
import os
import re
from abc import ABC, abstractmethod   # ABC = "abstract base class": defines an interface
from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

log = logging.getLogger(__name__)   # "app.db" -> logs/backend_billing.log


# Raised when the database refuses or fails a query (syntax error, unknown column, timeout...).
# pipeline.py catches it and gives the message to the LLM so it can fix the SQL and retry.
class QueryError(RuntimeError):
    """The database rejected or failed the query. Message is safe to feed back to the model."""


# What execute_readonly() returns.
@dataclass
class QueryResult:
    columns: list        # column names, in SELECT order
    rows: list           # list of tuples, at most max_rows of them
    truncated: bool      # True if the database had more rows than max_rows


# The interface every database provider must follow. @abstractmethod means "a subclass MUST
# implement this"; the rest of the code only relies on these methods + `dialect`.
class DBProvider(ABC):
    dialect: str = "unknown"          # sqlglot dialect name

    @abstractmethod
    def describe(self, tables) -> str:
        """Compact schema text (table(col TYPE, ...)) for the prompt, from the live database."""

    @abstractmethod
    def execute_readonly(self, sql: str, max_rows: int) -> QueryResult:
        """Run a validated SELECT. Must never be able to change data."""


# SQLAlchemy dialect name -> sqlglot dialect name
_SQLGLOT_DIALECTS = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql",
                     "bigquery": "bigquery", "snowflake": "snowflake", "mssql": "tsql",
                     "sqlite": "sqlite", "oracle": "oracle"}


# Regex below: a ":" that is NOT preceded by another ":" ((?<!:)) and IS followed by a letter or
# underscore ((?=[A-Za-z_])) gets a backslash in front, which tells text() "this is a literal
# colon, not a :parameter". Example: '10:30' is untouched (digit follows), 'x :a' -> 'x \:a'.
def _escape_colons(sql: str) -> str:
    """SQLAlchemy's text() treats ':name' as a bind parameter. Escape those so a literal like
    'a :b' survives, while leaving '::' casts alone."""
    return re.sub(r"(?<!:):(?=[A-Za-z_])", r"\\:", sql)


# The provider used for billing (config db.type: sqlalchemy). Works with any database that
# SQLAlchemy supports; for MySQL it uses the pymysql driver named in the URL.
class SQLAlchemyProvider(DBProvider):
    # cfg = the `db:` section of config_billing.yaml.
    def __init__(self, cfg: dict):
        # Prefer the environment variable so credentials never live in a config file in git.
        url = os.getenv(cfg.get("url_env", "DATABASE_URL")) or cfg["url"]
        # pool_pre_ping=True: before reusing a pooled connection, check it is still alive
        # (MySQL closes idle connections after a while; without this the first query fails).
        # create_engine does not connect yet - the first real connection happens on first use.
        self.engine = create_engine(url, pool_pre_ping=True)
        # Translate SQLAlchemy's dialect name ("mysql") into sqlglot's name for guardrails.py.
        self.dialect = _SQLGLOT_DIALECTS.get(self.engine.dialect.name, self.engine.dialect.name)
        # Only PostgresProvider below actually applies this timeout.
        self.timeout_ms = int(cfg.get("statement_timeout_ms", 15000))
        # render_as_string(hide_password=True) prints the URL with the password replaced by ***.
        log.info("Database engine ready: %s (dialect %s, from %s)",
                 self.engine.url.render_as_string(hide_password=True), self.dialect,
                 "environment" if os.getenv(cfg.get("url_env", "DATABASE_URL")) else "config")

    # Does nothing here (MySQL path): the read-only protection for MySQL comes from guardrails.py
    # plus the billing_readonly database account, not from a per-connection setting.
    def _prepare(self, conn):
        """Hook: per-connection safety settings. Subclasses override."""

    # Builds the schema text the LLM sees, e.g. "invoices(id INTEGER, amount_usd DECIMAL, ...)".
    # Called once by pipeline.py at startup. A missing table is a setup error, so it is logged
    # and turned into a RuntimeError with a hint on how to fix it (startup then stops).
    def describe(self, tables):
        insp = inspect(self.engine)   # SQLAlchemy inspector: reads table/column metadata
        lines = []
        for t in tables:
            try:
                cols = insp.get_columns(t)
            except NoSuchTableError:
                log.error("Configured table %s does not exist in the database", t)
                raise RuntimeError(f"Table '{t}' (from db.allowed_tables) does not exist in the "
                                   f"database. Did you run generate_sample_data.py --load?")
            lines.append(f"{t}(" + ", ".join(f"{c['name']} {c['type']}" for c in cols) + ")")
        return "\n".join(lines)

    # Same information as describe() but as data, so pipeline.py can show each role only the
    # columns role_permissions allows (see Pipeline.schema_for).
    def table_columns(self, tables):
        """{table: [(column, TYPE), ...]} from the live database, for role-filtered schemas."""
        insp = inspect(self.engine)
        return {t: [(c["name"], str(c["type"])) for c in insp.get_columns(t)] for t in tables}

    # Runs SQL that has ALREADY passed guardrails.validate_sql(). Read-only account, no commit.
    # Called by pipeline.py (the report query) and coverage.py (date-range lookups).
    def execute_readonly(self, sql, max_rows):
        try:
            # `with ... as conn` borrows a connection from the pool and always gives it back,
            # even if an error happens. Nothing is committed, so no change could be saved.
            with self.engine.connect() as conn:
                self._prepare(conn)
                result = conn.execute(text(_escape_colons(sql)))
                columns = list(result.keys())
                # Fetch ONE extra row: if it exists we know the result was cut off (truncated).
                rows = result.fetchmany(max_rows + 1)
                return QueryResult(columns, [tuple(r) for r in rows[:max_rows]],
                                   truncated=len(rows) > max_rows)
        # Every SQLAlchemy/driver failure (bad column, syntax, lost connection, ...) lands here.
        # e.orig is the original pymysql error; only its first line is kept so the message stays
        # short and safe to send back to the LLM. `from e` keeps the original for debugging.
        except SQLAlchemyError as e:
            msg = str(getattr(e, "orig", None) or e).strip().splitlines()[0]
            log.warning("Read-only query failed (%s): %s", type(e).__name__, msg)
            raise QueryError(msg) from e


# Kept from the original PostgreSQL version of this project; not used by the billing config.
class PostgresProvider(SQLAlchemyProvider):
    def _prepare(self, conn):
        # Second lock on the door (the first is guardrails.py, the third is the poc_readonly
        # role): the whole transaction is read-only and cannot run longer than the timeout.
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_ms}")


# Registry: config value of db.type -> class to create.
PROVIDERS = {"postgres": PostgresProvider, "sqlalchemy": SQLAlchemyProvider}


# Factory: reads db.type from the config section and returns the matching provider object.
# Called by chatbot_api.py (create_app) and run.py.
def build_db(db_cfg: dict) -> DBProvider:
    kind = db_cfg["type"]
    if kind not in PROVIDERS:
        raise ValueError(f"Unknown db.type '{kind}'. Available: {', '.join(PROVIDERS)}")
    return PROVIDERS[kind](db_cfg)
