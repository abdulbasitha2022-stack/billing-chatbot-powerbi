"""Requests for restricted data are refused before any AI text is shown, and nothing is left pending.

What is restricted comes only from role_permissions. The planner says which restricted data the
user wants to SEE (plan.restricted); words in the message are not scanned, so a request that only
mentions restricted data to leave it out ("customers without email") is answered normally.
"""
import json
from pathlib import Path

import yaml

from app.access import AccessController
from app.audit import ConversationRecorder
from app.conversation import DENIED_REPLY
from app.identity import Identity, IdentityResolver
from app.pipeline import Pipeline
from chatbot_api import ChatService
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json
from tests.support import BILLING_PERMISSIONS, REPORT, SQL

TABLES = ["charges", "customers", "payment_methods"]
ACCESS_CFG = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "config_billing.yaml").read_text(encoding="utf-8"))["access"]
ANALYST = Identity(None, "Analyst", "Power BI", True)
DEVELOPER = Identity(None, "Developer", "Power BI", True)
CEO = Identity(None, "CEO", "Power BI", True)


def make(replies, ask_chart=False, db=None):
    cfg = {"db": {"allowed_tables": TABLES, "max_rows": 1000}, "context": {"files": []},
           "pipeline": {"max_retries": 1, "max_clarifications": 2},
           "chat": {"ask_chart_type": ask_chart, "end_wait_seconds": 0.05},
           "app": {"timezone": "UTC", "platform": "Power BI"}, "access": ACCESS_CFG}
    store, llm, db = FakeStore(), ScriptedLLM(replies), db or FakeDB()
    pipeline = Pipeline(llm, db, json.loads(json.dumps(cfg)), base_dir=".")
    service = ChatService(cfg, pipeline, store, FakePowerBI(), recorder=ConversationRecorder(store, None),
                          identity=IdentityResolver(cfg, environ={}))
    # The rows stored in role_permissions: payment_methods and customers.email are denied to
    # analysts/developers there; nothing is restricted in Python.
    service.access = AccessController(BILLING_PERMISSIONS)
    return service, llm, db, store


def chat(service, text, sid=None, identity=ANALYST, **extra):
    return service.handle_chat({"message": text, "session_id": sid, **extra}, identity)


def wants(restricted, **kw):
    """A planner answer that says the user wants to see `restricted` data."""
    kw.setdefault("reply", "Here it is.")
    return plan_json(kw.pop("intent", "new_report"), restricted=restricted, sufficient=True,
                     confidence=95, **kw)


def assert_denied(payload, status):
    assert status == 400 and payload["status"] == "failed" and payload["access_denied"] is True
    assert payload["message"] == DENIED_REPLY
    assistant = [m for m in payload["messages"] if m["role"] == "assistant"]
    assert [m["text"] for m in assistant] == [DENIED_REPLY]          # nothing else is shown


def test_restricted_column_the_user_wants_is_refused_before_any_sql():
    for role in (ANALYST, DEVELOPER):
        service, llm, db, _ = make([wants(["customers.email"], request="Customer emails by country")])
        payload, status = chat(service, "Show customer emails by country", identity=role)
        assert_denied(payload, status)
        assert len(llm.calls) == 1 and db.ran == []                  # planned, never turned into SQL


def test_mentioning_restricted_data_only_to_leave_it_out_is_answered():
    """The bug this replaces: the word "email" alone used to trigger the refusal."""
    plan = plan_json("new_report", reply="All customers without their email.", title="Customers",
                     request="Customer list with id, name, country and created date",
                     sufficient=True, confidence=95)
    sql = "```sql\nSELECT customer_id, name, country, created_at FROM customers\n```"
    for role in (ANALYST, DEVELOPER):
        service, _, db, _ = make([plan, sql])
        payload, status = chat(service, "could you show the customer table without email", identity=role)
        assert status == 200 and payload["status"] == "ok", payload
        assert "email" not in payload["sql"].lower() and len(db.ran) == 1


def test_any_wording_for_a_restricted_table_is_refused():
    for text in ("list payment methods by card brand", "how many saved cards expire in 2026",
                 "SELECT * FROM payment_methods"):
        service, _, db, _ = make([wants(["payment_methods"], request="Saved cards by brand")])
        assert_denied(*chat(service, text))
        assert db.ran == []


def test_the_model_naming_allowed_data_never_causes_a_denial():
    service, _, _, _ = make([wants(["customers.country"], request="Customers by country",
                                   title="Customers by Country"), SQL])
    payload, status = chat(service, "customers by country")
    assert status == 200 and payload["status"] == "ok"


def test_denial_discards_a_pending_clarification():
    service, llm, _, _ = make([plan_json("clarify", reply="Which period?", clarification="Which period?",
                                         confidence=30, sufficient=False),
                               wants(["customers.email"], request="Customer report with email addresses")])
    first, _ = chat(service, "customer report")
    assert first["status"] == "clarify"
    session = service.sessions.get(first["session_id"])
    assert session.awaiting_clarification and session.request_id

    denied = chat(service, "include their email addresses", first["session_id"])
    assert_denied(*denied)
    assert len(llm.calls) == 2                                           # planned only, no SQL call
    assert not session.awaiting_clarification and session.thread_messages == []
    assert session.request_id is None and session.pending_chart is None
    row = service.store.rows[first["request_id"]]
    assert row["request_status"] == "failed" and "Access denied" in row["error_message"]


