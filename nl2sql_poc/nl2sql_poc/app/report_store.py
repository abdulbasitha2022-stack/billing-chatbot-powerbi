"""MySQL bookkeeping: request metadata, report views and the view-builder connection.

report_request_metadata holds ONE ROW PER REPORT-GENERATION REQUEST (not per chat message; messages
live in chat_conversation_log). Columns, in order:
  request_id (PK), session_id, user_id, user_role, platform, view_name, request_status, sql_source,
  response_time_ms, customer_satisfaction (NULL = not rated, 1 = satisfied, 0 = unsatisfied),
  request_ts (UTC), error_message, day_id (calendar date in the application time zone).

request_status values:  initiated -> in_progress -> completed | failed

Uses the restricted `view_builder` account (SELECT + INSERT/UPDATE on the bookkeeping tables
+ CREATE VIEW).
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# What it does: every WRITE the chatbot makes to MySQL goes through this class:
#   * report_request_metadata - one bookkeeping row per report request (status, timing, rating)
#   * CREATE VIEW vw_...      - saves the validated report SQL as a MySQL view; Power BI then
#                               reads that view as its data source
#   * report_views            - a registry of the views created (question, SQL, assumptions)
#   * chat_conversation_log   - every chat message (written via audit.py)
# Who calls it: chatbot_api.py (metadata, views, satisfaction), audit.py (conversation log)
#   and powerbi_service.py (view_columns, to know which columns the Power BI table needs).
# What it calls: pymysql directly (not SQLAlchemy). pymysql is a pure-Python MySQL client.
#
# Which DB account? NOT the read-only billing_readonly account used in db.py, but the separate
#   `view_builder` account, which may only insert/update the bookkeeping tables and create views.
#   Keeping the two apart means the LLM-driven query path can never write anything.
# Every method opens a fresh connection and closes it in `finally`, so a failure never leaves a
#   connection hanging. autocommit=True means each statement is saved immediately.
# All values are passed as %s parameters (pymysql escapes them) - never pasted into the SQL
#   text - which prevents SQL injection. Only identifiers we have validated are put in f-strings.
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .naming import validate_view_name

log = logging.getLogger(__name__)   # "app.report_store" -> logs/backend_billing.log

# Columns that may be changed after the row is created (request_id, session_id, request_ts and
# day_id are fixed at creation; satisfaction has its own method).
METADATA_FIELDS = {
    "user_id", "user_role", "platform", "view_name", "request_status", "sql_source",
    "response_time_ms", "error_message",
}
REQUEST_STATUSES = ("initiated", "in_progress", "completed", "failed")
# Column list reused by the INSERT and SELECT statements below (keeps them in the same order).
_METADATA_COLUMNS = ("request_id, session_id, user_id, user_role, platform, view_name, request_status, "
                     "sql_source, response_time_ms, customer_satisfaction, request_ts, error_message, day_id")


# Raised by create_view() when the name is taken; chatbot_api.py catches it and retries with
# a different (suffixed) name.
class ViewExistsError(RuntimeError):
    """A view with that name already exists (MySQL error 1050)."""


def utc_naive_now():
    """Store UTC in MySQL DATETIME columns by convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ReportStore:
    # Connection settings come from environment variables, with local-demo defaults.
    # Created once by chatbot_api.py (create_app). The password is never logged.
    def __init__(self):
        self.host = os.getenv("BILLING_DB_HOST", "localhost")
        self.port = int(os.getenv("BILLING_DB_PORT", "3306"))
        self.database = os.getenv("BILLING_DB_NAME", "billing")
        self.user = os.getenv("VIEW_BUILDER_USER", "view_builder")
        self.password = os.getenv("VIEW_BUILDER_PASSWORD", "view_builder")
        log.info("Report store: %s@%s/%s (password not logged)", self.user, self.server, self.database)

    @property
    def server(self) -> str:
        return f"{self.host}:{self.port}"

    # Opens a new pymysql connection as the view_builder account. autocommit=True: no explicit
    # commit needed; connect_timeout=10: give up after 10 s if MySQL is unreachable;
    # utf8mb4: MySQL's full Unicode charset (emoji, any language) for chat text.
    def _connect(self):
        import pymysql
        return pymysql.connect(host=self.host, port=self.port, user=self.user,
                               password=self.password, database=self.database, autocommit=True,
                               connect_timeout=10, charset="utf8mb4")

    # ------------------------------------------------------------------ request metadata
    def insert_metadata(self, *, session_id, request_id, user_id, user_role, platform, sql_source,
                        request_ts, day_id, request_status="initiated"):
        """Create the single row for a new report request. request_ts: naive UTC; day_id: a date."""
        # WRITE (view_builder). Called by chatbot_api.py when a report request starts. The NULLs
        # are view_name, response_time_ms, customer_satisfaction and error_message - filled later.
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"""INSERT INTO report_request_metadata ({_METADATA_COLUMNS})
                        VALUES (%s, %s, %s, %s, %s, NULL, %s, %s, NULL, NULL, %s, NULL, %s)""",
                    (request_id, session_id, user_id, user_role, platform, request_status,
                     sql_source, request_ts, day_id))
            log.debug("Metadata row %s inserted (%s, source %s)", request_id, request_status, sql_source)
        finally:
            conn.close()

    # READ the metadata row of one request as a dict (DictCursor = rows as {column: value}),
    # or None if not found. Used by chatbot_api.py for status checks.
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

    fetch_status_row = fetch_metadata   # an alias: a second name for the same method

    # WRITE (view_builder). Updates some columns of a request's row, e.g.
    # update_metadata(rid, request_status="completed", view_name="vw_...").
    # Called by chatbot_api.py as the request moves through its statuses.
    def update_metadata(self, request_id, **fields):
        if not fields:
            return
        # The field NAMES end up inside the SQL text (below), so they are checked against a
        # fixed whitelist first - this is what makes the f-string safe from SQL injection.
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
                # MySQL reports 0 changed rows both when the row is missing AND when the new
                # values equal the old ones. A quick SELECT tells the two cases apart; only a
                # truly missing row is an error.
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
        # Called by chatbot_api.py (POST /feedback) when the user clicks Satisfied / Unsatisfied.
        if not isinstance(value, bool):
            raise ValueError("customer_satisfaction must be True or False")
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                # Step 1: read the current rating. Restricting by session_id stops one chat
                # session from rating another session's report.
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
                # Same rating as before: nothing to write.
                if previous is value:
                    return {"found": True, "previous": previous, "changed": False}
                # Step 2: WRITE the new rating (stored as 1/0 in MySQL).
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
        # Called by chatbot_api.py (via naming.unique_view_name) to pick a free name.
        validate_view_name(view_name)
        # Two places are checked: MySQL's own catalogue of views (information_schema.views in
        # the current DATABASE()), and our report_views registry table.
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
            # "Best effort": if one check fails (e.g. report_views table missing, no permission)
            # log it and try the other. A clash that slips through is still caught later by
            # create_view() raising ViewExistsError.
            except Exception as exc:
                log.warning("View-name check skipped one source (%s: %s)", type(exc).__name__, exc)
                continue
        return False

    def create_view(self, sql_text: str, view_name: str):
        """Create a view using SQL that has already passed the SQL guardrails.

        The name is validated (vw_ + lowercase letters/digits/underscores, max 64) so it can be
        quoted with backticks safely. Raises ViewExistsError if the name is taken."""
        # WRITE / DDL (view_builder account). Called by chatbot_api.py after validate_sql().
        # A view stores the query, not the data: Power BI re-runs it on every refresh.
        # Identifiers cannot be passed as %s parameters, hence the strict name check first,
        # and sql_text is the guardrails-normalised SELECT, never raw LLM text.
        validate_view_name(view_name)
        log.debug("CREATE VIEW %s AS\n%s", view_name, sql_text)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE VIEW `{view_name}` AS {sql_text}")
        except Exception as exc:
            # pymysql errors carry the MySQL error number in args[0]; 1050 = "Table/view
            # already exists". That one becomes ViewExistsError so the caller can pick another
            # name; anything else is logged and re-raised unchanged.
            if getattr(exc, "args", None) and exc.args and exc.args[0] == 1050:
                raise ViewExistsError(view_name) from exc
            log.error("CREATE VIEW %s failed: %s: %s", view_name, type(exc).__name__, exc)
            raise
        finally:
            conn.close()

    # WRITE: one row in the report_views registry for each view created (called by
    # chatbot_api.py right after create_view). The LLM's assumptions are joined with " | ".
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

    # READ the column names and MySQL types of a view, e.g. [{"name": "month", "type": "date"}].
    # Called by powerbi_service.py to build the matching Power BI table definition.
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
        # Called by audit.ConversationRecorder.record(). executemany() runs the same INSERT
        # once per row in a single call.
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

    # READ helpers below are used by audit.py to show transcripts. The column list renames
    # message_kind/message_text to kind/text so rows look like the JSONL file's rows.
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

    # One summary row per chat session, newest first. In MySQL `role = 'user'` is 1 or 0, so
    # SUM(...) counts the user's questions. int(limit) guarantees a number is passed.
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
