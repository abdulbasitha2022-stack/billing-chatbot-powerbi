"""MySQL bookkeeping: request metadata, report views and the view-builder connection.

report_request_metadata holds ONE ROW PER REPORT-GENERATION REQUEST (not per chat message; messages
live in chat_conversation_log). Columns, in order:
  session_id, request_id (PK), user_id, user_role, platform, view_name, request_status, sql_source,
  response_time_ms, customer_satisfaction (NULL = not rated, 1 = satisfied, 0 = unsatisfied),
  request_ts (UTC), error_message, day_id (calendar date in the application time zone).

request_status values:  initiated -> in_progress -> completed | failed

Uses the restricted `view_builder` account (SELECT + INSERT/UPDATE on the bookkeeping tables
+ CREATE VIEW).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from .naming import validate_view_name

# Columns that may be changed after the row is created (session_id, request_id, request_ts and
# day_id are fixed at creation; satisfaction has its own method).
METADATA_FIELDS = {
    "user_id", "user_role", "platform", "view_name", "request_status", "sql_source",
    "response_time_ms", "error_message",
}
REQUEST_STATUSES = ("initiated", "in_progress", "completed", "failed")
_METADATA_COLUMNS = ("session_id, request_id, user_id, user_role, platform, view_name, request_status, "
                     "sql_source, response_time_ms, customer_satisfaction, request_ts, error_message, day_id")


class ViewExistsError(RuntimeError):
    """A view with that name already exists (MySQL error 1050)."""


def utc_naive_now():
    """Store UTC in MySQL DATETIME columns by convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReportStore:
    def __init__(self):
        self.host = os.getenv("BILLING_DB_HOST", "localhost")
        self.port = int(os.getenv("BILLING_DB_PORT", "3306"))
        self.database = os.getenv("BILLING_DB_NAME", "billing")
        self.user = os.getenv("VIEW_BUILDER_USER", "view_builder")
        self.password = os.getenv("VIEW_BUILDER_PASSWORD", "view_builder")

    @property
    def server(self) -> str:
        return f"{self.host}:{self.port}"

    def _connect(self):
        import pymysql
        return pymysql.connect(host=self.host, port=self.port, user=self.user,
                               password=self.password, database=self.database, autocommit=True,
                               connect_timeout=10, charset="utf8mb4")

    # ------------------------------------------------------------------ request metadata
    def insert_metadata(self, *, session_id, request_id, user_id, user_role, platform, sql_source,
                        request_ts, day_id, request_status="initiated"):
        """Create the single row for a new report request. request_ts: naive UTC; day_id: a date."""
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO report_request_metadata ({_METADATA_COLUMNS})
                        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, NULL, NULL, %s, NULL, %s)""",
                    (session_id, request_id, user_id, user_role, platform, request_status,
                     sql_source, request_ts, day_id))
        finally:
            conn.close()

    def fetch_metadata(self, request_id):
        import pymysql
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(f"SELECT {_METADATA_COLUMNS} FROM report_request_metadata WHERE request_id = %s",
                            (request_id,))
                return cur.fetchone()
        finally:
            conn.close()

    fetch_status_row = fetch_metadata

    def update_metadata(self, request_id, **fields):
        if not fields:
            return
        unknown = set(fields) - METADATA_FIELDS
        if unknown:
            raise ValueError(f"Unsupported metadata fields: {', '.join(sorted(unknown))}")
        status = fields.get("request_status")
        if status is not None and status not in REQUEST_STATUSES:
            raise ValueError(f"Unsupported request_status: {status}")
        assignments = ", ".join(f"`{name}` = %s" for name in fields)
        values = list(fields.values()) + [request_id]
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE report_request_metadata SET {assignments} WHERE request_id = %s", values)
                if cur.rowcount == 0:
                    cur.execute("SELECT 1 FROM report_request_metadata WHERE request_id = %s LIMIT 1",
                                (request_id,))
                    if cur.fetchone() is None:
                        raise LookupError(f"request_id '{request_id}' was not found")
        finally:
            conn.close()

    def set_satisfaction(self, request_id, session_id=None, value: bool = True) -> dict:
        """UPDATE (never insert) customer_satisfaction of the request that belongs to this session.

        Returns {"found", "previous", "changed"}. previous is None when it was never rated."""
        if not isinstance(value, bool):
            raise ValueError("customer_satisfaction must be True or False")
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if session_id:
                    cur.execute("SELECT customer_satisfaction FROM report_request_metadata "
                                "WHERE request_id = %s AND session_id = %s", (request_id, session_id))
                else:
                    cur.execute("SELECT customer_satisfaction FROM report_request_metadata "
                                "WHERE request_id = %s", (request_id,))
                row = cur.fetchone()
                if row is None:
                    return {"found": False, "previous": None, "changed": False}
                previous = None if row[0] is None else bool(row[0])
                if previous is value:
                    return {"found": True, "previous": previous, "changed": False}
                if session_id:
                    cur.execute("UPDATE report_request_metadata SET customer_satisfaction = %s "
                                "WHERE request_id = %s AND session_id = %s",
                                (1 if value else 0, request_id, session_id))
                else:
                    cur.execute("UPDATE report_request_metadata SET customer_satisfaction = %s "
                                "WHERE request_id = %s",
                                (1 if value else 0, request_id))
                return {"found": True, "previous": previous, "changed": True}
        finally:
            conn.close()

    # ------------------------------------------------------------------ views
    def view_exists(self, view_name: str) -> bool:
        """Best effort: is this view name already used (database or report registry)?"""
        validate_view_name(view_name)
        for sql in ("SELECT 1 FROM information_schema.views WHERE table_schema = DATABASE() AND table_name = %s",
                    "SELECT 1 FROM report_views WHERE view_name = %s"):
            try:
                conn = self._connect()
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, (view_name,))
                        if cur.fetchone() is not None:
                            return True
                finally:
                    conn.close()
            except Exception:
                continue
        return False

    def create_view(self, sql_text: str, view_name: str):
        """Create a view using SQL that has already passed the SQL guardrails.

        The name is validated (vw_ + lowercase letters/digits/underscores, max 64) so it can be
        quoted with backticks safely. Raises ViewExistsError if the name is taken."""
        validate_view_name(view_name)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE VIEW `{view_name}` AS {sql_text}")
        except Exception as exc:
            if getattr(exc, "args", None) and exc.args and exc.args[0] == 1050:
                raise ViewExistsError(view_name) from exc
            raise
        finally:
            conn.close()

    def log_view(self, session_id, question, sql_text, view_name, assumptions):
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO report_views (session_id, question, sql_text, view_name, assumptions)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (session_id, question, sql_text, view_name, " | ".join(assumptions) or None))
        finally:
            conn.close()

    def view_columns(self, view_name: str) -> list:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW COLUMNS FROM `{view_name}`")
                return [{"name": row[0], "type": row[1]} for row in cur.fetchall()]
        finally:
            conn.close()

    # ------------------------------------------------------------------ conversation audit log
    def log_messages(self, rows: list):
        """Insert chat messages into chat_conversation_log (see chatbot_schema.sql)."""
        if not rows:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO chat_conversation_log
                       (session_id, seq, request_id, role, message_kind, intent, message_text,
                        view_name, page_name, chart_type, requested_by, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    [(r["session_id"], r["seq"], r.get("request_id"), r["role"], r.get("kind"),
                      r.get("intent"), r["text"], r.get("view_name"), r.get("page_name"),
                      r.get("chart_type"), r.get("requested_by"), utc_naive_now()) for r in rows])
        finally:
            conn.close()

    _CONV_COLUMNS = ("session_id, seq, request_id, role, message_kind AS kind, intent, "
                     "message_text AS text, view_name, page_name, chart_type, requested_by, created_at")

    def fetch_conversation(self, session_id: str) -> list:
        import pymysql
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(f"SELECT {self._CONV_COLUMNS} FROM chat_conversation_log "
                            "WHERE session_id = %s ORDER BY seq", (session_id,))
                return list(cur.fetchall())
        finally:
            conn.close()

    def fetch_request_conversation(self, request_id: str) -> list:
        import pymysql
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(f"SELECT {self._CONV_COLUMNS} FROM chat_conversation_log "
                            "WHERE request_id = %s ORDER BY session_id, seq", (request_id,))
                return list(cur.fetchall())
        finally:
            conn.close()

    def list_conversations(self, limit: int = 50) -> list:
        import pymysql
        conn = self._connect()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    """SELECT session_id, MIN(created_at) AS started, MAX(created_at) AS last_activity,
                              COUNT(*) AS messages,
                              SUM(role = 'user') AS questions,
                              COUNT(DISTINCT view_name) AS reports
                       FROM chat_conversation_log GROUP BY session_id
                       ORDER BY last_activity DESC LIMIT %s""", (int(limit),))
                return list(cur.fetchall())
        finally:
            conn.close()
