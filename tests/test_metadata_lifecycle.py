"""report_request_metadata: ONE row per report request, created once, updated through its lifecycle."""
import time

from app.report_store import METADATA_FIELDS
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json
from tests.support import FIXED_NOW, REFUNDS, REPORT, SQL, SlowLLM, chat, make, report

COLUMNS = ["session_id", "request_id", "user_id", "user_role", "platform", "view_name", "request_status",
           "sql_source", "response_time_ms", "customer_satisfaction", "request_ts", "error_message", "day_id"]


def statuses(store, request_id):
    return [fields["request_status"] for rid, fields in store.updates
            if rid == request_id and "request_status" in fields]


def test_a_session_can_generate_several_reports_with_distinct_request_ids():
    service, _, store, _ = make([REPORT, SQL, REFUNDS, SQL])
    a, sid = report(service, "monthly revenue")
    b, _ = report(service, "refunds by reason", sid)
    assert a["request_id"] != b["request_id"] and len(store.rows) == 2
    assert {r["session_id"] for r in store.rows.values()} == {sid}          # session_id is NOT unique
    assert {r["request_status"] for r in store.rows.values()} == {"completed"}
    assert len({r["view_name"] for r in store.rows.values()}) == 2


def test_each_report_attempt_gets_its_own_request_id_even_in_one_message_sequence():
    service, _, store, _ = make([REPORT, SQL, REPORT, SQL, REPORT, SQL])
    ids = {report(service, "again")[0]["request_id"] for _ in range(3)}
    assert len(ids) == 3 and store.inserts == 3


def test_one_row_is_created_once_and_updated_through_the_lifecycle():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    service, _, store, _ = make([ask, REPORT, SQL], ask_chart=True)
    a, _ = chat(service, "show revenue")
    rid = a["request_id"]
    assert store.inserts == 1 and store.rows[rid]["request_status"] == "initiated"   # waiting for the answer
    b, _ = chat(service, "2025", a["session_id"])
    assert b["status"] == "awaiting_chart_choice" and store.inserts == 1
    c, _ = chat(service, "pie", a["session_id"])
    assert c["request_id"] == rid and store.inserts == 1                            # same id, never re-inserted
    assert statuses(store, rid) == ["in_progress", "in_progress", "completed"]
    assert store.rows[rid]["request_status"] == "completed"


def test_the_request_id_stays_stable_across_a_clarification_and_the_final_report():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    service, _, store, _ = make([ask, REPORT, SQL])
    a, _ = chat(service, "show revenue")
    b, _ = chat(service, "2025", a["session_id"])
    assert a["request_id"] == b["request_id"] and len(store.rows) == 1


def test_llm_retries_do_not_create_extra_rows():
    service, _, store, _ = make(["not json", REPORT, SQL])           # planner needs a second attempt
    payload, _ = chat(service, "monthly revenue")
    assert payload["status"] == "ok" and store.inserts == 1


def test_successful_request_records_the_final_view_name_everywhere():
    service, _, store, _ = make([REPORT, SQL])
    payload, _ = report(service)
    row = store.rows[payload["request_id"]]
    final = store.views[0][0]
    assert row["view_name"] == final == payload["view_name"] == store.logged[0][3]
    assert f"`billing {final}`" in payload["message"]
    assert row["request_status"] == "completed" and row["error_message"] is None


def test_the_row_has_exactly_the_13_columns_and_no_chat_messages():
    service, _, store, _ = make([REPORT, SQL])
    payload, _ = report(service)
    row = store.rows[payload["request_id"]]
    assert list(row) == COLUMNS and COLUMNS[0] == "session_id" and COLUMNS[-1] == "day_id"
    assert not {"message", "user_message", "assistant_message", "user_request"} & set(row)
    assert "monthly revenue" not in str(list(row.values())).lower()      # the chat text is not stored here
    assert METADATA_FIELDS <= set(COLUMNS)


