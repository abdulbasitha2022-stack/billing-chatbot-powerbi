"""Pipeline tests: planner parsing, SQL stage retries, day rollover. No network or database."""
import json
from datetime import date

import pytest

from app.pipeline import (Pipeline, PlannerError, normalize_chart_type, parse_plan, parse_response)
from app.access import AccessController
from tests.fakes import FakeDB, ScriptedLLM, plan_json

CFG = {"db": {"allowed_tables": ["charges"], "max_rows": 1000},
       "context": {"files": [], "extra_rules": "test rule"},
       "pipeline": {"max_retries": 2, "log_file": "log.jsonl"}}
SQL = "```sql\nSELECT DATE(created_at) AS d, SUM(amount_cents)/100.0 AS revenue_usd FROM charges GROUP BY 1\n```"
CEO_ACCESS = AccessController([{"role": "CEO", "resource_type": "table", "resource_name": "charges", "allowed": True}])


def make(replies, tmp_path, db=None):
    llm, db = ScriptedLLM(replies), db or FakeDB()
    return Pipeline(llm, db, json.loads(json.dumps(CFG)), base_dir=tmp_path), llm, db


# ------------------------------------------------------------------ SQL reply parsing
def test_parse_sql_with_assumptions():
    p = parse_response("ASSUMPTION: last 6 months\n```sql\nSELECT 1\n```")
    assert p.kind == "sql" and p.sql == "SELECT 1" and p.assumptions == ["last 6 months"]


def test_parse_clarify_and_garbage():
    assert parse_response("CLARIFY: Which year?").kind == "clarify"
    assert parse_response("Sure! Happy to help.").kind == "invalid"
    assert parse_response("SELECT 1").kind == "sql"


# ------------------------------------------------------------------ planner parsing
def test_parse_plan_plain_and_fenced():
    plan = parse_plan(plan_json("new_report", request="Monthly revenue", title="Monthly Revenue"))
    assert plan.intent == "new_report" and plan.request == "Monthly revenue"
    fenced = parse_plan("Sure!\n```json\n" + plan_json("chat", reply="Hi!") + "\n```")
    assert fenced.intent == "chat" and fenced.reply == "Hi!"


def test_parse_plan_is_tolerant():
    plan = parse_plan('{"intent": "New Report", "request": "x", "confidence": "85.0", "chart_type": "doughnut"}')
    assert plan.intent == "new_report" and plan.confidence == 85 and plan.chart_type == "donutChart"
    assert parse_plan('{"reply": "hello"}').intent == "chat"
    assert parse_plan('{"request": "revenue by plan"}').intent == "new_report"


def test_parse_plan_rejects_garbage():
    for bad in ("", "no json here", "[]", '{"intent": "x"}'):
        with pytest.raises(ValueError):
            parse_plan(bad)


@pytest.mark.parametrize("raw,expected", [
    ("pie", "pieChart"), ("Pie Chart", "pieChart"), ("column", "clusteredColumnChart"),
    ("horizontal bar", "barChart"), ("line", "lineChart"), ("table", "tableEx"),
    ("auto", "auto"), (None, "auto"), ("sparkline", None)])
def test_normalize_chart_type(raw, expected):
    assert normalize_chart_type(raw) == expected


# ------------------------------------------------------------------ plan()
def test_plan_retries_once_on_bad_json(tmp_path):
    pipe, llm, _ = make(["not json", plan_json("chat", reply="Hi")], tmp_path)
    plan, usage = pipe.plan([{"role": "user", "content": "hi"}], "state")
    assert plan.reply == "Hi" and len(llm.calls) == 2 and usage.input_tokens == 200
    roles = [m["role"] for m in llm.calls[1][1]]
    assert roles == ["user", "assistant", "user"]


def test_plan_gives_up_after_two_bad_replies(tmp_path):
    pipe, _, _ = make(["nope", "still nope"], tmp_path)
    with pytest.raises(PlannerError):
        pipe.plan([{"role": "user", "content": "hi"}])


def test_plan_wraps_llm_failure(tmp_path):
    pipe, _, _ = make([RuntimeError("rate limited")], tmp_path)
    with pytest.raises(PlannerError, match="rate limited"):
        pipe.plan([{"role": "user", "content": "hi"}])


def test_planner_prompt_contains_state_and_today(tmp_path):
    pipe, _, _ = make([], tmp_path)
    prompt = pipe.planner_system("LAST REPORT: demo-state")
    assert "LAST REPORT: demo-state" in prompt and date.today().isoformat() in prompt
    assert "__" not in prompt.replace("__init__", "")


