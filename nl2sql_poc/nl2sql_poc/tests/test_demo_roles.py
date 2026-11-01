import time

from app.identity import IdentityResolver
from chatbot_api import create_app
from app.access import AccessController
from tests.support import grants
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json
from tests.support import REPORT, SQL


def test_demo_role_selection_is_disabled_by_default_and_ignored_in_the_body():
    resolver = IdentityResolver({"app": {"demo_mode": False}})

    identity = resolver.resolve(None, {"demo_role": "CEO"})

    assert resolver.demo_role_selection is False
    assert identity.user_role is None and identity.demo_role is False


def test_demo_role_selection_requires_explicit_demo_mode_and_no_trusted_proxy():
    demo = IdentityResolver({"app": {"demo_mode": True}})
    trusted = IdentityResolver({"app": {"demo_mode": True},
                                "identity": {"provider": "trusted_proxy"}}, environ={})

    assert demo.demo_role_selection is True
    assert demo.resolve(None, {"demo_role": "dEvElOpEr"}).user_role == "Developer"
    assert demo.resolve(None, {"demo_role": "admin"}).user_role is None
    assert trusted.demo_role_selection is False
    assert trusted.resolve(None, {"demo_role": "CEO"}).user_role is None


def _app(demo_mode, replies=None):
    cfg = {"db": {"allowed_tables": ["charges"], "max_rows": 1000},
           "context": {"files": []}, "pipeline": {"max_retries": 1},
           "chat": {"ask_chart_type": False},
           "app": {"demo_mode": demo_mode, "platform": "Power BI"}}
    app = create_app(cfg=cfg, llm=ScriptedLLM(replies or []), db=FakeDB(),
                     store=FakeStore(), powerbi=FakePowerBI())
    app.config["SERVICE"].access = AccessController(grants(cfg["db"]["allowed_tables"]))
    return app


def test_demo_role_is_required_for_new_chats_and_pinned_for_followups_and_charts():
    app = _app(True, [REPORT, SQL, plan_json("explain", reply="Revenue report ready.")])
    client = app.test_client()

    assert client.get("/health").get_json()["demo_role_selection"] is True
    missing = client.post("/chat", json={"message": "revenue"})
    assert missing.status_code == 403

    created = client.post("/chat", json={"message": "revenue", "demo_role": "CEO"})
    assert created.status_code == 200 and created.get_json()["status"] == "ok"
    session_id = created.get_json()["session_id"]
    session = app.config["SERVICE"].sessions.get(session_id)
    assert session.user_role == "CEO"

    chart_change = client.post("/chat", json={"message": "Pie", "session_id": session_id,
                                               "action": "change_chart", "visual_type": "pieChart",
                                               "demo_role": "CEO"})
    assert chart_change.status_code == 200

    followup = client.post("/chat", json={"message": "Return to conversation", "session_id": session_id,
                                          "action": "feedback_followup", "choice": "return",
                                          "demo_role": "CEO"})
    assert followup.status_code == 200

    changed_role = client.post("/chat", json={"message": "explain this", "session_id": session_id,
                                               "demo_role": "Analyst"})
    assert changed_role.status_code == 409

    missing_role_followup = client.post("/chat", json={"message": "explain this", "session_id": session_id})
    assert missing_role_followup.status_code == 200

    invalid_role_followup = client.post("/chat", json={"message": "explain this", "session_id": session_id,
                                                      "demo_role": "admin"})
    assert invalid_role_followup.status_code == 403


def test_demo_role_field_has_no_effect_outside_demo_mode():
    app = _app(False, [REPORT, SQL])
    client = app.test_client()

    assert client.get("/health").get_json()["demo_role_selection"] is False
    response = client.post("/chat", json={"message": "revenue", "demo_role": "CEO"})
    assert response.status_code == 400
    service = app.config["SERVICE"]
    assert service.pipeline.db.ran == []
    assert service.store.rows[response.get_json()["request_id"]]["user_role"] is None


def test_demo_latest_session_fallback_resumes_only_the_selected_role():
    app = _app(True)
    service = app.config["SERVICE"]
    analyst = service.sessions.get_or_create("analyst-session")
    analyst.user_role = "Analyst"
    analyst.demo_role_selected = True
    analyst.add_message("user", "Analyst chat")
    ceo = service.sessions.get_or_create("ceo-session")
    ceo.user_role = "CEO"
    ceo.demo_role_selected = True
    ceo.add_message("user", "CEO chat")
    client = app.test_client()

    assert client.get("/session/latest?demo_role=Analyst").get_json()["session_id"] == "analyst-session"
    assert client.get("/session/latest?demo_role=CEO").get_json()["session_id"] == "ceo-session"
    assert client.get("/session/latest").status_code == 403


def test_latest_session_reports_when_it_was_last_active_so_roles_can_be_compared():
    app = _app(True)
    service = app.config["SERVICE"]
    now = time.time()
    for sid, role, when in (("ceo-old", "CEO", now - 120), ("dev-new", "Developer", now - 60)):
        session = service.sessions.get_or_create(sid)
        session.user_role, session.demo_role_selected = role, True
        session.add_message("user", f"{role} chat")
        session.updated = when
    client = app.test_client()

    ceo = client.get("/session/latest?demo_role=CEO").get_json()
    dev = client.get("/session/latest?demo_role=Developer").get_json()
    assert (ceo["session_id"], ceo["last_active"]) == ("ceo-old", now - 120)
    assert (dev["session_id"], dev["last_active"]) == ("dev-new", now - 60)
    assert client.get("/session/latest?demo_role=Analyst").status_code == 404   # "no session for this role"
