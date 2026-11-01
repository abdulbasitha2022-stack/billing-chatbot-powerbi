"""user_id / user_role / platform: only trusted sources are recorded, nothing is invented."""
import json
import os

from werkzeug.datastructures import Headers

from app.identity import Identity, IdentityResolver
from chatbot_api import create_app
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json
from tests.support import REPORT, SQL, chat, make, report

PROXY = {"provider": "trusted_proxy", "trusted_proxy": {"secret_env": "GW_SECRET"}}
GOOD = {"X-Gateway-Secret": "s3cret", "X-Authenticated-User": "asha@contoso.com", "X-Authenticated-Role": "Finance Analyst"}


def resolver(identity=PROXY, env=None):
    return IdentityResolver({"app": {"platform": "Power BI"}, "identity": identity},
                            environ={"GW_SECRET": "s3cret"} if env is None else env)


def test_by_default_no_user_is_recorded_and_reports_still_work():
    service, _, store, _ = make([REPORT, SQL])
    payload, _ = report(service)
    row = store.rows[payload["request_id"]]
    assert (row["user_id"], row["user_role"], row["platform"]) == (None, None, "Power BI")


def test_a_user_name_sent_by_the_browser_is_ignored():
    service, _, store, _ = make([REPORT, SQL])
    payload, _ = chat(service, "monthly revenue", None, requested_by="the.ceo", user_id="the.ceo", user_role="admin")
    row = store.rows[payload["request_id"]]
    assert row["user_id"] is None and row["user_role"] is None


def test_trusted_proxy_headers_are_accepted_only_with_the_shared_secret():
    who = resolver().resolve(Headers(GOOD))
    assert who == Identity("asha@contoso.com", "Finance Analyst", "Power BI", True)
    for headers in ({**GOOD, "X-Gateway-Secret": "wrong"}, {k: v for k, v in GOOD.items() if k != "X-Gateway-Secret"}):
        anonymous = resolver().resolve(Headers(headers))
        assert anonymous.user_id is None and anonymous.user_role is None and anonymous.verified is False


def test_without_a_configured_secret_the_headers_are_never_trusted():
    assert resolver(env={}).resolve(Headers(GOOD)).user_id is None
    assert resolver({"provider": "none"}).resolve(Headers(GOOD)).user_id is None
    assert resolver({"provider": "something_else"}).resolve(Headers(GOOD)).user_id is None
    assert resolver().resolve(None).user_id is None


def test_identity_values_are_cleaned_and_length_limited():
    headers = {**GOOD, "X-Authenticated-User": "a" * 400, "X-Authenticated-Role": "ro\x00le\x1f" + "r" * 300}
    who = resolver().resolve(Headers(headers))
    assert len(who.user_id) == 255 and len(who.user_role) == 100 and "\x00" not in who.user_role
    assert resolver().resolve(Headers({**GOOD, "X-Authenticated-User": "  "})).user_id is None


def test_platform_comes_from_an_allow_list_not_from_whatever_the_client_says():
    r = resolver()
    assert r.platform_for({"platform": "powerbi"}) == "Power BI"
    assert r.platform_for({"platform": "tableau"}) == "Power BI"          # not supported yet: not recorded
    assert r.platform_for({"platform": "<script>"}) == "Power BI" and r.platform_for(None) == "Power BI"


def test_the_first_verified_user_owns_the_session():
    service, _, store, _ = make([REPORT, SQL, plan_json("chat", reply="hi")])
    asha = Identity("asha@contoso.com", "Finance Analyst", "Power BI", True)
    ravi = Identity("ravi@contoso.com", "Auditor", "Power BI", True)
    a, _ = chat(service, "monthly revenue", "0123456789ab", asha)
    row = store.rows[a["request_id"]]
    assert (row["user_id"], row["user_role"]) == ("asha@contoso.com", "Finance Analyst")
    assert chat(service, "hello", "0123456789ab", ravi)[1] == 403            # another user: refused
    assert chat(service, "hello", "0123456789ab", None)[1] == 403            # anonymous: refused
    assert service.end_session("0123456789ab", "user", ravi)[1] == 403
    assert service.handle_feedback({"session_id": "0123456789ab", "request_id": a["request_id"],
                                    "satisfaction": "satisfied"}, ravi)[1] == 403
    assert store.rows[a["request_id"]]["customer_satisfaction"] is None
    assert chat(service, "hello", "0123456789ab", asha)[1] == 200            # the owner still can


def test_end_to_end_over_http_with_a_trusted_gateway():
    os.environ["GW_SECRET"] = "s3cret"
    try:
        store = FakeStore()
        cfg = {"db": {"allowed_tables": ["charges"], "max_rows": 1000}, "context": {"files": []},
               "pipeline": {"max_retries": 1}, "chat": {"ask_chart_type": False},
               "app": {"timezone": "UTC"}, "identity": PROXY}
        app = create_app(cfg=cfg, llm=ScriptedLLM([REPORT, SQL, REPORT, SQL]), db=FakeDB(), store=store,
                         powerbi=FakePowerBI())
        client = app.test_client()
        via_gateway = client.post("/chat", json={"message": "monthly revenue", "platform": "powerbi"}, headers=GOOD)
        direct = client.post("/chat", json={"message": "monthly revenue", "user_id": "forged"})
        row1 = store.rows[via_gateway.get_json()["request_id"]]
        row2 = store.rows[direct.get_json()["request_id"]]
        assert (row1["user_id"], row1["user_role"], row1["platform"]) == ("asha@contoso.com", "Finance Analyst", "Power BI")
        assert row2["user_id"] is None and row2["user_role"] is None
        pre = client.options("/chat")
        assert "Authorization" in pre.headers["Access-Control-Allow-Headers"]
    finally:
        os.environ.pop("GW_SECRET", None)