def test_denial_discards_a_pending_chart_question():
    service, llm, db, _ = make([REPORT, wants(["customers.email"], request="Monthly revenue with customer email")],
                               ask_chart=True)
    first, _ = chat(service, "monthly revenue")
    assert first["status"] == "awaiting_chart_choice"
    session = service.sessions.get(first["session_id"])
    assert session.pending_chart

    assert_denied(*chat(service, "add the customer email to it", first["session_id"]))
    assert session.pending_chart is None and db.ran == []
    # The chart chip for the discarded request cannot resume it.
    after, _ = chat(service, "Pie", first["session_id"], action="choose_chart", visual_type="pieChart")
    assert after["status"] == "chat" and db.ran == [] and len(llm.calls) == 2


def test_the_planner_text_is_hidden_when_access_is_denied():
    for plan in (wants(["payment_methods"], intent="clarify", reply="Do you want the card brand too?",
                       clarification="Do you want the card brand too?", request="Customers by card brand"),
                 wants(["payment_methods"], intent="decline",
                       reply="Sorry, the database doesn't include details about customers' payment cards.")):
        service, _, db, _ = make([plan])
        payload, status = chat(service, "how do our customers pay?", identity=DEVELOPER)
        assert_denied(payload, status)
        assert "card" not in json.dumps(payload).lower() and db.ran == []
        assert service.sessions.get(payload["session_id"]).awaiting_clarification is False


def test_sql_guardrail_is_still_the_final_gate():
    """Even when the planner does not flag it, SQL that reads restricted data is refused."""
    sneaky = "```sql\nSELECT c.email FROM customers c\n```"
    service, _, db, store = make([plan_json("new_report", request="Customer contact list", title="Contacts",
                                            sufficient=True, confidence=95), sneaky, sneaky])
    payload, status = chat(service, "customer contact list")
    assert_denied(payload, status)
    assert db.ran == []
    assert store.rows[payload["request_id"]]["request_status"] == "failed"


def test_authorised_requests_still_work():
    service, _, _, _ = make([REPORT, SQL])
    payload, status = chat(service, "monthly revenue")
    assert status == 200 and payload["status"] == "ok"

    # For the CEO nothing is restricted, so even a planner that lists it does not refuse.
    service, llm, _, _ = make([wants(["customers.email"], intent="chat", reply="Here is what I can do.")])
    payload, status = chat(service, "show customer emails by country", identity=CEO)
    assert status == 200 and payload["status"] == "chat" and len(llm.calls) == 1


def test_access_controller_lists_denied_resources():
    access = AccessController(BILLING_PERMISSIONS)
    assert access.denied_resources("Analyst", TABLES) == ({"payment_methods"}, {"customers.email"})
    assert access.denied_resources("CEO", TABLES) == (set(), set())


# ------------------------------------------------------------------ "cards customers pay with"
CARDS = "Show the cards that the customers pay with."


def assert_no_restricted_names(payload):
    """Nothing the assistant says names restricted data or calls it missing."""
    text = " ".join([payload["message"]] + [m["text"] for m in payload["messages"]
                                            if m["role"] == "assistant"]).lower()
    for word in ("payment_method", "payment method", "card", "email", "does not exist",
                 "doesn't include", "not available"):
        assert word not in text, word


def test_developer_asking_for_customer_cards_gets_the_standard_denial():
    service, llm, db, _ = make([wants(["payment_methods"], request="Cards customers pay with",
                                      reply="Here are the cards customers use.")])
    payload, status = chat(service, CARDS, identity=DEVELOPER)
    assert_denied(payload, status)
    assert payload["message"] == ("You are not permitted to access this information. "
                                  "Please contact your administrator if you need access.")
    assert_no_restricted_names(payload)
    planner_prompt = llm.calls[0][0]
    assert "Restricted for this user" in planner_prompt and "payment_methods" in planner_prompt
    assert db.ran == []


def test_ceo_access_to_customer_cards_follows_role_permissions():
    report = plan_json("new_report", reply="Customers by card brand.", request="Customers by card brand",
                       title="Cards Customers Use", confidence=95, sufficient=True)
    sql = "```sql\nSELECT card_brand, COUNT(*) AS customers FROM payment_methods GROUP BY card_brand\n```"
    service, llm, db, _ = make([report, sql])
    payload, status = chat(service, CARDS, identity=CEO)
    assert status == 200 and payload["status"] == "ok" and len(db.ran) == 1
    assert "Restricted for this user" not in llm.calls[0][0]

    # Deny the table to the CEO in role_permissions: the planner is now told it is restricted.
    service, llm, db, _ = make([wants(["payment_methods"], request="Customers by card brand")])
    service.access = AccessController([
        dict(r, allowed=False) if (r["role"], r["resource_name"]) == ("CEO", "payment_methods") else r
        for r in BILLING_PERMISSIONS])
    assert_denied(*chat(service, CARDS, identity=CEO))
    assert "payment_methods" in llm.calls[0][0] and db.ran == []
