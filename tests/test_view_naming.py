"""View naming convention: vw_<description>_<YYYYMMDD>_<request>[_<suffix>], safe and unique."""
import re
from datetime import date

import pytest

from app.naming import (InvalidViewName, build_view_name, new_request_id, request_token, unique_view_name,
                        validate_view_name)
from app.report_store import ViewExistsError
from tests.fakes import FakeStore
from tests.support import FIXED_NOW, REFUNDS, REPORT, SQL, chat, make, report

DAY = date(2026, 10, 27)
REQUEST = "3fa91c07b2de"


def test_the_documented_examples_come_out_exactly():
    assert build_view_name("Refunds by Reason", DAY, REQUEST) == "vw_refunds_by_reason_20261027_3fa91c07b2de"
    assert build_view_name("Monthly Revenue", DAY, REQUEST) == "vw_monthly_revenue_20261027_3fa91c07b2de"
    assert build_view_name("Declines by Payment Method", DAY, REQUEST) == \
        "vw_declines_by_payment_method_20261027_3fa91c07b2de"


@pytest.mark.parametrize("title", [
    "Refunds by Reason - 2026", "", "   ", "Caf\u00e9 \u00dcn\u00efcode & R\u00e9sum\u00e9!", "x`; DROP TABLE users; --",
    "emoji \U0001F4C8 revenue", "UPPER lower 123", "a" * 300, "a very long report title about refunds for every "
    "single plan and reason in the whole year of 2026 including everything else"])
@pytest.mark.parametrize("request_id", [REQUEST, "R" * 64, "weird/request id!", "\u00c4\u00d6\u00dc", ""])
@pytest.mark.parametrize("suffix", [None, "ab12", "deadbeefcafe0000"])
def test_names_are_always_valid_short_and_keep_the_required_parts(title, request_id, suffix):
    name = build_view_name(title, DAY, request_id, suffix)
    assert name == validate_view_name(name) and name.startswith("vw_") and len(name) <= 64
    assert "_20261027_" in name
    if re.search(r"[a-z0-9]", request_id.lower()):     # ids with nothing usable get a random token
        assert f"_{request_token(request_id)}" in name
    assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in name)


def test_the_description_is_shortened_not_the_date_or_request():
    name = build_view_name("word " * 40, DAY, REQUEST, suffix="ab12")
    assert name.endswith("_20261027_3fa91c07b2de_ab12") and len(name) <= 64


def test_new_request_ids_are_short_unique_hex():
    ids = {new_request_id() for _ in range(1000)}
    assert len(ids) == 1000 and all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids)


def test_request_token_is_a_clean_12_character_identifier():
    assert request_token("3FA91C07B2DE-extra") == "3fa91c07b2de"
    assert request_token("a/b c") == "abc"
    # an old 32-character uuid id is cut to its first 12 characters
    assert request_token("0123456789abcdef0123456789abcdef") == "0123456789ab"
    # no id at all still gives a valid, random token (never a shared placeholder)
    assert re.fullmatch(r"[0-9a-f]{12}", request_token("")) and request_token("") != request_token(None)


@pytest.mark.parametrize("bad", ["", "refunds", "VW_x", "vw_x y", "vw_x`", "vw_x;drop", "vw_" + "a" * 70, None, 5,
                                 "vw_x\nselect"])
def test_unsafe_names_are_rejected(bad):
    with pytest.raises(InvalidViewName):
        validate_view_name(bad)


def test_unique_name_only_gets_a_suffix_when_the_plain_name_is_taken():
    free = unique_view_name("Refunds", DAY, REQUEST, lambda n: False)
    assert free == "vw_refunds_20261027_3fa91c07b2de"
    second = unique_view_name("Refunds", DAY, REQUEST, lambda n: n == free)
    assert re.fullmatch(r"vw_refunds_20261027_3fa91c07b2de_[0-9a-f]{4}", second)
    third = unique_view_name("Refunds", DAY, REQUEST, lambda n: n == free or len(n) == len(second))
    assert re.fullmatch(r"vw_refunds_20261027_3fa91c07b2de_[0-9a-f]{6}", third)
    always = unique_view_name("Refunds", DAY, REQUEST, lambda n: True)
    assert always.startswith("vw_refunds_20261027_3fa91c07b2de_") and len(always) <= 64


# ------------------------------------------------------------------ through the service
def test_two_reports_with_the_same_title_in_one_session_on_one_day_get_distinct_names():
    service, _, store, _ = make([REPORT, SQL, REPORT, SQL], clock=lambda: FIXED_NOW)
    a, sid = report(service, "monthly revenue")
    b, _ = report(service, "monthly revenue again", sid)
    first, second = a["view_name"], b["view_name"]
    # each name carries its own request_id, so no suffix is needed to tell them apart
    assert first == f"vw_monthly_revenue_20261026_{a['request_id']}"
    assert second == f"vw_monthly_revenue_20261026_{b['request_id']}" and first != second
    assert re.fullmatch(r"[0-9a-f]{12}", a["request_id"]) and a["request_id"] != b["request_id"]
    assert store.rows[b["request_id"]]["view_name"] == second == store.views[1][0]


def test_a_chart_change_reuses_the_view_but_gets_its_own_short_request_id():
    service, _, store, _ = make([REPORT, SQL], clock=lambda: FIXED_NOW)
    a, sid = report(service, "monthly revenue")
    b, _ = chat(service, "Pie chart", sid, action="change_chart", visual_type="pieChart")
    assert b["request_id"] != a["request_id"] and re.fullmatch(r"[0-9a-f]{12}", b["request_id"])
    assert store.rows[b["request_id"]]["view_name"] == a["view_name"] and len(store.views) == 1


def test_a_name_already_used_in_the_database_is_not_reused():
    class TakenPlainName(FakeStore):
        def view_exists(self, name):          # every plain name (no suffix) already exists in MySQL
            return re.fullmatch(r"vw_monthly_revenue_20261026_[0-9a-f]{12}", name) is not None
    store = TakenPlainName()
    service, _, _, _ = make([REPORT, SQL], store=store, clock=lambda: FIXED_NOW)
    payload, _ = report(service, "monthly revenue")
    plain = f"vw_monthly_revenue_20261026_{payload['request_id']}"
    assert payload["view_name"].startswith(plain + "_") and store.views[0][0] == payload["view_name"]


def test_losing_a_race_for_the_name_retries_with_a_suffix():
    class RaceOnPlainName(FakeStore):
        def create_view(self, sql, name):     # the plain name is taken between check and CREATE
            if re.fullmatch(r"vw_monthly_revenue_20261026_[0-9a-f]{12}", name):
                raise ViewExistsError(name)
            return super().create_view(sql, name)
    store = RaceOnPlainName()
    service, _, _, _ = make([REPORT, SQL], store=store, clock=lambda: FIXED_NOW)
    payload, _ = report(service, "monthly revenue")
    assert payload["view_name"].startswith(f"vw_monthly_revenue_20261026_{payload['request_id']}_")
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
