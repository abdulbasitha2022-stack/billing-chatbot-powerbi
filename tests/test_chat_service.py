"""End-to-end conversation tests through ChatService with fake LLM / MySQL / Power BI."""
import json

import pytest

from app.conversation import CHART_QUESTION, detect_chart_choice
from chatbot_api import ChatService, create_app
from app.db import QueryResult
from app.pipeline import Pipeline
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json

CFG = {"db": {"allowed_tables": ["charges"], "max_rows": 1000},
       "context": {"files": [], "extra_rules": ""},
       "pipeline": {"max_retries": 1, "max_clarifications": 1}}
SQL = "ASSUMPTION: all successful charges\n```sql\nSELECT month_start, revenue_usd FROM charges\n```"
REPORT = plan_json("new_report", reply="Monthly gross revenue from successful charges.",
                   request="Monthly gross revenue", title="Monthly Revenue",
                   assumptions=["period: all time"])


def make(replies, powerbi=None, db=None, background=None, ask_chart=False):
    """ask_chart=False keeps the older tests focused; the chart-last flow has its own tests below."""
    cfg = json.loads(json.dumps(CFG))
    cfg["chat"] = {"ask_chart_type": ask_chart}
    llm, store = ScriptedLLM(replies), FakeStore()
    powerbi = powerbi or FakePowerBI()
    pipeline = Pipeline(llm, db or FakeDB(), cfg, base_dir=".")
    return ChatService(cfg, pipeline, store, powerbi, background=background), llm, store, powerbi


def chat(service, text, session_id=None, **extra):
    return service.handle_chat({"message": text, "session_id": session_id, **extra})


# ------------------------------------------------------------------ basics
def test_smalltalk_needs_no_sql_and_no_metadata():
    service, llm, store, _ = make([plan_json("chat", reply="Hi! Ask me about revenue, refunds or subscriptions.")])
    payload, status = chat(service, "hello")
    assert status == 200 and payload["status"] == "chat" and "Hi!" in payload["message"]
    assert len(llm.calls) == 1 and not store.rows and not store.views
    assert [m["role"] for m in payload["messages"]] == ["user", "assistant"]


def test_report_message_names_the_page_and_says_refresh():
    service, _, store, powerbi = make([REPORT, SQL])
    payload, status = chat(service, "monthly revenue")
    assert status == 200 and payload["status"] == "ok"
    msg = payload["message"]
    assert "Report generated" in msg and "“Monthly Revenue”" in msg and "Click **Refresh**" in msg
    assert "Highest revenue: $250.50 (2026-02-01)" in msg      # deterministic quick take
    assert "Assumptions:" in msg
    assert len(store.views) == 1 and powerbi.pages[0]["title"] == "Monthly Revenue"
    row = next(iter(store.rows.values()))
    assert row["request_status"] == "completed" and row["view_name"] == store.views[0][0]
    assert row["session_id"] == payload["session_id"] and row["customer_satisfaction"] is None
    labels = [c["label"] for c in payload["messages"][-1]["meta"]["suggestions"]]
    assert labels[0] == "Explain this report" and "Pie" in labels and "Line" not in labels


# ------------------------------------------------------------------ memory after a report
def test_conversation_continues_after_report_and_explains_from_data():
    service, llm, store, _ = make([
        REPORT, SQL,
        plan_json("explain", reply="February was the strongest month at **$250.50**.")])
    first, _ = chat(service, "monthly revenue")
    sid = first["session_id"]
    second, status = chat(service, "which month was best?", sid)
    assert status == 200 and second["status"] == "chat" and "February" in second["message"]
    assert second["session_id"] == sid and len(store.views) == 1      # no new view for a question
    system, messages = llm.calls[-1]                                   # planner call for turn 2
    assert 'LAST REPORT: page "Monthly Revenue"' in system
    assert "2026-02-01,250.5" in system and "max $250.50 (2026-02-01)" in system.replace("revenue_usd: ", "")
    roles = [m["role"] for m in messages]
    assert roles == ["user", "assistant", "user"] and messages[-1]["content"] == "which month was best?"
    assert "Monthly Revenue" in messages[1]["content"]                 # memory of the report


