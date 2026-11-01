"""View naming convention: vw_<description>_<YYYYMMDD>_<session>[_<suffix>], safe and unique."""
from datetime import date

import pytest

from app.naming import (InvalidViewName, build_view_name, session_token, unique_view_name,
                        validate_view_name)
from app.report_store import ViewExistsError
from tests.fakes import FakeStore
from tests.support import FIXED_NOW, REFUNDS, REPORT, SQL, chat, make, report

DAY = date(2026, 10, 27)
SESSION = "f5ba1970c8cf"


def test_the_documented_examples_come_out_exactly():
    assert build_view_name("Refunds by Reason", DAY, SESSION) == "vw_refunds_by_reason_20261027_f5ba1970c8cf"
    assert build_view_name("Monthly Revenue", DAY, SESSION) == "vw_monthly_revenue_20261027_f5ba1970c8cf"
    assert build_view_name("Declines by Payment Method", DAY, SESSION) == \
        "vw_declines_by_payment_method_20261027_f5ba1970c8cf"


@pytest.mark.parametrize("title", [
    "Refunds by Reason - 2026", "", "   ", "Café Ünïcode & Résumé!", "x`; DROP TABLE users; --",
    "emoji 📈 revenue", "UPPER lower 123", "a" * 300, "a very long report title about refunds for every "
    "single plan and reason in the whole year of 2026 including everything else"])
@pytest.mark.parametrize("session", [SESSION, "S" * 64, "weird/session id!", "", "ÄÖÜ"])
@pytest.mark.parametrize("suffix", [None, "ab12", "deadbeefcafe0000"])
def test_names_are_always_valid_short_and_keep_the_required_parts(title, session, suffix):
    name = build_view_name(title, DAY, session, suffix)
    assert name == validate_view_name(name) and name.startswith("vw_") and len(name) <= 64
    assert "_20261027_" in name and f"_{session_token(session)}" in name
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in name)


def test_the_description_is_shortened_not_the_date_or_session():
    name = build_view_name("word " * 40, DAY, SESSION, suffix="ab12")
    assert name.endswith("_20261027_f5ba1970c8cf_ab12") and len(name) <= 64


def test_session_token_is_a_clean_12_character_identifier():
    assert session_token("F5BA1970C8CF-extra") == "f5ba1970c8cf"
    assert session_token("a/b c") == "abc" and session_token("") == "nosession"


@pytest.mark.parametrize("bad", ["", "refunds", "VW_x", "vw_x y", "vw_x`", "vw_x;drop", "vw_" + "a" * 70, None, 5,
                                 "vw_x\nselect"])
def test_unsafe_names_are_rejected(bad):
    with pytest.raises(InvalidViewName):
        validate_view_name(bad)


def test_unique_name_only_gets_a_suffix_when_the_plain_name_is_taken():
    free = unique_view_name("Refunds", DAY, SESSION, "abcd1234ef", lambda n: False)
    assert free == "vw_refunds_20261027_f5ba1970c8cf"
    base = free
    second = unique_view_name("Refunds", DAY, SESSION, "abcd1234ef", lambda n: n == base)
    assert second == "vw_refunds_20261027_f5ba1970c8cf_abcd"
    third = unique_view_name("Refunds", DAY, SESSION, "abcd1234ef", lambda n: n in (base, second))
    assert third == "vw_refunds_20261027_f5ba1970c8cf_abcd12"
    always = unique_view_name("Refunds", DAY, SESSION, "abcd1234ef", lambda n: True)
    assert always.startswith("vw_refunds_20261027_f5ba1970c8cf_") and len(always) <= 64


# ------------------------------------------------------------------ through the service
def test_two_reports_with_the_same_title_in_one_session_on_one_day_get_distinct_names():
    service, _, store, _ = make([REPORT, SQL, REPORT, SQL], clock=lambda: FIXED_NOW)
    a, sid = report(service, "monthly revenue", SESSION)
    b, _ = report(service, "monthly revenue again", sid)
    first, second = a["view_name"], b["view_name"]
    assert first == "vw_monthly_revenue_20261026_f5ba1970c8cf"
    assert second == f"{first}_{b['request_id'][:4]}" and first != second
    assert store.rows[b["request_id"]]["view_name"] == second == store.views[1][0]


def test_a_name_already_used_in_the_database_is_not_reused():
    store = FakeStore()
    store.taken_names.add("vw_monthly_revenue_20261026_f5ba1970c8cf")
    service, _, _, _ = make([REPORT, SQL], store=store, clock=lambda: FIXED_NOW)
    payload, _ = report(service, "monthly revenue", SESSION)
    assert payload["view_name"].startswith("vw_monthly_revenue_20261026_f5ba1970c8cf_")


def test_losing_a_race_for_the_name_retries_with_a_suffix():
    store = FakeStore()
    store.race_names.add("vw_monthly_revenue_20261026_f5ba1970c8cf")     # taken between check and CREATE
    service, _, _, _ = make([REPORT, SQL], store=store, clock=lambda: FIXED_NOW)
    payload, _ = report(service, "monthly revenue", SESSION)
    assert payload["view_name"] != "vw_monthly_revenue_20261026_f5ba1970c8cf"
    assert store.rows[payload["request_id"]]["view_name"] == payload["view_name"] == store.views[0][0]


def test_if_every_name_is_taken_the_request_fails_cleanly():
    class AlwaysTaken(FakeStore):
        def create_view(self, sql, name):
            raise ViewExistsError(name)
    store = AlwaysTaken()
    service, _, _, _ = make([REPORT, SQL], store=store)
    payload, status = chat(service, "monthly revenue")
    row = store.rows[payload["request_id"]]
    assert status == 500 and row["request_status"] == "failed" and row["view_name"] is None
    assert "unused view name" in row["error_message"] and not store.views


def test_user_text_never_reaches_the_identifier_unsanitised():
    evil = REPORT.replace("Monthly Revenue", "x`; DROP TABLE charges; --")
    service, _, store, _ = make([evil, SQL])
    payload, _ = report(service, "monthly revenue")
    name = payload["view_name"]
    assert validate_view_name(name) == name and "`" not in name and ";" not in name and " " not in name
