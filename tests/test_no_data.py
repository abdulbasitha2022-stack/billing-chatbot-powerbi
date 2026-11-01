"""A request whose query matches no rows gets an explanation (with the data's date range), not a blank page."""
import json
from datetime import date, datetime

from app.coverage import DataCoverage, coverage_sentence, no_data_message
from app.db import QueryError, QueryResult
from app.pipeline import Pipeline
from chatbot_api import ChatService
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json

SQL = "```sql\nSELECT month_start, revenue_usd FROM charges WHERE 1=0\n```"
REPORT = plan_json("new_report", reply="Gross revenue by month for 2022.", request="Monthly gross revenue in 2022",
                   title="2022 Monthly Revenue", confidence=95, sufficient=True)


class CoverageDB(FakeDB):
    """Main query returns `rows`; coverage queries (MIN/MAX) return a date range."""
    def __init__(self, rows, ranges=None, fail_coverage=False):
        super().__init__(rows=rows)
        self.ranges = ranges if ranges is not None else {"charges": (datetime(2024, 3, 5, 10, 0), datetime(2026, 10, 20, 9, 0))}
        self.fail_coverage, self.coverage_sql = fail_coverage, []

    def execute_readonly(self, sql, max_rows):
        if "MIN(" in sql:
            self.coverage_sql.append(sql)
            if self.fail_coverage:
                raise QueryError("no permission")
            table = sql.split(" FROM ")[1].split()[0]
            span = self.ranges.get(table)
            return QueryResult(["first_date", "last_date"], [span] if span else [(None, None)], False)
        return super().execute_readonly(sql, max_rows)


def make(replies, db, chat_cfg=None):
    cfg = {"db": {"allowed_tables": ["charges"], "max_rows": 1000}, "context": {"files": []},
           "pipeline": {"max_retries": 1}, "chat": {"ask_chart_type": False, **(chat_cfg or {})}}
    store, powerbi = FakeStore(), FakePowerBI()
    llm = ScriptedLLM(replies)
    return ChatService(cfg, Pipeline(llm, db, json.loads(json.dumps(cfg)), base_dir="."), store, powerbi), store, powerbi


def chat(service, text, sid=None):
    return service.handle_chat({"message": text, "session_id": sid})


def test_empty_result_builds_no_page_and_explains_the_data_range():
    service, store, powerbi = make([REPORT, SQL], CoverageDB(rows=[]))
    payload, status = chat(service, "monthly revenue for 2022")
    assert status == 200 and payload["status"] == "no_data" and payload["result_row_count"] == 0
    msg = payload["message"]
    assert "returned no rows" in msg and "didn't create a page" in msg
    assert "covers Mar 2024 to Oct 2026" in msg
    assert not store.views and not powerbi.pages                       # nothing blank is built
    labels = [c["label"] for c in payload["messages"][-1]["meta"]["suggestions"]]
    assert labels == ["Try 2024", "Try 2025", "Try 2026"]


def test_audit_row_records_what_happened():
    service, store, _ = make([REPORT, SQL], CoverageDB(rows=[]))
    payload, _ = chat(service, "monthly revenue for 2022")
    row = store.rows[payload["request_id"]]
    assert row["request_status"] == "failed" and "no rows" in row["error_message"]
    assert row["view_name"] is None and row["response_time_ms"] is not None   # no view was created


def test_the_suggested_year_chip_leads_to_a_real_report():
    db = CoverageDB(rows=[])
    redo = plan_json("new_report", reply="Gross revenue for 2025.", request="Monthly gross revenue in 2025",
                     title="2025 Monthly Revenue", confidence=95, sufficient=True)
    service, store, powerbi = make([REPORT, SQL, redo, SQL.replace("1=0", "1=1")], db)
    a, _ = chat(service, "monthly revenue for 2022")
    db.rows = [("2025-01-01", 10.0), ("2025-02-01", 20.0)]             # data exists for 2025
    b, _ = chat(service, "Same request, but for 2025", a["session_id"])
    assert b["status"] == "ok" and len(store.views) == 1 and powerbi.pages[0]["title"] == "2025 Monthly Revenue"


def test_planner_remembers_which_request_came_back_empty():
    service, store, _ = make([REPORT, SQL, plan_json("chat", reply="ok")], CoverageDB(rows=[]))
    a, _ = chat(service, "monthly revenue for 2022")
    chat(service, "thanks", a["session_id"])
    history = service.sessions.get(a["session_id"]).history
    assert 'The request "Monthly gross revenue in 2022" returned no rows' in history[1]["content"]


def test_coverage_uses_the_tables_the_query_touched():
    db = CoverageDB(rows=[], ranges={"refunds": (datetime(2025, 1, 2), datetime(2026, 9, 9)),
                                     "charges": (datetime(2024, 3, 5), datetime(2026, 10, 20))})
    coverage = DataCoverage(db)
    ranges = coverage.for_sql("SELECT reason FROM refunds r JOIN charges c ON c.id=r.charge_id")
    assert [r["label"] for r in ranges] == ["charges", "refunds"]          # both tables used
    db.coverage_sql.clear()
    only = coverage.for_sql("SELECT * FROM refunds")
    assert [r["label"] for r in only] == ["refunds"]
    assert coverage_sentence(ranges).startswith("The data I have covers charges: Mar 2024 to Oct 2026; refunds:")


def test_coverage_lookups_are_cached():
    db = CoverageDB(rows=[])
    service, _, _ = make([REPORT, SQL, REPORT, SQL], db)
    a, _ = chat(service, "monthly revenue for 2022")
    first = len(db.coverage_sql)
    chat(service, "monthly revenue for 2022 again", a["session_id"])
    assert first >= 1 and len(db.coverage_sql) == first


def test_message_still_works_if_the_date_range_cannot_be_read():
    service, store, _ = make([REPORT, SQL], CoverageDB(rows=[], fail_coverage=True))
    payload, status = chat(service, "monthly revenue for 2022")
    assert status == 200 and payload["status"] == "no_data" and "covers" not in payload["message"]
    assert "meta" in payload["messages"][-1] and not payload["messages"][-1]["meta"].get("suggestions")
    assert not store.views


def test_old_behaviour_is_available_with_the_switch_off():
    service, store, powerbi = make([REPORT, SQL], CoverageDB(rows=[]), {"skip_empty_reports": False})
    payload, _ = chat(service, "monthly revenue for 2022")
    assert payload["status"] == "ok" and len(store.views) == 1 and "no rows" in payload["message"]


def test_normal_reports_are_untouched():
    service, store, _ = make([REPORT, SQL], CoverageDB(rows=[("2024-04-01", 5.0)]))
    payload, _ = chat(service, "monthly revenue for 2024")
    assert payload["status"] == "ok" and len(store.views) == 1


def test_no_data_message_text_and_identifier_safety():
    text, chips = no_data_message(None, None)
    assert text.startswith("I ran your request") and chips == []
    unsafe = DataCoverage(CoverageDB(rows=[]), [{"table": "charges; DROP TABLE x", "column": "created_at"}])
    assert unsafe.sources == [] and unsafe.for_sql("select 1") == []       # bad names are ignored, never run
