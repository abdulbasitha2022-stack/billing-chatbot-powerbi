"""End session: history is kept, no more messages are accepted, new conversations get new ids."""
import threading

from tests.fakes import FakeDB, ScriptedLLM, plan_json
from tests.support import REPORT, SQL, GatedLLM, chat, make, report

SID = "0123456789ab"


def test_ending_a_session_keeps_its_history_and_blocks_further_messages():
    service, _, store, _ = make([plan_json("chat", reply="Hi!")])
    a, _ = chat(service, "hello", SID)
    before = [m["text"] for m in service.transcript(SID)[0]["messages"]]
    result, status = service.end_session(SID, "user")
    assert status == 200 and result["ended"] is True
    transcript, _ = service.transcript(SID)
    assert transcript["ended"] is True
    texts = [m["text"] for m in transcript["messages"]]
    assert texts[:len(before)] == before and "Session ended" in texts[-1]       # nothing deleted
    assert service.sessions.get(SID).history                                    # LLM memory kept too
    payload, code = chat(service, "one more thing", SID)
    assert code == 409 and payload["session_ended"] is True and payload["status"] == "session_ended"
    assert len(service.transcript(SID)[0]["messages"]) == len(texts)            # the rejected message was not stored


def test_ending_is_idempotent_and_unknown_sessions_are_a_404():
    service, _, _, _ = make([plan_json("chat", reply="Hi!")])
    chat(service, "hello", SID)
    assert service.end_session(SID, "user")[1] == 200
    again, status = service.end_session(SID, "user")
    assert status == 200 and again["already_ended"] is True
    assert sum("Session ended" in m["text"] for m in service.transcript(SID)[0]["messages"]) == 1
    assert service.end_session("nope", "user")[1] == 404
    assert service.end_session("", "user")[1] == 400


def test_the_end_is_recorded_in_the_conversation_log():
    service, _, store, _ = make([plan_json("chat", reply="Hi!")])
    chat(service, "hello", SID)
    service.end_session(SID, "user")
    last = store.conversation[-1]
    assert last["intent"] == "session_end" and last["session_id"] == SID and "Session ended" in last["text"]


def test_a_new_conversation_gets_a_new_session_id_and_the_old_one_stays_ended():
    service, _, _, _ = make([plan_json("chat", reply="Hi!"), plan_json("chat", reply="Hello again")])
    first, _ = chat(service, "hello")
    service.end_session(first["session_id"], "user")
    second, status = chat(service, "hello again")                  # the visual sends no id for a new chat
    assert status == 200 and second["session_id"] != first["session_id"]
    assert service.sessions.get(first["session_id"]).ended is True
    assert service.sessions.get(second["session_id"]).ended is False


def test_an_ended_session_is_never_resumed_as_the_latest():
    service, _, _, _ = make([plan_json("chat", reply="Hi!")])
    a, _ = chat(service, "hello")
    assert service.latest()[0]["session_id"] == a["session_id"]
    service.end_session(a["session_id"], "user")
    assert service.latest()[1] == 404


def test_navigating_between_pages_never_ends_a_session():
    """The visual only READS (transcript / progress / latest) when it is rebuilt on page changes."""
    service, _, _, _ = make([plan_json("chat", reply="Hi!"), plan_json("chat", reply="Still here")])
    a, _ = chat(service, "hello", SID)
    for _ in range(5):                                             # leave the page, come back, repeat
        assert service.latest()[0]["session_id"] == SID
        assert service.transcript(SID)[0]["ended"] is False
        assert service.progress(SID)[0]["ended"] is False
    again, status = chat(service, "are you there?", SID)
    assert status == 200 and again["message"] == "Still here" and again["session_ended"] is False


def test_ending_while_a_clarification_is_open_closes_the_request_row():
    ask = plan_json("new_report", request="Revenue", confidence=70, sufficient=False, clarification="Which period?")
    service, _, store, _ = make([ask])
    a, _ = chat(service, "show revenue", SID)
    rid = a["request_id"]
    assert store.rows[rid]["request_status"] == "initiated"
    service.end_session(SID, "user")
    row = store.rows[rid]
    assert row["request_status"] == "failed" and "Session ended" in row["error_message"]
    assert row["response_time_ms"] is not None and service.sessions.get(SID).request_id is None


def test_ending_while_the_chart_question_is_pending_closes_the_row_and_clears_the_choice():
    ready = plan_json("new_report", request="Revenue", title="Revenue", confidence=95, sufficient=True)
    service, _, store, _ = make([ready], ask_chart=True)
    a, _ = chat(service, "revenue", SID)
    assert service.sessions.get(SID).pending_chart is not None
    service.end_session(SID, "user")
    assert service.sessions.get(SID).pending_chart is None
    assert store.rows[a["request_id"]]["request_status"] == "failed" and not store.views


def test_ending_while_a_report_is_running_lets_it_finish_cleanly_first():
    llm = GatedLLM([REPORT, SQL], block_on_call=2)                  # blocks inside the SQL generation call
    service, _, store, _ = make([], llm=llm)
    result = {}

    def run():
        result["payload"], result["status"] = chat(service, "monthly revenue", SID)

    worker = threading.Thread(target=run)
    worker.start()
    assert llm.entered.wait(5)
    pending, status = service.end_session(SID, "user")             # End clicked mid-request
    assert status == 202 and pending["pending"] is True and pending["ended"] is False
    assert service.sessions.get(SID).ended is False                # not cut off mid-way
    llm.release.set()
    worker.join(5)
    payload = result["payload"]
    assert result["status"] == 200 and payload["status"] == "ok" and payload["session_ended"] is True
    row = store.rows[payload["request_id"]]
    assert row["request_status"] == "completed" and row["view_name"] == store.views[0][0]   # report not corrupted
    session = service.sessions.get(SID)
    assert session.ended is True and session.last_report is not None
    assert "Session ended" in service.transcript(SID)[0]["messages"][-1]["text"]
    assert chat(service, "more", SID)[1] == 409


def test_old_new_chat_endpoint_ends_instead_of_deleting():
    service, _, _, _ = make([plan_json("chat", reply="Hi!")])
    chat(service, "hello", SID)
    result, status = service.reset(SID)
    assert status == 200 and result["ended"] is True and result["deleted"] is False
    assert service.sessions.get(SID) is not None and service.transcript(SID)[0]["messages"]
    assert service.reset("unknown")[0] == {"ok": True, "deleted": False, "ended": False}


def test_a_report_requested_after_a_new_session_starts_has_its_own_request_id():
    service, _, store, _ = make([REPORT, SQL, REPORT, SQL])
    a, sid = report(service)
    service.end_session(sid, "new_chat")
    b, new_sid = report(service)
    assert new_sid != sid and a["request_id"] != b["request_id"]
    assert store.rows[b["request_id"]]["session_id"] == new_sid
