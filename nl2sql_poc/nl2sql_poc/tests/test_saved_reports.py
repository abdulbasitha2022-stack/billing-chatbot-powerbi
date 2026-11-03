"""Saved reports: the "Save report" button, the "Saved reports" list, open (restore) and delete."""
from app.access import AccessController
from app.identity import Identity
from tests.fakes import FakePowerBI
from tests.support import REPORT, SQL, grants, make, report

CEO = Identity(None, "CEO", "Power BI", True)
ANALYST = Identity(None, "Analyst", "Power BI", True)


def saved_report(**kw):
    """A service with one generated report; returns (service, store, powerbi, request_id, session_id)."""
    service, _, store, powerbi = make([REPORT, SQL], **kw)
    payload, sid = report(service)
    return service, store, powerbi, payload["request_id"], sid


def test_save_stores_the_page_once_and_lists_it():
    service, store, _, rid, sid = saved_report()
    payload, status = service.save_report({"request_id": rid, "session_id": sid}, CEO)
    assert status == 201 and payload["status"] == "saved" and payload["title"] == "Monthly Revenue"
    row = store.saved[rid]
    assert row["user_role"] == "CEO" and row["user_id"] is None
    assert row["sql_text"] and "page.json" in row["files"] and row["table_tmdl"]

    again, status = service.save_report({"request_id": rid}, CEO)
    assert status == 200 and again["status"] == "already_saved" and len(store.saved) == 1

    assert service.saved_report_status(rid, CEO)[0]["saved"] is True
    listed, status = service.list_saved_reports(CEO)
    assert status == 200 and [r["request_id"] for r in listed["reports"]] == [rid]
    assert "files" not in listed["reports"][0]                    # the list stays small


def test_open_rewrites_a_removed_page_and_reloads_in_the_chat():
    service, store, powerbi, rid, sid = saved_report(powerbi=FakePowerBI(automation=True),
                                                     background=lambda job: job())
    service.save_report({"request_id": rid}, CEO)
    powerbi.remove_page(rid)
    reloads_before = len(powerbi.reloads)

    payload, status = service.open_saved_report(rid, {"session_id": sid}, CEO)
    assert status == 200 and payload["status"] == "restored"
    assert payload["page_name"] == "Monthly Revenue"
    assert "Open latest report" in payload["message"]
    assert powerbi.restores == [rid] and len(powerbi.reloads) == reloads_before + 1
    # The restore message is part of the chat, so it survives the Desktop reload.
    assert any("Restored the saved report" in m["text"] for m in payload["messages"])
    assert store.saved[rid]["last_opened_at"] is not None


def test_open_without_a_chat_session_still_reloads():
    service, _, powerbi, rid, _ = saved_report(powerbi=FakePowerBI(automation=True),
                                               background=lambda job: job())
    service.save_report({"request_id": rid}, CEO)
    powerbi.remove_page(rid)
    reloads_before = len(powerbi.reloads)
    payload, status = service.open_saved_report(rid, {}, CEO)
    assert status == 200 and payload["status"] == "restored" and payload["busy"] is False
    assert len(powerbi.reloads) == reloads_before + 1


def test_open_reuses_a_page_that_is_still_there():
    service, _, powerbi, rid, sid = saved_report(powerbi=FakePowerBI(automation=True),
                                                 background=lambda job: job())
    service.save_report({"request_id": rid}, CEO)
    reloads_before = len(powerbi.reloads)
    payload, status = service.open_saved_report(rid, {"session_id": sid}, CEO)
    assert status == 200 and payload["status"] == "already_present"
    assert "page tab" in payload["message"] and len(powerbi.reloads) == reloads_before


def test_only_the_owner_can_save_see_open_or_delete():
    service, store, _, rid, _ = saved_report()
    assert service.save_report({"request_id": rid}, ANALYST)[1] == 404
    service.save_report({"request_id": rid}, CEO)
    assert service.list_saved_reports(ANALYST)[0]["reports"] == []
    assert service.saved_report_status(rid, ANALYST)[0]["saved"] is False
    assert service.open_saved_report(rid, {}, ANALYST)[1] == 404
    assert service.delete_saved_report(rid, ANALYST)[1] == 404
    assert rid in store.saved


def test_a_verified_user_owns_reports_by_user_id():
    service, _, store, _ = make([REPORT, SQL])
    alice, bob = Identity("alice", "CEO", "Power BI", True), Identity("bob", "CEO", "Power BI", True)
    payload, _ = report(service, identity=alice)
    rid = payload["request_id"]
    assert service.save_report({"request_id": rid}, bob)[1] == 404
    assert service.save_report({"request_id": rid}, alice)[1] == 201
    assert service.list_saved_reports(bob)[0]["reports"] == []
    assert len(service.list_saved_reports(alice)[0]["reports"]) == 1


def test_open_is_refused_when_the_role_lost_access():
    service, _, powerbi, rid, _ = saved_report()
    service.save_report({"request_id": rid}, CEO)
    powerbi.remove_page(rid)
    service.access = AccessController(grants(["refunds"]))      # CEO may no longer read charges
    payload, status = service.open_saved_report(rid, {}, CEO)
    assert status == 403 and "not authorized" in payload["error"]
    assert powerbi.restores == []


def test_a_missing_view_is_recreated_from_the_saved_sql():
    service, store, powerbi, rid, _ = saved_report()
    service.save_report({"request_id": rid}, CEO)
    view_name = store.saved[rid]["view_name"]
    store.views.clear()                                          # dropped by hand
    powerbi.remove_page(rid)
    assert service.open_saved_report(rid, {}, CEO)[1] == 200
    assert [v[0] for v in store.views] == [view_name]


def test_delete_forgets_only_the_saved_copy():
    service, store, powerbi, rid, _ = saved_report()
    service.save_report({"request_id": rid}, CEO)
    payload, status = service.delete_saved_report(rid, CEO)
    assert status == 200 and rid not in store.saved
    assert powerbi._page(rid) is not None                       # the page itself stays
    assert service.list_saved_reports(CEO)[0]["reports"] == []


def test_save_rejects_unknown_and_unfinished_requests():
    service, store, _, rid, _ = saved_report()
    assert service.save_report({"request_id": "nope"}, CEO)[1] == 404
    assert service.save_report({}, CEO)[1] == 400
    store.rows[rid]["request_status"] = "failed"
    assert service.save_report({"request_id": rid}, CEO)[1] == 409


def test_demo_mode_needs_a_role_to_find_the_owner():
    service, _, _, rid, _ = saved_report()
    service.identity.demo_role_selection = True
    assert service.list_saved_reports(Identity(platform="Power BI", demo_role=True))[1] == 403
    assert service.save_report({"request_id": rid}, Identity(platform="Power BI", demo_role=True))[1] == 403
