"""Meaningful view names, live progress text, and the permanent question/answer record."""
import json
import re

import pytest

from app.audit import ConversationRecorder, format_conversation_text
from app.powerbi_project_generator import VIEW_RE
from app.naming import build_view_name
from app.util import slugify
from chatbot_api import ChatService, create_app
from app.pipeline import Pipeline
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json

CFG = {"db": {"allowed_tables": ["charges"], "max_rows": 1000},
       "context": {"files": [], "extra_rules": ""},
       "pipeline": {"max_retries": 1, "max_clarifications": 2},
       "chat": {"ask_chart_type": True}}
SQL = "```sql\nSELECT month_start, revenue_usd FROM charges\n```"
ASK_PERIOD = plan_json("new_report", request="Refunds", confidence=70, sufficient=False,
                       clarification="Which period?")
READY = plan_json("new_report", reply="Refunds by reason for 2026.", request="Refunds by reason in 2026",
                  title="Refunds by Reason - 2026", confidence=95, sufficient=True)


def make(replies, store=None, db=None, jsonl=None):
    cfg = json.loads(json.dumps(CFG))
    store = store or FakeStore()
    pipeline = Pipeline(ScriptedLLM(replies), db or FakeDB(), cfg, base_dir=".")
    recorder = ConversationRecorder(store, jsonl)
    return ChatService(cfg, pipeline, store, FakePowerBI(), recorder=recorder), store


def chat(service, text, sid=None, **extra):
    return service.handle_chat({"message": text, "session_id": sid, **extra})


def full_flow(service):
    a, _ = chat(service, "show refunds")
    b, _ = chat(service, "2026", a["session_id"])
    c, _ = chat(service, "pie chart", a["session_id"])
    return a["session_id"], a, b, c


# ------------------------------------------------------------------ view names
@pytest.mark.parametrize("text,expected", [
    ("Refunds by Reason - 2026", "refunds_by_reason_2026"), ("", "report"),
    ("Café Ünïcode & Résumé!", "cafe_unicode_resume"),
    ("A very long report title about refunds for every single plan and reason in 2026",
     "a_very_long_report_title_about_refunds_for")])
def test_slugify(text, expected):
    assert slugify(text, 44) == expected


def test_view_names_are_meaningful_and_valid():
    from datetime import date
    name = build_view_name("Refunds by Reason - 2026", date(2026, 10, 27), "f5ba1970c8cf")
    assert name == "vw_refunds_by_reason_2026_20261027_f5ba1970c8cf" and VIEW_RE.fullmatch(name)
    assert len(build_view_name("x " * 100, date(2026, 10, 27), "f5ba1970c8cf")) <= 64


def test_generated_view_is_named_after_the_report():
    service, store = make([READY, SQL])
    a, _ = chat(service, "refunds by reason for 2026")
    chat(service, "pie", a["session_id"])
    assert re.fullmatch(r"vw_refunds_by_reason_2026_\d{8}_[0-9a-f]{12}", store.views[0][0])
    assert store.logged[0][3] == store.views[0][0]                        # report_views got the same name


# ------------------------------------------------------------------ progress
def test_progress_text_is_visible_while_the_report_is_being_built_and_cleared_after():
    seen = {}

    class WatchingDB(FakeDB):
        def execute_readonly(self, sql, max_rows):
            seen["during_sql"] = service.progress(seen["sid"])[0]["progress"]
            return super().execute_readonly(sql, max_rows)

    service, store = make([READY, SQL], db=WatchingDB())
    a, _ = chat(service, "refunds by reason for 2026")
    seen["sid"] = a["session_id"]
    assert service.progress(a["session_id"])[0]["progress"] is None       # idle between turns
    chat(service, "pie", a["session_id"])
    assert seen["during_sql"].startswith("Report generation in progress")
    assert "query" in seen["during_sql"]
    assert service.progress(a["session_id"])[0]["progress"] is None       # cleared when done
    assert service.progress("missing")[1] == 404


def test_progress_is_cleared_even_when_the_turn_fails():
    service, _ = make([READY, "nope", "nope"])
    a, _ = chat(service, "refunds by reason for 2026")
    chat(service, "pie", a["session_id"])
    assert service.progress(a["session_id"])[0]["progress"] is None