def test_modify_report_sends_previous_sql_and_builds_a_new_page():
    service, llm, store, powerbi = make([
        REPORT, SQL,
        plan_json("modify_report", reply="Same, for 2025.", request="Monthly gross revenue for 2025",
                  title="Monthly Revenue 2025"),
        "```sql\nSELECT month_start, revenue_usd FROM charges WHERE 1=1\n```"])
    first, _ = chat(service, "monthly revenue")
    second, _ = chat(service, "same but only 2025", first["session_id"])
    assert second["status"] == "ok" and len(store.views) == 2 and len(powerbi.pages) == 2
    sql_call_user = llm.calls[-1][1][0]["content"]
    assert "Earlier SQL" in sql_call_user and "SELECT month_start, revenue_usd FROM charges" in sql_call_user


def test_modify_without_a_report_becomes_a_new_report():
    service, _, store, _ = make([plan_json("modify_report", request="Revenue by plan", title="Revenue By Plan"), SQL])
    payload, _ = chat(service, "now by plan")
    assert payload["status"] == "ok" and len(store.views) == 1


# ------------------------------------------------------------------ chart changes
def test_chart_change_by_chip_reuses_the_view_and_skips_the_llm():
    service, llm, store, powerbi = make([REPORT, SQL])
    first, _ = chat(service, "monthly revenue")
    calls_before = len(llm.calls)
    payload, status = chat(service, "Show it as a pie chart", first["session_id"],
                           action="change_chart", visual_type="pieChart")
    assert status == 200 and len(llm.calls) == calls_before and len(store.views) == 1
    assert powerbi.pages[-1] == {"title": "Monthly Revenue - Pie chart", "view": store.views[0][0],
                                 "visual": "pieChart", "reuse": True}
    assert "Report generated" in payload["message"] and "Pie chart" in payload["message"]


def test_chart_change_by_text_and_repeated_changes_keep_a_clean_title():
    service, _, _, powerbi = make([REPORT, SQL, plan_json("change_chart", chart_type="pie"),
                                   plan_json("change_chart", chart_type="donut")])
    first, _ = chat(service, "monthly revenue")
    chat(service, "make it a pie", first["session_id"])
    chat(service, "actually a donut", first["session_id"])
    assert [p["title"] for p in powerbi.pages[1:]] == ["Monthly Revenue - Pie chart", "Monthly Revenue - Donut chart"]


def test_chart_change_without_report_is_a_friendly_reply():
    service, _, _, powerbi = make([plan_json("change_chart", chart_type="pie")])
    payload, status = chat(service, "make it a pie")
    assert status == 200 and payload["status"] == "chat" and not powerbi.pages
    payload, _ = chat(service, "x", payload["session_id"], action="change_chart", visual_type="pieChart")
    assert "don't have a report" in payload["message"]


def test_explicit_chart_in_new_report_and_unsupported_chart_note():
    service, _, _, powerbi = make([plan_json("new_report", request="Revenue", title="Revenue", chart_type="pie"), SQL])
    payload, _ = chat(service, "revenue as a pie chart")
    assert powerbi.pages[0]["visual"] == "pieChart"


# ------------------------------------------------------------------ clarification
def test_clarify_once_then_proceed_even_if_model_asks_again():
    service, llm, store, _ = make([
        plan_json("clarify", reply="Which period?"),
        plan_json("clarify", reply="Which status?"),          # budget exhausted: must proceed
        SQL])
    first, _ = chat(service, "revenue")
    assert first["status"] == "clarify" and first["message"] == "Which period?"
    assert next(iter(store.rows.values()))["request_status"] == "initiated"      # waiting for the answer
    second, _ = chat(service, "all time", first["session_id"])
    assert second["status"] == "ok" and len(store.rows) == 1            # same audit row reused
    assert next(iter(store.rows.values()))["request_status"] == "completed"
    assert "clarification budget left for this request: 0" in llm.calls[1][0].lower()


def test_clarify_answer_is_understood_in_context():
    service, llm, _, _ = make([plan_json("clarify", reply="Which period?"),
                               plan_json("new_report", request="Revenue for 2025", title="Revenue 2025"), SQL])
    first, _ = chat(service, "revenue")
    chat(service, "2025", first["session_id"])
    system, messages = llm.calls[1]
    assert "just asked the user a clarifying question" in system
    assert [m["content"] for m in messages if m["role"] == "user"] == ["revenue", "2025"]


