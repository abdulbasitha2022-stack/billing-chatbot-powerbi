"""Test setup. When sqlglot / SQLAlchemy are not installed (e.g. an offline sandbox) light
stand-ins for app.guardrails and app.db are registered so the conversation, pipeline and API
logic can still be tested. With the real packages installed nothing is replaced."""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import sqlglot  # noqa: F401
    import sqlalchemy  # noqa: F401
except ImportError:
    from abc import ABC, abstractmethod
    from dataclasses import dataclass

    import app  # the real package (empty __init__)

    guard = types.ModuleType("app.guardrails")

    class GuardrailError(ValueError):
        pass

    @dataclass
    class Validated:
        sql: str
        tables: list

    def validate_sql(sql, dialect, allowed_tables, max_rows):
        text = sql.strip().rstrip(";")
        if not text.lower().startswith(("select", "with")):
            raise GuardrailError("Only SELECT queries are allowed.")
        for word in ("insert ", "update ", "delete ", "drop "):
            if word in text.lower():
                raise GuardrailError("Statement type not allowed.")
        if " limit " not in text.lower():
            text += f" LIMIT {max_rows}"
        return Validated(text, [])

    guard.GuardrailError, guard.validate_sql = GuardrailError, validate_sql

    dbm = types.ModuleType("app.db")

    class QueryError(RuntimeError):
        pass

    @dataclass
    class QueryResult:
        columns: list
        rows: list
        truncated: bool

    class DBProvider(ABC):
        dialect = "unknown"

        @abstractmethod
        def describe(self, tables): ...

        @abstractmethod
        def execute_readonly(self, sql, max_rows): ...

    dbm.QueryError, dbm.QueryResult, dbm.DBProvider = QueryError, QueryResult, DBProvider
    dbm.build_db = lambda cfg: (_ for _ in ()).throw(RuntimeError("no real DB in tests"))
    sys.modules["app.guardrails"], sys.modules["app.db"] = guard, dbm
    app.guardrails, app.db = guard, dbm
