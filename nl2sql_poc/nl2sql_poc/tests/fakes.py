"""Shared test doubles: scripted LLM, fake DB, fake report store, fake Power BI."""
import json

from app.db import DBProvider, QueryError, QueryResult
from app.llm import LLMProvider, LLMResponse


def plan_json(intent="chat", reply="", request="", title="", chart_type="auto",
              assumptions=None, confidence=90, sufficient=None, clarification="", restricted=None):
    data = {"intent": intent, "reply": reply, "request": request, "title": title,
            "chart_type": chart_type, "assumptions": assumptions or [], "score": confidence,
            "clarification": clarification, "restricted": restricted or []}
    if sufficient is not None:
        data["sufficient"] = sufficient
    return json.dumps(data)


class ScriptedLLM(LLMProvider):
    """Returns queued replies in order and records every (system, messages) call."""
    name = "scripted"

    def __init__(self, replies):
        super().__init__({"model": "scripted-1"})
        self.replies, self.calls = list(replies), []

    def complete(self, system, messages):
        self.calls.append((system, [dict(m) for m in messages]))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return LLMResponse(reply, input_tokens=100, output_tokens=20, extra={"cache_read_tokens": 0})


class FakeDB(DBProvider):
    dialect = "mysql"

    def __init__(self, columns=("month_start", "revenue_usd"), rows=None, fail_first=None):
        self.columns = list(columns)
        self.rows = rows if rows is not None else [("2026-01-01", 100.0), ("2026-02-01", 250.5),
                                                   ("2026-03-01", 75.25)]
        self.fail_first, self.ran = fail_first, []

    def describe(self, tables):
        return "charges(charge_id INT, amount_cents INT, status VARCHAR, created_at DATETIME)"

    def execute_readonly(self, sql, max_rows):
        self.ran.append(sql)
        if self.fail_first and len(self.ran) == 1:
            raise QueryError(self.fail_first)
        return QueryResult(self.columns, [tuple(r) for r in self.rows], False)


class FakeStore:
    """In-memory stand-in for ReportStore with the same rules as the real one: one metadata row
    per request, only known columns/statuses accepted, view names validated and unique."""
    server, database = "localhost:3306", "billing"

    def __init__(self, fail_log=False):
        from app.report_store import METADATA_FIELDS, REQUEST_STATUSES
        self._fields, self._statuses = METADATA_FIELDS, REQUEST_STATUSES
        self.rows, self.views, self.logged, self.saved = {}, [], [], {}
        self.inserts, self.updates = 0, []
        self.taken_names, self.race_names = set(), set()
        self.conversation, self.fail_log = [], fail_log

    # -- conversation audit log
    def log_messages(self, rows):
        if self.fail_log:
            raise RuntimeError("Table 'billing.chat_conversation_log' doesn't exist")
        self.conversation.extend(rows)

    def fetch_conversation(self, session_id):
        return [dict(r, created_at="2026-10-22 09:30:00") for r in self.conversation if r["session_id"] == session_id]

    def fetch_request_conversation(self, request_id):
        return [dict(r, created_at="2026-10-22 09:30:00") for r in self.conversation if r.get("request_id") == request_id]

    def list_conversations(self, limit=50):
        ids = list(dict.fromkeys(r["session_id"] for r in self.conversation))
        return [{"session_id": i, "messages": sum(r["session_id"] == i for r in self.conversation)} for i in ids]

    # -- report_request_metadata (13 columns)
    def insert_metadata(self, *, session_id, request_id, user_id, user_role, platform, sql_source,
                        request_ts, day_id, request_status="initiated"):
        assert request_id not in self.rows, "a request must never be inserted twice"
        assert request_status in self._statuses
        self.inserts += 1
        self.rows[request_id] = {
            "session_id": session_id, "request_id": request_id, "user_id": user_id, "user_role": user_role,
            "platform": platform, "view_name": None, "request_status": request_status,
            "sql_source": sql_source, "response_time_ms": None, "customer_satisfaction": None,
            "request_ts": request_ts, "error_message": None, "day_id": day_id}

    def fetch_metadata(self, request_id):
        return self.rows.get(request_id)

    fetch_status_row = fetch_metadata

    def update_metadata(self, request_id, **fields):
        unknown = set(fields) - self._fields
        assert not unknown, f"unsupported metadata fields: {unknown}"
        if "request_status" in fields:
            assert fields["request_status"] in self._statuses, fields["request_status"]
        if request_id not in self.rows:
            raise LookupError(request_id)
        self.rows[request_id].update(fields)
        self.updates.append((request_id, dict(fields)))

    def set_satisfaction(self, request_id, session_id=None, value=True):
        assert isinstance(value, bool)
        row = self.rows.get(request_id)
        if row is None or (session_id and row["session_id"] != session_id):
            return {"found": False, "previous": None, "changed": False}
        previous = row["customer_satisfaction"]
        if previous is value:
            return {"found": True, "previous": previous, "changed": False}
        row["customer_satisfaction"] = value
        return {"found": True, "previous": previous, "changed": True}

    # -- views
    def view_exists(self, name):
        return name in self.taken_names or any(v[0] == name for v in self.views)

    def create_view(self, sql, name):
        from app.naming import validate_view_name
        from app.report_store import ViewExistsError
        validate_view_name(name)
        if name in self.race_names or any(v[0] == name for v in self.views):
            raise ViewExistsError(name)
        self.views.append((name, sql))

    def log_view(self, *args):
        self.logged.append(args)

    def view_columns(self, view_name):
        return [{"name": "month_start", "type": "date"}, {"name": "revenue_usd", "type": "double"}]

    def fetch_view_record(self, view_name):
        # log_view(session_id, question, sql_text, view_name, assumptions)
        for session_id, question, sql_text, name, assumptions in self.logged:
            if name == view_name:
                return {"view_name": name, "question": question, "sql_text": sql_text,
                        "assumptions": " | ".join(assumptions) or None}
        return None

    def view_defined(self, view_name):
        return any(v[0] == view_name for v in self.views)

    # -- saved_reports (same owner rule as ReportStore._owner_clause)
    def insert_saved_report(self, row):
        if row["request_id"] in self.saved:
            raise RuntimeError("Duplicate entry for key 'PRIMARY'")
        self.saved[row["request_id"]] = dict(row, last_opened_at=None)

    def fetch_saved_report(self, request_id):
        row = self.saved.get(request_id)
        return dict(row) if row else None

    def list_saved_reports(self, user_id, user_role):
        rows = [r for r in self.saved.values()
                if (r["user_id"] == user_id if user_id else r["user_id"] is None and r["user_role"] == user_role)]
        keys = ("request_id", "title", "visual_type", "view_name", "saved_at", "last_opened_at")
        return [{k: r[k] for k in keys} for r in sorted(rows, key=lambda r: r["saved_at"], reverse=True)]

    def touch_saved_report(self, request_id, opened_at):
        self.saved[request_id]["last_opened_at"] = opened_at

    def delete_saved_report(self, request_id):
        return self.saved.pop(request_id, None) is not None