# ------------------------------------------------------------------ failures keep the chat alive
def test_llm_outage_is_friendly_and_history_stays_consistent():
    service, llm, _, _ = make([RuntimeError("429 rate limit"), plan_json("chat", reply="Back!")])
    payload, status = chat(service, "hello")
    assert status == 500 and payload["status"] == "error" and "try again" in payload["message"]
    sid = payload["session_id"]
    ok, status = chat(service, "hello again", sid)
    assert status == 200 and ok["message"] == "Back!"
    roles = [m["role"] for m in llm.calls[-1][1]]
    assert roles == ["user", "assistant", "user"]


def test_sql_failure_reports_error_and_resets_the_thread():
    service, _, store, _ = make([REPORT, "nope", "nope", plan_json("chat", reply="ok")])
    payload, status = chat(service, "monthly revenue")
    assert status == 400 and payload["status"] == "failed" and "rephrase" in payload["message"]
    assert next(iter(store.rows.values()))["request_status"] == "failed" and not store.views


def test_empty_result_is_called_out():
    service, _, store, powerbi = make([REPORT, SQL], db=FakeDB(rows=[]))
    payload, _ = chat(service, "monthly revenue")
    assert payload["status"] == "no_data" and "no rows" in payload["message"]
    assert not store.views and not powerbi.pages                  # no blank page is built


def test_powerbi_disabled_and_page_error_messages():
    service, _, _, _ = make([REPORT, SQL], powerbi=FakePowerBI(enabled=False))
    payload, _ = chat(service, "monthly revenue")
    assert "turned off" in payload["message"] and "Refresh" not in payload["message"]

    class Broken(FakePowerBI):
        def build_page(self, *a, **k):
            raise RuntimeError("model folder missing")
    service, _, _, _ = make([REPORT, SQL], powerbi=Broken())
    payload, status = chat(service, "monthly revenue")
    assert status == 200 and "couldn't create the Power BI page" in payload["message"]


# ------------------------------------------------------------------ surviving a Power BI reload
def test_transcript_restores_the_whole_conversation_and_supports_polling():
    service, _, _, _ = make([plan_json("chat", reply="Hi"), plan_json("chat", reply="Sure")])
    a, _ = chat(service, "hello")
    sid = a["session_id"]
    chat(service, "what can you do?", sid)
    full, status = service.transcript(sid)
    assert status == 200 and [m["role"] for m in full["messages"]] == ["user", "assistant"] * 2
    ids = [m["id"] for m in full["messages"]]
    tail, _ = service.transcript(sid, after=ids[1])
    assert [m["id"] for m in tail["messages"]] == ids[2:]
    assert service.transcript("missing")[1] == 404


def test_reload_runs_in_background_after_the_reply_and_reports_back():
    jobs = []
    service, _, _, powerbi = make([REPORT, SQL], powerbi=FakePowerBI(automation=True), background=jobs.append)
    payload, _ = chat(service, "monthly revenue")
    assert "Report generated" in payload["message"] and payload["busy"] is True
    assert powerbi.reloads == []                                  # reply returned BEFORE the reload
    jobs[0]()                                                     # reload happens afterwards
    assert powerbi.reloads == ["Monthly Revenue"]
    tail, _ = service.transcript(payload["session_id"], after=payload["messages"][-1]["id"])
    assert tail["busy"] is False and tail["messages"][0]["kind"] == "status"
    assert "refreshed" in tail["messages"][0]["text"]


def test_background_reload_failure_tells_the_user_what_to_do():
    jobs = []
    service, _, _, _ = make([REPORT, SQL], powerbi=FakePowerBI(automation=True, fail_reload=True),
                            background=jobs.append)
    payload, _ = chat(service, "monthly revenue")
    jobs[0]()
    tail, _ = service.transcript(payload["session_id"], after=payload["messages"][-1]["id"])
    assert tail["messages"][0]["kind"] == "error" and "click Refresh" in tail["messages"][0]["text"]