# ------------------------------------------------------------------ audit record
def test_every_question_and_answer_is_recorded_in_order():
    service, store = make([ASK_PERIOD, READY, SQL])
    sid, a, b, c = full_flow(service)
    rows = [r for r in store.conversation if r["session_id"] == sid]
    assert [r["role"] for r in rows] == ["user", "assistant"] * 3
    assert [r["seq"] for r in rows] == sorted(r["seq"] for r in rows) and len({r["seq"] for r in rows}) == 6
    assert [r["text"] for r in rows if r["role"] == "user"] == ["show refunds", "2026", "pie chart"]
    assert rows[1]["text"] == "Which period?" and rows[1]["kind"] == "clarify"
    assert "What type of visual" in rows[3]["text"]                       # the chart question is recorded
    report = rows[-1]
    assert report["kind"] == "report" and report["page_name"] == "Refunds by Reason - 2026"
    assert re.fullmatch(r"vw_refunds_by_reason_2026_\d{8}_[0-9a-f]{12}", report["view_name"])
    assert report["chart_type"] == "pieChart"
    assert len({r["request_id"] for r in rows}) == 1 and rows[0]["request_id"]     # one request thread


def test_small_talk_is_recorded_without_a_request_id():
    service, store = make([plan_json("chat", reply="Hi!")])
    chat(service, "hello")
    assert [r["text"] for r in store.conversation] == ["hello", "Hi!"]
    assert all(r["request_id"] is None for r in store.conversation)


def test_background_refresh_note_is_recorded_too():
    jobs = []
    cfg = json.loads(json.dumps(CFG))
    store, powerbi = FakeStore(), FakePowerBI(automation=True)
    service = ChatService(cfg, Pipeline(ScriptedLLM([READY, SQL]), FakeDB(), cfg, base_dir="."), store, powerbi,
                          background=jobs.append, recorder=ConversationRecorder(store, None))
    a, _ = chat(service, "refunds by reason for 2026")
    chat(service, "pie", a["session_id"])
    jobs[0]()
    assert store.conversation[-1]["intent"] == "powerbi_refresh" and "refreshed" in store.conversation[-1]["text"]


def test_jsonl_file_is_written_and_used_when_the_table_is_missing(tmp_path):
    path = tmp_path / "logs" / "conversations.jsonl"
    store = FakeStore(fail_log=True)                                       # table not created yet
    service, _ = make([ASK_PERIOD, READY, SQL], store=store, jsonl=path)
    sid, *_ = full_flow(service)                                           # chat still works
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 6 and lines[0]["text"] == "show refunds"
    payload, status = service.audit_session(sid)                           # falls back to the file
    assert status == 200 and [m["role"] for m in payload["messages"]] == ["user", "assistant"] * 3
    assert service.audit_sessions()[0]["sessions"][0]["messages"] == 6


def test_database_logging_failure_never_breaks_the_chat():
    service, _ = make([plan_json("chat", reply="Hi!")], store=FakeStore(fail_log=True))
    payload, status = chat(service, "hello")
    assert status == 200 and payload["message"] == "Hi!"


def test_audit_endpoints_and_text_view():
    cfg = json.loads(json.dumps(CFG))
    store = FakeStore()
    app = create_app(cfg=cfg, llm=ScriptedLLM([ASK_PERIOD, READY, SQL]), db=FakeDB(), store=store,
                     powerbi=FakePowerBI())
    client = app.test_client()
    a = client.post("/chat", json={"message": "show refunds"}).get_json()
    sid = a["session_id"]
    client.post("/chat", json={"message": "2026", "session_id": sid})
    last = client.post("/chat", json={"message": "pie chart", "session_id": sid}).get_json()
    assert client.get(f"/session/{sid}/progress").get_json() == {"progress": None, "busy": False, "ended": False}

    listing = client.get("/audit/sessions").get_json()["sessions"]
    assert listing[0]["session_id"] == sid
    data = client.get(f"/audit/sessions/{sid}").get_json()
    assert len(data["messages"]) == 6
    text = client.get(f"/audit/sessions/{sid}?format=text")
    assert text.mimetype == "text/plain" and "USER: show refunds" in text.get_data(as_text=True)
    assert "ASSISTANT (clarify): Which period?" in text.get_data(as_text=True)
    assert "view: vw_refunds_by_reason_2026_" in text.get_data(as_text=True)

    req = client.get(f"/audit/requests/{last['request_id']}").get_json()
    assert req["metadata"]["request_status"] == "completed" and len(req["conversation"]) == 6
    assert client.get("/audit/sessions/unknown").status_code == 404
    assert client.get("/audit/requests/unknown").status_code == 404


def test_text_format_for_an_empty_conversation():
    assert "No conversation recorded" in format_conversation_text("x", [])