class FakePowerBI:
    def __init__(self, automation=False, fail_reload=False, enabled=True):
        self.enabled, self.automation_enabled, self.reload_delay = enabled, automation, 0
        self.fail_reload, self.pages, self.reloads = fail_reload, [], []
        self.in_report, self.restores = {}, []    # request_id -> page currently in the report

    def build_page(self, view_name, title, requested_visual_type=None, reuse_existing_table=False,
                   request_id=None, session_id=None, **kwargs):
        if not self.enabled:
            return {"status": "disabled", "message": "disabled"}
        visual = requested_visual_type if requested_visual_type not in (None, "auto") else "lineChart"
        page = f"{title} ({len(self.pages) + 1})" if any(p["title"] == title for p in self.pages) else title
        self.pages.append({"title": title, "view": view_name, "visual": visual, "reuse": reuse_existing_table})
        if request_id:
            self.in_report[request_id] = {"page_name": page, "page_id": f"p{len(self.pages)}", "visual": visual}
        return {"status": "generated_pending_reload_and_refresh", "view_name": view_name,
                "model_table": f"billing {view_name}", "page_name": page, "visual_type": visual,
                "dimension_field": "month_start", "measure_field": "revenue_usd"}

    # -- saved reports: pages are found by the request id stored in their feedback/save visual
    def _page(self, request_id):
        return self.in_report.get(request_id)

    def remove_page(self, request_id):
        """Simulates the user deleting the page in Power BI Desktop."""
        del self.in_report[request_id]

    def export_page(self, request_id, view_name):
        page = self._page(request_id)
        if page is None:
            return None
        files = {"page.json": json.dumps({"name": page["page_id"], "displayName": page["page_name"]}),
                 "visuals/chart/visual.json": json.dumps({"visual": {"visualType": page["visual"]}})}
        return {"page_id": page["page_id"], "page_name": page["page_name"], "visual_type": page["visual"],
                "view_name": view_name, "model_table": f"billing {view_name}", "files": files,
                "table_tmdl": "table 'billing %s'" % view_name}

    def restore_page(self, saved):
        self.restores.append(saved["request_id"])
        page = self._page(saved["request_id"])
        if page is not None:
            return {"status": "already_present", "page_id": page["page_id"], "page_name": page["page_name"]}
        name = json.loads(saved["files"]["page.json"])["displayName"]
        self.in_report[saved["request_id"]] = {"page_name": name, "page_id": saved["page_id"],
                                               "visual": saved["visual_type"]}
        return {"status": "generated_pending_reload_and_refresh", "page_id": saved["page_id"],
                "page_name": name, "view_name": saved["view_name"], "model_table": saved["model_table"]}

    def reload_refresh(self, generated):
        self.reloads.append(generated["page_name"])
        if self.fail_reload:
            raise RuntimeError("Desktop not open")
        return {"success": True}