def test_new_chat_reset_and_latest_session():
    service, _, _, _ = make([plan_json("chat", reply="Hi")])
    payload, _ = chat(service, "hello")
    assert service.latest()[0]["session_id"] == payload["session_id"]
    result = service.reset(payload["session_id"])[0]
    assert result["ended"] is True and result["deleted"] is False          # ended, never deleted
    assert service.latest()[1] == 404                                      # an ended session is not resumed
    assert service.transcript(payload["session_id"])[0]["messages"]        # ...but its history is kept


# ------------------------------------------------------------------ validation + HTTP layer
def test_input_validation():
    service, _, _, _ = make([])
    assert service.handle_chat({})[1] == 400
    assert service.handle_chat({"message": "x" * 2001})[1] == 400
    assert service.handle_chat({"message": "hi", "visual_type": "radar"})[1] == 400
    assert service.handle_chat({"message": "hi", "action": "drop"})[1] == 400
    assert service.handle_chat({"message": "hi", "session_id": "s" * 65})[1] == 400


def test_http_routes_and_cors():
    llm = ScriptedLLM([plan_json("chat", reply="Hello!")])
    app = create_app(cfg=json.loads(json.dumps(CFG)), llm=llm, db=FakeDB(), store=FakeStore(),
                     powerbi=FakePowerBI())
    client = app.test_client()
    res = client.post("/chat", json={"message": "hi"})
    body = res.get_json()
    assert res.status_code == 200 and body["reply"] == "Hello!"
    assert res.headers["Access-Control-Allow-Origin"] == "*"
    sid = body["session_id"]
    assert client.get(f"/session/{sid}/transcript?after=0").get_json()["last_id"] == 2
    assert client.get("/session/latest").get_json()["session_id"] == sid
    assert client.get(f"/session/{sid}").get_json()["messages"][0]["content"] == "hi"
    pre = client.options("/chat", headers={"Access-Control-Request-Private-Network": "true"})
    assert pre.headers["Access-Control-Allow-Private-Network"] == "true"
    reset = client.post(f"/session/{sid}/reset").get_json()
    assert reset["ended"] is True and reset["deleted"] is False
    assert client.get("/health").get_json()["mode"] == "conversational"
    assert client.post("/revise-report", json={"session_id": sid, "visual_type": "pieChart"}).status_code == 409


# ------------------------------------------------------------------ readiness check (original behaviour)
VAGUE = plan_json("new_report", request="Monthly gross revenue", title="Monthly Gross Revenue",
                  confidence=20, sufficient=False,
                  clarification="What would you like to see? For example revenue by month, refunds by reason, or subscriptions by plan?")


def test_vague_request_is_clarified_not_guessed():
    """'generate a report' has no metric: it must produce a question, never a report."""
    service, _, store, powerbi = make([VAGUE])
    payload, status = chat(service, "generate a report")
    assert status == 200 and payload["status"] == "clarify"
    assert payload["message"].startswith("What would you like to see?")
    assert not store.views and not powerbi.pages


def test_very_low_score_is_asked_about_even_when_the_cap_is_used_up():
    service, _, store, _ = make([VAGUE, VAGUE, VAGUE])
    sid = None
    for _ in range(3):                                   # cap is 1, yet it keeps asking
        payload, _ = chat(service, "uh, a report", sid)
        sid = payload["session_id"]
        assert payload["status"] == "clarify"
    assert not store.views


def test_missing_period_asks_then_builds_after_the_answer():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False,
                    clarification="What time period would you like to see?")
    ready = plan_json("new_report", request="Revenue for 2025", title="Revenue 2025", confidence=92, sufficient=True)
    service, _, store, _ = make([ask, ready, SQL])
    first, _ = chat(service, "show me revenue")
    assert first["status"] == "clarify" and first["message"] == "What time period would you like to see?"
    second, _ = chat(service, "2025", first["session_id"])
    assert second["status"] == "ok" and len(store.views) == 1 and len(store.rows) == 1


def test_not_sufficient_above_floor_proceeds_once_the_cap_is_reached():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    service, _, store, _ = make([ask, ask, SQL])         # cap = 1
    first, _ = chat(service, "revenue")
    assert first["status"] == "clarify"
    second, _ = chat(service, "whatever you think", first["session_id"])
    assert second["status"] == "ok" and len(store.views) == 1


