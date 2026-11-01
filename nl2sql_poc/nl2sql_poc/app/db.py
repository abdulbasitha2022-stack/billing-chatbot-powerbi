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
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError


class QueryError(RuntimeError):
    """The database rejected or failed the query. Message is safe to feed back to the model."""


@dataclass
class QueryResult:
    columns: list
    rows: list
    truncated: bool


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


def _escape_colons(sql: str) -> str:
    """SQLAlchemy's text() treats ':name' as a bind parameter. Escape those so a literal like
    'a :b' survives, while leaving '::' casts alone."""
    return re.sub(r"(?<!:):(?=[A-Za-z_])", r"\\:", sql)


class SQLAlchemyProvider(DBProvider):
    def __init__(self, cfg: dict):
        # Prefer the environment variable so credentials never live in a config file in git.
        url = os.getenv(cfg.get("url_env", "DATABASE_URL")) or cfg["url"]
        self.engine = create_engine(url, pool_pre_ping=True)
        self.dialect = _SQLGLOT_DIALECTS.get(self.engine.dialect.name, self.engine.dialect.name)
        self.timeout_ms = int(cfg.get("statement_timeout_ms", 15000))

    def _prepare(self, conn):
        """Hook: per-connection safety settings. Subclasses override."""

    def describe(self, tables):
        insp = inspect(self.engine)
        lines = []
        for t in tables:
            try:
                cols = insp.get_columns(t)
            except NoSuchTableError:
                raise RuntimeError(f"Table '{t}' (from db.allowed_tables) does not exist in the "
                                   f"database. Did you run generate_sample_data.py --load?")
            lines.append(f"{t}(" + ", ".join(f"{c['name']} {c['type']}" for c in cols) + ")")
        return "\n".join(lines)

    def table_columns(self, tables):
        """{table: [(column, TYPE), ...]} from the live database, for role-filtered schemas."""
        insp = inspect(self.engine)
        return {t: [(c["name"], str(c["type"])) for c in insp.get_columns(t)] for t in tables}

    def execute_readonly(self, sql, max_rows):
        try:
            with self.engine.connect() as conn:
                self._prepare(conn)
                result = conn.execute(text(_escape_colons(sql)))
                columns = list(result.keys())
                rows = result.fetchmany(max_rows + 1)
                return QueryResult(columns, [tuple(r) for r in rows[:max_rows]],
                                   truncated=len(rows) > max_rows)
        except SQLAlchemyError as e:
            msg = str(getattr(e, "orig", None) or e).strip().splitlines()[0]
            raise QueryError(msg) from e


class PostgresProvider(SQLAlchemyProvider):
    def _prepare(self, conn):
        # Second lock on the door (the first is guardrails.py, the third is the poc_readonly
        # role): the whole transaction is read-only and cannot run longer than the timeout.
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_ms}")


PROVIDERS = {"postgres": PostgresProvider, "sqlalchemy": SQLAlchemyProvider}


def build_db(db_cfg: dict) -> DBProvider:
    kind = db_cfg["type"]
    if kind not in PROVIDERS:
        raise ValueError(f"Unknown db.type '{kind}'. Available: {', '.join(PROVIDERS)}")
    return PROVIDERS[kind](db_cfg)