def test_failed_sql_generation_is_recorded_with_an_error_message():
    service, _, store, _ = make([REPORT, "nope", "nope"])
    payload, status = chat(service, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert status == 400 and row["request_status"] == "failed"
    assert row["error_message"].startswith("Query generation failed") and row["view_name"] is None
    assert row["response_time_ms"] is not None and not store.views


def test_technical_error_is_recorded_as_failed():
    service, _, store, _ = make([REPORT, RuntimeError("upstream down")])
    payload, status = chat(service, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert status == 500 and row["request_status"] == "failed" and "Technical error" in row["error_message"]
    assert "upstream down" not in payload["message"]                 # internals are not sent to the visual


def test_page_failure_after_the_view_exists_is_failed_but_keeps_the_view_name():
    class Broken(FakePowerBI):
        def build_page(self, *a, **k):
            raise RuntimeError("secret path C:\\private\\model")
    service, _, store, _ = make([REPORT, SQL], powerbi=Broken())
    payload, status = chat(service, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert status == 200 and row["request_status"] == "failed"
    assert row["view_name"] == store.views[0][0] and "Power BI page was not created" in row["error_message"]
    assert "private" not in payload["message"] and "private" not in str(payload["powerbi_project"])
    assert payload["messages"][-1]["meta"]["feedback_enabled"] is False


def test_powerbi_generation_disabled_still_completes_the_request():
    service, _, store, _ = make([REPORT, SQL], powerbi=FakePowerBI(enabled=False))
    payload, _ = chat(service, "monthly revenue")
    assert store.rows[payload["request_id"]]["request_status"] == "completed"
    assert payload["messages"][-1]["meta"]["feedback_enabled"] is False   # no page was displayed


def test_response_time_counts_processing_across_turns_but_not_idle_time():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    llm = SlowLLM([ask, REPORT, SQL], delay=0.04)
    service, _, store, _ = make([], llm=llm)
    a, _ = chat(service, "show revenue")
    time.sleep(0.4)                                                    # the user thinking
    b, _ = chat(service, "2025", a["session_id"])
    ms = store.rows[b["request_id"]]["response_time_ms"]
    assert 100 <= ms < 380, ms                                         # ~3 slow calls, not the 0.4 s pause
    assert b["response_time_ms"] == ms


def test_day_id_and_request_ts_follow_the_application_time_zone():
    india, _, store, _ = make([REPORT, SQL], clock=lambda: FIXED_NOW, tz="Asia/Kolkata")
    payload, _ = chat(india, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert str(row["day_id"]) == "2026-10-27"                          # 01:30 IST on the 27th
    assert str(row["request_ts"]) == "2026-10-26 20:00:00"             # stored in UTC (naive)
    assert "_20261027_" in row["view_name"]
    utc, _, store2, _ = make([REPORT, SQL], clock=lambda: FIXED_NOW, tz="UTC")
    payload2, _ = chat(utc, "monthly revenue")
    row2 = store2.rows[payload2["request_id"]]
    assert str(row2["day_id"]) == "2026-10-26" and "_20261026_" in row2["view_name"]


def test_a_chart_change_is_its_own_request_on_the_same_view():
    service, _, store, powerbi = make([REPORT, SQL])
    a, sid = report(service)
    b, _ = chat(service, "Pie chart", sid, action="change_chart", visual_type="pieChart")
    assert b["request_id"] != a["request_id"] and len(store.views) == 1 and store.inserts == 2
    row = store.rows[b["request_id"]]
    assert row["sql_source"] == "chart_change" and row["view_name"] == store.views[0][0]
    assert row["request_status"] == "completed" and row["session_id"] == sid


def test_missing_identity_does_not_stop_report_generation():
    service, _, store, _ = make([REPORT, SQL])
    payload, status = chat(service, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert status == 200 and payload["status"] == "ok"
    assert row["user_id"] is None and row["user_role"] is None and row["platform"] == "Power BI"


def test_empty_result_is_recorded_as_failed_without_a_view():
    service, _, store, _ = make([REPORT, SQL], db=FakeDB(rows=[]))
    payload, _ = chat(service, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert payload["status"] == "no_data" and row["request_status"] == "failed" and row["view_name"] is None
