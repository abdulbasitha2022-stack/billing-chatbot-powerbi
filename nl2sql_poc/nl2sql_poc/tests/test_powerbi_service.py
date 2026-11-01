"""PowerBIService logic with PowerShell replaced by a recorder (no Power BI needed)."""
import json
import threading
import time

import pytest

from app.powerbi_project_generator import PowerBIProjectError
from app.powerbi_service import PowerBIService
from app.pipeline import Pipeline
from chatbot_api import ChatService
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json
from tests.test_powerbi_generator import skeleton


def make_service(tmp_path, automation=True):
    report, model = skeleton(tmp_path)
    cfg = {"powerbi": {"enabled": True, "report_dir": str(report), "model_dir": str(model),
                       "desktop_automation": {"enabled": automation, "timeout_seconds": 5}}}
    service = PowerBIService(cfg, tmp_path, FakeStore())
    calls = []

    def fake_run(action, table_name=None):
        calls.append(action)
        if action == "prepare" and getattr(service, "slow_prepare", 0):
            time.sleep(service.slow_prepare)
        if action in service.failing:
            raise PowerBIProjectError(f"{action} failed")
        return {"success": True, "timings": {"x": 1}}

    service.failing, service.run_desktop_automation = set(), fake_run
    return service, calls


def test_background_prepare_is_reused_and_not_repeated(tmp_path):
    service, calls = make_service(tmp_path)
    service.start_prepare()
    service.start_prepare()                                   # second call while fresh: ignored
    out = service.build_page("vw_a_1", "Revenue")
    assert out["page_name"] == "Revenue" and calls == ["prepare"]


def test_without_background_prepare_it_runs_once_synchronously(tmp_path):
    service, calls = make_service(tmp_path)
    service.build_page("vw_a_1", "Revenue")
    assert calls == ["prepare"]


def test_build_page_waits_for_a_slow_background_prepare(tmp_path):
    service, calls = make_service(tmp_path)
    service.slow_prepare = 0.3
    service.start_prepare()
    started = time.perf_counter()
    service.build_page("vw_a_1", "Revenue")
    assert time.perf_counter() - started >= 0.25 and calls == ["prepare"]


def test_prepare_failure_surfaces_and_blocks_file_writes(tmp_path):
    service, calls = make_service(tmp_path)
    service.failing = {"prepare"}
    service.start_prepare()
    with pytest.raises(PowerBIProjectError, match="prepare failed"):
        service.build_page("vw_a_1", "Revenue")
    report_dir = tmp_path / "demo.Report" / "definition" / "pages"
    assert len(json.loads((report_dir / "pages.json").read_text())["pageOrder"]) == 1   # nothing written


def test_stale_background_prepare_is_repeated(tmp_path, monkeypatch):
    service, calls = make_service(tmp_path)
    service.start_prepare()
    service._prep["done"].wait(2)
    service._prep["finished_at"] -= 10_000                    # pretend it finished long ago
    service.build_page("vw_a_1", "Revenue")
    assert calls == ["prepare", "prepare"]


def test_prepare_is_skipped_when_automation_is_off(tmp_path):
    service, calls = make_service(tmp_path, automation=False)
    service.start_prepare()
    service.build_page("vw_a_1", "Revenue")
    assert calls == []


# ------------------------------------------------------------------ apply()
def test_new_table_gets_the_full_reload_and_refresh(tmp_path):
    service, calls = make_service(tmp_path)
    project = service.build_page("vw_a_1", "Revenue")
    calls.clear()
    result = service.apply(project)
    assert calls == ["reload_refresh"] and result["mode"] == "full" and result["seconds"] >= 0


def test_chart_change_reloads_only_the_report(tmp_path):
    service, calls = make_service(tmp_path)
    service.build_page("vw_a_1", "Revenue")
    project = service.build_page("vw_a_1", "Revenue - Pie", "pieChart", reuse_existing_table=True)
    calls.clear()
    result = service.apply(project)
    assert calls == ["reload_report"] and result["mode"] == "page_only"


def test_chart_change_falls_back_to_the_full_update_if_desktop_rejects_it(tmp_path):
    service, calls = make_service(tmp_path)
    service.build_page("vw_a_1", "Revenue")
    project = service.build_page("vw_a_1", "Revenue - Pie", "pieChart", reuse_existing_table=True)
    calls.clear()
    service.failing = {"reload_report"}
    result = service.apply(project)
    assert calls == ["reload_report", "reload_refresh"] and result["mode"] == "page_only_fallback_full"


def test_total_failure_is_raised_so_the_chat_can_tell_the_user(tmp_path):
    service, _ = make_service(tmp_path)
    project = service.build_page("vw_a_1", "Revenue")
    service.failing = {"reload_refresh"}
    with pytest.raises(PowerBIProjectError):
        service.apply(project)


# ------------------------------------------------------------------ chat level
CFG = {"db": {"allowed_tables": ["charges"], "max_rows": 1000}, "context": {"files": []},
       "pipeline": {"max_retries": 1}, "chat": {"ask_chart_type": False}}
SQL = "```sql\nSELECT month_start, revenue_usd FROM charges\n```"
REPORT = plan_json("new_report", request="Revenue", title="Revenue", confidence=95, sufficient=True)


def test_saving_desktop_starts_when_sql_generation_starts_and_messages_match_the_update_mode():
    jobs = []
    cfg = json.loads(json.dumps(CFG))
    powerbi, store = FakePowerBI(automation=True), FakeStore()
    llm = ScriptedLLM([REPORT, SQL, plan_json("change_chart", chart_type="pie")])
    service = ChatService(cfg, Pipeline(llm, FakeDB(), cfg, base_dir="."), store, powerbi, background=jobs.append)
    a, _ = service.handle_chat({"message": "revenue"})
    assert powerbi.prepares == 1                                  # started before the SQL finished
    jobs.pop()()
    service.handle_chat({"message": "make it a pie", "session_id": a["session_id"]})
    jobs.pop()()
    notes = [m["text"] for m in service.transcript(a["session_id"])[0]["messages"] if m["kind"] == "status"]
    assert "reloaded and refreshed “Revenue” in 1.5 s" in notes[0]
    assert "reloaded “Revenue - Pie chart” in 1.5 s" in notes[1] and "refreshed" not in notes[1]