# ------------------------------------------------------------------ generate_sql()
def test_generate_sql_happy_path_and_log(tmp_path):
    pipe, llm, db = make(["ASSUMPTION: all time\n" + SQL], tmp_path)
    result = pipe.generate_sql("Daily revenue", assumptions=["period: all time"], role="CEO", access=CEO_ACCESS)
    assert result.status == "ok" and result.rows and "LIMIT" in db.ran[0].upper()
    assert result.assumptions == ["all time"]
    first_user = llm.calls[0][1][0]["content"]
    assert "Report request: Daily revenue" in first_user and "period: all time" in first_user
    assert (tmp_path / "log.jsonl").read_text().count("\n") == 1


def test_generate_sql_includes_previous_sql_for_revisions(tmp_path):
    pipe, llm, _ = make([SQL], tmp_path)
    pipe.generate_sql("Same but 2025", previous={"request": "Daily revenue", "sql": "SELECT 1 AS old_marker"},
                      role="CEO", access=CEO_ACCESS)
    content = llm.calls[0][1][0]["content"]
    assert "old_marker" in content and "Daily revenue" in content


def test_generate_sql_retries_on_db_error_with_local_messages(tmp_path):
    db = FakeDB(fail_first="Unknown column 'x'")
    pipe, llm, _ = make([SQL, SQL], tmp_path, db)
    result = pipe.generate_sql("Daily revenue", role="CEO", access=CEO_ACCESS)
    assert result.status == "ok" and result.attempts == 2
    assert "Unknown column 'x'" in llm.calls[1][1][-1]["content"]


def test_generate_sql_clarify_and_failure(tmp_path):
    pipe, _, _ = make(["CLARIFY: Which year?"], tmp_path)
    assert pipe.generate_sql("revenue", role="CEO", access=CEO_ACCESS).status == "clarify"
    pipe, _, _ = make(["nonsense"] * 3, tmp_path)
    failed = pipe.generate_sql("revenue", role="CEO", access=CEO_ACCESS)
    assert failed.status == "failed" and failed.attempts == 3


def test_generate_sql_llm_error(tmp_path):
    pipe, _, _ = make([RuntimeError("boom")], tmp_path)
    result = pipe.generate_sql("revenue", role="CEO", access=CEO_ACCESS)
    assert result.status == "error" and "boom" in result.error


def test_system_prompt_refreshes_when_the_day_changes(tmp_path, monkeypatch):
    import app.pipeline as module
    pipe, _, _ = make([], tmp_path)
    assert date.today().isoformat() in pipe.system

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date(2031, 1, 2)
    monkeypatch.setattr(module, "date", Tomorrow)
    assert "2031-01-02" in pipe.system


# ------------------------------------------------------------------ ask() (CLI)
def test_ask_report_chat_and_clarify(tmp_path):
    pipe, _, _ = make([plan_json("new_report", request="Daily revenue"), SQL], tmp_path)
    assert pipe.ask("daily revenue", role="CEO", access=CEO_ACCESS).status == "ok"
    pipe, _, _ = make([plan_json("chat", reply="Hello!")], tmp_path)
    res = pipe.ask("hi")
    assert res.status == "chat" and res.clarification == "Hello!"
    pipe, _, _ = make([plan_json("clarify", reply="Which period?")], tmp_path)
    assert pipe.ask("revenue").status == "clarify"


def test_parse_plan_readiness_fields():
    plan = parse_plan('{"intent": "new_report", "request": "x", "score": 35, "sufficient": "false", '
                      '"clarification": "Which period?"}')
    assert plan.scored and plan.confidence == 35 and plan.sufficient is False
    assert plan.clarification == "Which period?"
    assert parse_plan('{"intent": "clarify", "reply": "q?"}').sufficient is False      # derived
    assert parse_plan('{"intent": "chat", "reply": "hi"}').scored is False             # no score given
    assert parse_plan('{"intent": "chat", "reply": "hi", "confidence": 88}').confidence == 88  # old key


def test_planner_prompt_contains_the_rubric_and_the_vague_request_rule(tmp_path):
    pipe, _, _ = make([], tmp_path)
    prompt = pipe.planner_system("state")
    assert "Business intent / objective" in prompt and "generate a report" in prompt
    assert "Never invent a topic" in prompt and "Prefer a sensible default" not in prompt
