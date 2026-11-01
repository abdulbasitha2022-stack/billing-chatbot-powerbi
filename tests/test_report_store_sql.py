"""ReportStore SQL, checked against a fake `pymysql` that records every statement.

These tests need no database. They prove WHAT is sent to MySQL; they do not prove the statements
run on a real server (that needs the migration and a live MySQL)."""
import sys
import types
from datetime import date, datetime

import pytest

from app.naming import InvalidViewName
from app.report_store import ReportStore, ViewExistsError


class FakeMySQLError(Exception):
    pass


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append((" ".join(sql.split()), params))
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise FakeMySQLError(*self.conn.fail_args)
        self.rowcount = self.conn.rowcounts.pop(0) if self.conn.rowcounts else 1

    def fetchone(self):
        return self.conn.fetches.pop(0) if self.conn.fetches else None


class Conn:
    def __init__(self):
        self.log, self.fetches, self.rowcounts, self.fail_on, self.fail_args = [], [], [], None, ()
        self.closed = False

    def cursor(self, *a):
        return Cursor(self)

    def close(self):
        self.closed = True


def with_fake_mysql(fn):
    def runner():
        conn = Conn()
        fake = types.ModuleType("pymysql")
        fake.connect = lambda **kw: conn
        fake.cursors = types.SimpleNamespace(DictCursor=object)
        previous = sys.modules.get("pymysql")
        sys.modules["pymysql"] = fake
        try:
            fn(ReportStore(), conn)
        finally:
            if previous is None:
                sys.modules.pop("pymysql", None)
            else:
                sys.modules["pymysql"] = previous
        assert conn.closed or not conn.log          # a connection that was opened is always closed
    runner.__name__ = fn.__name__
    return runner


@with_fake_mysql
def test_insert_creates_the_row_with_the_13_columns_in_order(store, conn):
    store.insert_metadata(session_id="s1", request_id="r1", user_id=None, user_role=None, platform="Power BI",
                          sql_source="groq_api", request_ts=datetime(2026, 10, 26, 20, 0), day_id=date(2026, 10, 27))
    sql, params = conn.log[0]
    assert sql.startswith("INSERT INTO report_request_metadata (session_id, request_id, user_id, user_role, "
                          "platform, view_name, request_status, sql_source, response_time_ms, "
                          "customer_satisfaction, request_ts, error_message, day_id)")
    assert "VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, NULL, NULL, %s, NULL, %s)" in sql
    assert params == ("s1", "r1", None, None, "Power BI", "initiated", "groq_api",
                      datetime(2026, 10, 26, 20, 0), date(2026, 10, 27))


@with_fake_mysql
def test_update_only_touches_allowed_columns_and_statuses(store, conn):
    store.update_metadata("r1", request_status="completed", view_name="vw_x_20261027_s", response_time_ms=42)
    sql, params = conn.log[0]
    assert sql == ("UPDATE report_request_metadata SET `request_status` = %s, `view_name` = %s, "
                   "`response_time_ms` = %s WHERE request_id = %s")
    assert params == ["completed", "vw_x_20261027_s", 42, "r1"]
    for bad in ({"customer_satisfaction": True}, {"session_id": "x"}, {"day_id": date.today()},
                {"request_ts": 1}, {"nonsense": 1}, {"request_status": "view_created"}):
        with pytest.raises(ValueError):
            store.update_metadata("r1", **bad)


@with_fake_mysql
def test_update_of_a_missing_request_raises(store, conn):
    conn.rowcounts, conn.fetches = [0], [None]
    with pytest.raises(LookupError):
        store.update_metadata("nope", request_status="failed")


@with_fake_mysql
def test_satisfaction_updates_the_existing_row_never_inserts(store, conn):
    conn.fetches = [(None,)]                                   # never rated
    result = store.set_satisfaction("r1", "s1", True)
    assert result == {"found": True, "previous": None, "changed": True}
    statements = [s for s, _ in conn.log]
    assert statements[0].startswith("SELECT customer_satisfaction FROM report_request_metadata")
    assert statements[1] == ("UPDATE report_request_metadata SET customer_satisfaction = %s "
                             "WHERE request_id = %s AND session_id = %s")
    assert conn.log[1][1] == (1, "r1", "s1") and not any("INSERT" in s for s in statements)


@with_fake_mysql
def test_satisfaction_false_is_stored_as_0_and_unchanged_values_skip_the_update(store, conn):
    conn.fetches = [(1,)]
    assert store.set_satisfaction("r1", "s1", False)["changed"] is True
    assert conn.log[1][1] == (0, "r1", "s1")
    conn.log.clear()
    conn.fetches = [(0,)]
    assert store.set_satisfaction("r1", "s1", False) == {"found": True, "previous": False, "changed": False}
    assert len(conn.log) == 1                                  # only the SELECT


@with_fake_mysql
def test_satisfaction_for_another_sessions_request_is_not_found(store, conn):
    conn.fetches = [None]
    assert store.set_satisfaction("r1", "other-session", True)["found"] is False
    assert len(conn.log) == 1


@with_fake_mysql
def test_satisfaction_must_be_a_real_boolean(store, conn):
    for bad in (1, 0, "true", None):
        with pytest.raises(ValueError):
            store.set_satisfaction("r1", "s1", bad)
    assert conn.log == []


@with_fake_mysql
def test_create_view_quotes_a_validated_name(store, conn):
    store.create_view("SELECT 1 AS x", "vw_refunds_by_reason_20261027_f5ba1970c8cf")
    assert conn.log[0][0] == "CREATE VIEW `vw_refunds_by_reason_20261027_f5ba1970c8cf` AS SELECT 1 AS x"


@with_fake_mysql
def test_create_view_refuses_unsafe_names_before_touching_the_database(store, conn):
    for bad in ("vw_x`; DROP TABLE y; --", "VW_Upper", "vw_" + "a" * 70, "refunds", "vw_a b"):
        with pytest.raises(InvalidViewName):
            store.create_view("SELECT 1", bad)
    assert conn.log == []


@with_fake_mysql
def test_mysql_error_1050_becomes_view_exists(store, conn):
    conn.fail_on, conn.fail_args = "CREATE VIEW", (1050, "Table 'vw_x' already exists")
    with pytest.raises(ViewExistsError):
        store.create_view("SELECT 1", "vw_x_20261027_f5ba1970c8cf")


@with_fake_mysql
def test_other_mysql_errors_are_not_swallowed(store, conn):
    conn.fail_on, conn.fail_args = "CREATE VIEW", (1142, "CREATE VIEW command denied")
    with pytest.raises(FakeMySQLError):
        store.create_view("SELECT 1", "vw_x_20261027_f5ba1970c8cf")


@with_fake_mysql
def test_view_exists_checks_information_schema_and_the_registry(store, conn):
    conn.fetches = [None, (1,)]
    assert store.view_exists("vw_x_20261027_f5ba1970c8cf") is True
    assert "information_schema.views" in conn.log[0][0] and "report_views" in conn.log[1][0]