def test_unlimited_cap_keeps_asking():
    cfg = json.loads(json.dumps(CFG)); cfg["pipeline"]["max_clarifications"] = 0; cfg["chat"] = {"ask_chart_type": False}
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    llm, store = ScriptedLLM([ask] * 3), FakeStore()
    service = ChatService(cfg, Pipeline(llm, FakeDB(), cfg, base_dir="."), store, FakePowerBI())
    sid = None
    for _ in range(3):
        payload, _ = chat(service, "revenue", sid)
        sid = payload["session_id"]
        assert payload["status"] == "clarify"


def test_clear_request_is_still_built_without_questions():
    service, _, store, _ = make([plan_json("new_report", request="Revenue by plan in 2026", title="Revenue By Plan",
                                           confidence=95, sufficient=True), SQL])
    payload, _ = chat(service, "revenue by plan in 2026")
    assert payload["status"] == "ok" and len(store.views) == 1


def test_follow_up_on_the_last_report_is_not_re_clarified():
    service, _, store, _ = make([REPORT, SQL,
                                 plan_json("modify_report", request="Monthly gross revenue for 2025",
                                           title="Revenue 2025", confidence=93, sufficient=True), SQL])
    first, _ = chat(service, "monthly gross revenue for the last 12 months")
    second, _ = chat(service, "same but 2025", first["session_id"])
    assert second["status"] == "ok" and len(store.views) == 2


# ------------------------------------------------------------------ chart type is the LAST question
READY = plan_json("new_report", reply="Refunds by reason for 2026.", request="Refunds by reason in 2026",
                  title="Refunds by Reason - 2026", confidence=95, sufficient=True)


def chips_of(payload):
    return [c["label"] for c in payload["messages"][-1]["meta"]["suggestions"]]


def test_chart_question_comes_after_the_request_is_ready_and_runs_no_sql_yet():
    service, llm, store, powerbi = make([READY], ask_chart=True)
    payload, status = chat(service, "refunds by reason for 2026")
    assert status == 200 and payload["status"] == "awaiting_chart_choice"
    assert payload["message"].endswith(CHART_QUESTION) and "Refunds by reason for 2026." in payload["message"]
    assert chips_of(payload) == ["Auto", "Column chart", "Bar chart", "Line chart", "Pie chart",
                                 "Donut chart", "Table only"]
    assert len(llm.calls) == 1 and not store.views and not powerbi.pages     # SQL has not run
    assert next(iter(store.rows.values()))["request_status"] == "initiated"


def test_typed_chart_answer_builds_the_report_and_runs_the_sql_exactly_once():
    service, llm, store, powerbi = make([READY, SQL], ask_chart=True)
    first, _ = chat(service, "refunds by reason for 2026")
    second, status = chat(service, "pie chart please", first["session_id"])
    assert status == 200 and second["status"] == "ok"
    assert powerbi.pages[0]["visual"] == "pieChart" and powerbi.pages[0]["title"] == "Refunds by Reason - 2026"
    assert len(llm.calls) == 2 and len(store.views) == 1 and len(store.rows) == 1   # 1 planner + 1 SQL call
    assert next(iter(store.rows.values()))["request_status"] == "completed"


def test_chart_chip_click_and_auto_choice():
    service, _, store, powerbi = make([READY, SQL], ask_chart=True)
    first, _ = chat(service, "refunds by reason for 2026")
    second, _ = chat(service, "Auto", first["session_id"], action="choose_chart", visual_type="auto")
    assert second["status"] == "ok" and powerbi.pages[0]["visual"] == "lineChart"   # fake picks lineChart for auto
    assert len(store.views) == 1


def test_all_clarifications_come_first_and_the_chart_question_is_last():
    ask = plan_json("new_report", request="Refunds", confidence=70, sufficient=False,
                    clarification="Which period?")
    service, llm, store, _ = make([ask, READY, SQL], ask_chart=True)
    first, _ = chat(service, "show refunds")
    assert first["status"] == "clarify" and first["message"] == "Which period?"
    assert "suggestions" not in first["messages"][-1]["meta"] or not first["messages"][-1]["meta"]["suggestions"]
    second, _ = chat(service, "2026", first["session_id"])
    assert second["status"] == "awaiting_chart_choice"                 # only now
    third, _ = chat(service, "donut", first["session_id"])
    assert third["status"] == "ok" and len(store.rows) == 1             # one audit row for the whole thread


