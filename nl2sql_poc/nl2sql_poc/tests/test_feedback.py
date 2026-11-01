"""Satisfied / Unsatisfied: stored on the report request's existing row; NULL until rated."""
import threading

from tests.fakes import FakeStore, plan_json
from tests.support import REFUNDS, REPORT, SQL, chat, make, report

FOLLOW_UP = "Would you like to return to the conversation with the assistant to improve this report?"


def feedback(service, sid, rid, value):
    return service.handle_feedback({"session_id": sid, "request_id": rid, "satisfaction": value})


def report_entry(service, sid):
    return [m for m in service.transcript(sid)[0]["messages"] if m["kind"] == "report"][-1]


def test_buttons_are_offered_only_on_a_successfully_generated_report():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    ready = plan_json("new_report", request="Revenue 2025", title="Revenue 2025", confidence=95, sufficient=True)
    service, _, _, _ = make([ask, ready, SQL], ask_chart=True)
    a, sid = chat(service, "show revenue")[0], None
    sid = a["session_id"]
    b, _ = chat(service, "2025", sid)                               # the chart question
    for early in (a, b):                                            # not during clarification / chart choice
        assert not any((m.get("meta") or {}).get("feedback_enabled") for m in early["messages"])
    c, _ = chat(service, "pie", sid)
    meta = [m for m in c["messages"] if m["kind"] == "report"][0]["meta"]
    assert meta["feedback_enabled"] is True and meta["feedback"] is None and meta["request_id"] == c["request_id"]


def test_satisfaction_starts_as_null_not_false():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    assert store.rows[a["request_id"]]["customer_satisfaction"] is None


def test_satisfied_saves_true_on_the_same_row_and_confirms():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    rid, inserts = a["request_id"], store.inserts
    payload, status = feedback(service, sid, rid, "satisfied")
    assert status == 200 and payload["customer_satisfaction"] is True and payload["changed"] is True
    assert store.rows[rid]["customer_satisfaction"] is True
    assert store.inserts == inserts and len(store.rows) == 1                  # no duplicate record
    assert "recorded as **Satisfied**" in payload["message"]
    assert [m["text"] for m in payload["messages"]] == ["Satisfied", payload["message"]]
    assert payload["updated"][0]["meta"]["feedback"] == "satisfied"
    assert report_entry(service, sid)["meta"]["feedback"] == "satisfied"      # survives a restore


def test_unsatisfied_saves_false_and_asks_the_follow_up_question_with_two_choices():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    payload, _ = feedback(service, sid, a["request_id"], "unsatisfied")
    assert store.rows[a["request_id"]]["customer_satisfaction"] is False       # FALSE, not NULL
    assert FOLLOW_UP in payload["message"]
    chips = payload["messages"][-1]["meta"]["suggestions"]
    assert [(c["label"], c["action"], c["choice"]) for c in chips] == [
        ("Return to conversation", "feedback_followup", "return"),
        ("Finish session", "feedback_followup", "finish")]


def test_changing_feedback_updates_the_same_row_last_click_wins():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    rid = a["request_id"]
    feedback(service, sid, rid, "satisfied")
    second, _ = feedback(service, sid, rid, "unsatisfied")
    assert store.rows[rid]["customer_satisfaction"] is False and "updated your feedback" in second["message"]
    third, _ = feedback(service, sid, rid, "satisfied")
    assert store.rows[rid]["customer_satisfaction"] is True and len(store.rows) == 1 and store.inserts == 1
    assert report_entry(service, sid)["meta"]["feedback"] == "satisfied"


def test_clicking_the_saved_value_again_changes_nothing():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    feedback(service, sid, a["request_id"], "satisfied")
    count = len(service.transcript(sid)[0]["messages"])
    again, status = feedback(service, sid, a["request_id"], "satisfied")
    assert status == 200 and again["status"] == "unchanged" and again["changed"] is False
    assert len(service.transcript(sid)[0]["messages"]) == count               # no extra messages


def test_simultaneous_clicks_end_in_one_consistent_value_and_one_row():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    rid = a["request_id"]
    threads = [threading.Thread(target=feedback, args=(service, sid, rid, v))
               for v in ("satisfied", "unsatisfied", "satisfied", "unsatisfied", "satisfied")]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    entry = report_entry(service, sid)["meta"]["feedback"]
    assert store.rows[rid]["customer_satisfaction"] is (entry == "satisfied")  # UI state == database state
    assert len(store.rows) == 1 and store.inserts == 1


def test_return_to_conversation_keeps_the_session_history_and_report_context():
    service, llm, store, _ = make([REPORT, SQL, plan_json("modify_report", request="Monthly gross revenue for 2025",
                                                          title="Monthly Revenue 2025", confidence=95, sufficient=True), SQL])
    a, sid = report(service)
    feedback(service, sid, a["request_id"], "unsatisfied")
    history_before = list(service.sessions.get(sid).history)
    back, status = chat(service, "Return to conversation", sid, action="feedback_followup", choice="return")
    session = service.sessions.get(sid)
    assert status == 200 and back["session_id"] == sid and back["session_ended"] is False
    assert "What would you like to change" in back["message"]
    assert session.ended is False and session.last_report is not None
    assert session.history[:len(history_before)] == history_before             # conversation preserved
    assert "unsatisfied" in session.history[-1]["content"].lower()             # the assistant remembers why
    labels = [c["label"] for c in back["messages"][-1]["meta"]["suggestions"]]
    assert "Explain this report" in labels
    # the next report gets a NEW request id in the SAME session, and the first rating is untouched
    b, _ = report(service, "same but for 2025", sid)
    assert b["request_id"] != a["request_id"] and b["session_id"] == sid
    assert store.rows[a["request_id"]]["customer_satisfaction"] is False
    assert store.rows[b["request_id"]]["customer_satisfaction"] is None


def test_finish_session_ends_it_and_blocks_further_use():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    feedback(service, sid, a["request_id"], "unsatisfied")
    done, status = chat(service, "Finish session", sid, action="feedback_followup", choice="finish")
    assert status == 200 and done["session_ended"] is True and done["status"] == "session_ended"
    assert service.sessions.get(sid).ended is True
    assert chat(service, "hello?", sid)[1] == 409
    assert feedback(service, sid, a["request_id"], "satisfied")[1] == 409
    assert store.rows[a["request_id"]]["customer_satisfaction"] is False


def test_a_report_without_feedback_stays_null_when_another_is_rated():
    service, _, store, _ = make([REPORT, SQL, REFUNDS, SQL])
    a, sid = report(service)
    b, _ = report(service, "refunds by reason", sid)
    feedback(service, sid, b["request_id"], "unsatisfied")
    assert store.rows[a["request_id"]]["customer_satisfaction"] is None         # missing != FALSE
    assert store.rows[b["request_id"]]["customer_satisfaction"] is False


def test_each_report_can_be_rated_independently_including_chart_changes():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    b, _ = chat(service, "Pie", sid, action="change_chart", visual_type="pieChart")
    feedback(service, sid, a["request_id"], "satisfied")
    feedback(service, sid, b["request_id"], "unsatisfied")
    assert store.rows[a["request_id"]]["customer_satisfaction"] is True
    assert store.rows[b["request_id"]]["customer_satisfaction"] is False


def test_validation_and_ownership():
    service, _, store, _ = make([REPORT, SQL, REPORT, SQL])
    a, sid = report(service)
    other, other_sid = report(service, "monthly revenue", "ffffffffffff")
    ok = {"session_id": sid, "request_id": a["request_id"], "satisfaction": "satisfied"}
    for bad in ({**ok, "satisfaction": "maybe"}, {**ok, "satisfaction": True}, {**ok, "request_id": ""},
                {**ok, "session_id": None}, {**ok, "request_id": "x" * 65}, {}):
        assert service.handle_feedback(bad)[1] == 400
    assert service.handle_feedback({**ok, "session_id": "unknown"})[1] == 404
    assert service.handle_feedback({**ok, "request_id": "nonexistent"})[1] == 404
    # a request that belongs to another session cannot be rated through this one
    assert service.handle_feedback({**ok, "request_id": other["request_id"]})[1] == 404
    assert store.rows[other["request_id"]]["customer_satisfaction"] is None


def test_a_database_failure_is_reported_without_internals_and_changes_nothing():
    class Down(FakeStore):
        def set_satisfaction(self, *a):
            raise RuntimeError("Access denied for user 'view_builder'@'10.0.0.5'")
    service, _, store, _ = make([REPORT, SQL], store=Down())
    a, sid = report(service)
    payload, status = feedback(service, sid, a["request_id"], "satisfied")
    assert status == 500 and "view_builder" not in str(payload) and "10.0.0.5" not in str(payload)
    assert report_entry(service, sid)["meta"]["feedback"] is None               # UI state unchanged
    assert len(service.transcript(sid)[0]["messages"]) == 2                     # no confirmation message added


def test_feedback_is_logged_in_the_conversation_audit():
    service, _, store, _ = make([REPORT, SQL])
    a, sid = report(service)
    feedback(service, sid, a["request_id"], "unsatisfied")
    logged = [r for r in store.conversation if r["intent"] == "feedback"]
    assert [r["text"] for r in logged][0] == "Unsatisfied" and logged[0]["request_id"] == a["request_id"]
    assert FOLLOW_UP in logged[1]["text"]


def test_followup_validation():
    service, _, _, _ = make([REPORT, SQL])
    a, sid = report(service)
    assert chat(service, "x", sid, action="feedback_followup", choice="maybe")[1] == 400
    assert chat(service, "x", sid, action="feedback_followup")[1] == 400