def test_a_chart_named_in_the_request_skips_the_question():
    named = plan_json("new_report", request="Refunds by reason in 2026", title="Refunds", chart_type="pie",
                      confidence=95, sufficient=True)
    service, _, _, powerbi = make([named, SQL], ask_chart=True)
    payload, _ = chat(service, "refunds by reason 2026 as a pie chart")
    assert payload["status"] == "ok" and powerbi.pages[0]["visual"] == "pieChart"


def test_a_revision_keeps_the_current_chart_and_does_not_ask_again():
    modify = plan_json("modify_report", request="Refunds by reason in 2025", title="Refunds 2025",
                       confidence=94, sufficient=True)
    service, _, _, powerbi = make([READY, SQL, modify, SQL], ask_chart=True)
    a, _ = chat(service, "refunds by reason for 2026")
    b, _ = chat(service, "donut chart", a["session_id"])
    c, _ = chat(service, "same but 2025", a["session_id"])
    assert c["status"] == "ok" and powerbi.pages[-1]["visual"] == "donutChart"


def test_side_questions_while_waiting_are_answered_and_the_choice_stays_open():
    service, _, store, _ = make([READY, plan_json("chat", reply="Column is best for comparing reasons."), SQL],
                                ask_chart=True)
    a, _ = chat(service, "refunds by reason for 2026")
    b, _ = chat(service, "which chart is best for this, and why would it matter?", a["session_id"])
    assert b["status"] == "chat" and "Column is best" in b["message"] and b["message"].endswith(CHART_QUESTION)
    c, _ = chat(service, "column", a["session_id"])
    assert c["status"] == "ok" and len(store.views) == 1


def test_changing_the_request_while_waiting_replaces_the_pending_choice():
    changed = plan_json("new_report", request="Refunds by reason in 2025", title="Refunds 2025",
                        confidence=95, sufficient=True)
    service, _, store, powerbi = make([READY, changed, SQL], ask_chart=True)
    a, _ = chat(service, "refunds by reason for 2026")
    b, _ = chat(service, "actually make that the year 2025 instead of 2026 please", a["session_id"])
    assert b["status"] == "awaiting_chart_choice" and "2025" in service.sessions.get(a["session_id"]).pending_chart["request"]
    c, _ = chat(service, "line", a["session_id"])
    assert c["status"] == "ok" and len(store.views) == 1 and powerbi.pages[0]["visual"] == "lineChart"


def test_stale_chart_chip_without_a_pending_question_is_harmless():
    service, _, store, _ = make([], ask_chart=True)
    payload, status = chat(service, "Pie chart", action="choose_chart", visual_type="pieChart")
    assert status == 200 and "not waiting on a chart choice" in payload["message"] and not store.views


def test_sql_failure_after_the_chart_choice_ends_the_request_cleanly():
    service, _, store, _ = make([READY, "nope", "nope"], ask_chart=True)
    a, _ = chat(service, "refunds by reason for 2026")
    b, status = chat(service, "pie", a["session_id"])
    assert status == 400 and b["status"] == "failed"
    assert service.sessions.get(a["session_id"]).pending_chart is None and not store.views


def test_chart_chips_survive_in_the_transcript_for_restore():
    service, _, _, _ = make([READY], ask_chart=True)
    a, _ = chat(service, "refunds by reason for 2026")
    full, _ = service.transcript(a["session_id"])
    assert full["messages"][-1]["meta"]["suggestions"][0]["action"] == "choose_chart"


@pytest.mark.parametrize("text,expected", [
    ("pie", "pieChart"), ("Pie chart please", "pieChart"), ("donut", "donutChart"), ("doughnut chart", "donutChart"),
    ("column", "clusteredColumnChart"), ("vertical bars", "clusteredColumnChart"),
    ("horizontal bar", "barChart"), ("bar chart", "barChart"), ("line", "lineChart"), ("trend line", "lineChart"),
    ("table only", "tableEx"), ("Auto", "auto"), ("you choose", "auto"), ("whatever", "auto"),
    ("hmm what do you recommend", None), ("2025", None),
    ("show me refunds by reason for the year 2025 as a pie chart instead", None)])
def test_detect_chart_choice(text, expected):
    assert detect_chart_choice(text) == expected
