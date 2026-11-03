"""Generator tests on a synthetic PBIP skeleton (no Power BI needed)."""
import json

import pytest

from app.powerbi_project_generator import PowerBIProjectError, generate_native_report_page

COLUMNS = [{"name": "month_start", "type": "date"}, {"name": "revenue_usd", "type": "double"}]


def skeleton(tmp_path):
    report, model = tmp_path / "demo.Report", tmp_path / "demo.SemanticModel"
    (report / "definition" / "pages" / "p1").mkdir(parents=True)
    (model / "definition" / "tables").mkdir(parents=True)
    (report / "definition.pbir").write_text(json.dumps(
        {"datasetReference": {"byPath": {"path": "../demo.SemanticModel"}}}))
    (report / "definition" / "pages" / "pages.json").write_text(json.dumps({"pageOrder": ["p1"]}))
    (report / "definition" / "pages" / "p1" / "page.json").write_text(json.dumps(
        {"name": "p1", "displayName": "Chatbot", "width": 1280, "height": 720}))
    (model / "definition" / "model.tmdl").write_text(
        'model Model\n\nannotation PBI_QueryOrder = ["old"]\n\nref cultureInfo en-IN\n')
    return report, model


def generate(report, model, **kw):
    return generate_native_report_page(report_dir=report, model_dir=model, view_name="vw_1_abc",
                                       title="Monthly Revenue", columns=COLUMNS, **kw)


def test_new_page_creates_table_and_page(tmp_path):
    report, model = skeleton(tmp_path)
    out = generate(report, model, requested_visual_type="pieChart")
    assert out["visual_type"] == "pieChart" and out["page_name"] == "Monthly Revenue"
    assert (model / "definition" / "tables" / "billing vw_1_abc.tmdl").exists()
    assert "ref table 'billing vw_1_abc'" in (model / "definition" / "model.tmdl").read_text()
    assert out["needs_model_refresh"] is True


def test_reuse_adds_only_a_page_and_leaves_the_model_untouched(tmp_path):
    report, model = skeleton(tmp_path)
    generate(report, model)
    table = model / "definition" / "tables" / "billing vw_1_abc.tmdl"
    model_text, table_text = (model / "definition" / "model.tmdl").read_text(), table.read_text()
    out = generate(report, model, requested_visual_type="donutChart", reuse_existing_table=True)
    assert out["reused_table"] is True and out["needs_model_refresh"] is False
    assert out["page_name"] == "Monthly Revenue (2)"                      # unique page name
    assert (model / "definition" / "model.tmdl").read_text() == model_text
    assert table.read_text() == table_text
    pages = json.loads((report / "definition" / "pages" / "pages.json").read_text())["pageOrder"]
    assert len(pages) == 3
    visual = next((report / "definition" / "pages" / out["page_id"] / "visuals").glob("*/visual.json"))
    assert json.loads(visual.read_text())["visual"]["visualType"] in ("donutChart", "tableEx")


def test_duplicate_without_reuse_and_reuse_without_table_are_rejected(tmp_path):
    report, model = skeleton(tmp_path)
    with pytest.raises(PowerBIProjectError, match="not in the semantic model"):
        generate(report, model, reuse_existing_table=True)
    generate(report, model)
    with pytest.raises(PowerBIProjectError, match="already contains"):
        generate(report, model)


def test_request_id_adds_feedback_visual_to_generated_report_page(tmp_path):
    report, model = skeleton(tmp_path)
    out = generate(report, model, request_id="req-123", session_id="session-456")
    assert out["feedback_visual_added"] is True
    visuals = list((report / "definition" / "pages" / out["page_id"] / "visuals").glob("*/visual.json"))
    chatbots = [json.loads(path.read_text()) for path in visuals
                if json.loads(path.read_text()).get("visual", {}).get("visualType") ==
                "billingChatbot79170476B0CA440F9F0C35FF45CF4C13"]
    # Two copies of the chatbot visual: the feedback panel and the "Save report" button.
    modes = lambda v: v["visual"]["objects"]["feedback"][0]["properties"]["mode"]["expr"]["Literal"]["Value"]
    assert sorted(modes(v) for v in chatbots) == ["'feedback'", "'save'"]
    feedback = [v for v in chatbots if modes(v) == "'feedback'"]
    feedback_visual = feedback[0]["visual"]
    props = feedback_visual["objects"]["feedback"][0]["properties"]
    # The feedback visual no longer uses a data-field projection; it is driven
    # entirely by the objects below (no queryState / feedbackAnchor needed).
    assert "query" not in feedback_visual, "feedbackAnchor field binding must be absent"
    assert props["mode"]["expr"]["Literal"]["Value"] == "'feedback'"
    assert props["requestId"]["expr"]["Literal"]["Value"] == "'req-123'"
    assert props["sessionId"]["expr"]["Literal"]["Value"] == "'session-456'"
    # Panel must be bottom-right aligned (x > 0, far from the left edge)
    pos = feedback[0]["position"]
    assert pos["x"] > 30, "feedback panel should be right-aligned, not flush left"


# ------------------------------------------------------------------ navigation buttons
from app import powerbi_project_generator as gen  # noqa: E402
from app.powerbi_project_generator import (CHATBOT_VISUAL_TYPE, NAV_ANNOTATION,  # noqa: E402
                                           ensure_latest_report_button)

# Keys the PBIR visualContainer 2.13.0 / visualConfiguration 2.8.0 schemas allow.
CONTAINER_KEYS = {"$schema", "name", "position", "visual", "visualGroup", "parentGroupName",
                  "filterConfig", "isHidden", "annotations", "howCreated"}
VISUAL_KEYS = {"visualType", "autoSelectVisualType", "query", "expansionStates", "objects",
               "visualContainerObjects", "syncGroup", "drillFilterOtherVisuals"}


def with_chatbot(report):
    """Put the chat (non-feedback) visual on p1, like the AI Assistant page."""
    path = report / "definition" / "pages" / "p1" / "visuals" / "chat" / "visual.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"name": "chat", "position": {"x": 0, "y": 0, "width": 1100, "height": 700},
                                "visual": {"visualType": CHATBOT_VISUAL_TYPE}}))
    return report


def nav_buttons(report, page_id, role):
    found = []
    for f in (report / "definition" / "pages" / page_id / "visuals").glob("*/visual.json"):
        v = json.loads(f.read_text(encoding="utf-8"))
        if {"name": NAV_ANNOTATION, "value": role} in v.get("annotations", []):
            found.append(v)
    return found


def link_of(button):
    props = button["visual"]["visualContainerObjects"]["visualLink"][0]["properties"]
    return {k: v["expr"]["Literal"]["Value"] for k, v in props.items()}


def text_of(button):
    return button["visual"]["objects"]["text"][1]["properties"]["text"]["expr"]["Literal"]["Value"]


def latest(report):
    (button,) = nav_buttons(report, "p1", "latestReport")
    return button


def test_before_any_report_the_button_says_no_report_and_has_no_action(tmp_path):
    report, _ = skeleton(tmp_path)
    with_chatbot(report)
    assert ensure_latest_report_button(report) is True
    button = latest(report)
    assert text_of(button) == "'No report generated yet'"
    assert link_of(button)["show"] == "false" and "navigationSection" not in link_of(button)
    assert set(button) <= CONTAINER_KEYS and set(button["visual"]) <= VISUAL_KEYS
    assert button["visual"]["visualType"] == "actionButton"
    # Fixed top-right slot, here clear of the chat visual, and layered above it.
    assert button["position"]["x"] >= 1100 and button["position"]["x"] + button["position"]["width"] <= 1280
    assert button["position"]["y"] == 10
    before = json.dumps(button)
    assert ensure_latest_report_button(report) is False and json.dumps(latest(report)) == before


def test_a_report_activates_the_button_and_gets_a_back_button(tmp_path):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    ensure_latest_report_button(report)
    result = generate(report, model, request_id="req1", session_id="s1")
    assert result["latest_button_updated"] and result["back_button_added"]

    button = latest(report)
    link = link_of(button)
    assert (link["show"], link["type"], link["navigationSection"]) == (
        "true", "'PageNavigation'", f"'{result['page_id']}'")
    assert text_of(button) == "'Open latest report'" and "Monthly Revenue" in link_of(button)["tooltip"]

    (back,) = nav_buttons(report, result["page_id"], "backToAssistant")
    assert link_of(back)["navigationSection"] == "'p1'" and text_of(back) == "'Back to Chatbot'"
    assert set(back) <= CONTAINER_KEYS and set(back["visual"]) <= VISUAL_KEYS
    pos = back["position"]
    assert pos["y"] + pos["height"] <= 720 and pos["x"] + pos["width"] <= 1280


def test_the_next_report_moves_the_same_button_to_the_new_page(tmp_path):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    first = generate(report, model, request_id="req1")          # no placeholder: created on demand
    position = latest(report)["position"]
    second = generate_native_report_page(report_dir=report, model_dir=model, view_name="vw_2_def",
                                         title="Refunds", columns=COLUMNS, request_id="req2")
    assert link_of(latest(report))["navigationSection"] == f"'{second['page_id']}'" != f"'{first['page_id']}'"
    assert latest(report)["position"] == position                # one button, same place


def test_a_failed_generation_keeps_the_previous_successful_report(tmp_path, monkeypatch):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    good = generate(report, model, request_id="req1")

    def broken(*args, **kwargs):
        raise PowerBIProjectError("model update failed")
    monkeypatch.setattr(gen, "_update_model_file", broken)
    with pytest.raises(PowerBIProjectError):
        generate_native_report_page(report_dir=report, model_dir=model, view_name="vw_2_def",
                                    title="Refunds", columns=COLUMNS, request_id="req2")
    assert link_of(latest(report))["navigationSection"] == f"'{good['page_id']}'"
    pages = json.loads((report / "definition" / "pages" / "pages.json").read_text())["pageOrder"]
    assert pages == ["p1", good["page_id"]]                      # the failed page was rolled back


def test_a_button_write_failure_leaves_the_old_target_and_the_report_still_succeeds(tmp_path, monkeypatch):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    good = generate(report, model, request_id="req1")
    monkeypatch.setattr(gen, "_json_write_atomic", lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    second = generate_native_report_page(report_dir=report, model_dir=model, view_name="vw_2_def",
                                         title="Refunds", columns=COLUMNS, request_id="req2")
    assert second["latest_button_updated"] is False
    assert link_of(latest(report))["navigationSection"] == f"'{good['page_id']}'"


def test_the_button_is_found_by_its_annotation_even_if_renamed(tmp_path):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    ensure_latest_report_button(report)
    visuals = report / "definition" / "pages" / "p1" / "visuals"
    (visuals / "aiOpenLatestReport").rename(visuals / "renamedByDesktop")
    result = generate(report, model, request_id="req1")
    assert link_of(latest(report))["navigationSection"] == f"'{result['page_id']}'"
    assert not (visuals / "aiOpenLatestReport").exists()


def test_the_button_stays_in_its_corner_and_above_a_chat_visual_that_covers_the_page(tmp_path):
    report, model = skeleton(tmp_path)
    chat = with_chatbot(report) / "definition" / "pages" / "p1" / "visuals" / "chat" / "visual.json"
    chat.write_text(json.dumps({"name": "chat", "position": {"x": 0, "y": 0, "z": 5000, "width": 1280, "height": 720},
                                "visual": {"visualType": CHATBOT_VISUAL_TYPE}}))
    ensure_latest_report_button(report)
    pos = latest(report)["position"]
    assert (pos["x"] + pos["width"], pos["y"]) == (1270, 10) and pos["z"] > 5000
    generate(report, model, request_id="req1")
    assert latest(report)["position"] == pos


def test_startup_moves_a_misplaced_button_back_and_keeps_its_target(tmp_path):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    result = generate(report, model, request_id="req1")
    button_file = next((report / "definition" / "pages" / "p1" / "visuals").glob("aiOpenLatestReport/visual.json"))
    misplaced = json.loads(button_file.read_text(encoding="utf-8"))
    misplaced["position"].update(x=400, y=300, z=1)               # e.g. dragged behind the chat visual
    button_file.write_text(json.dumps(misplaced), encoding="utf-8")

    assert ensure_latest_report_button(report) is True
    button = latest(report)
    assert (button["position"]["x"], button["position"]["y"]) == (1130, 10) and button["position"]["z"] >= 200
    assert link_of(button)["navigationSection"] == f"'{result['page_id']}'"
    assert text_of(button) == "'Open latest report'"


def test_without_a_chatbot_page_no_buttons_are_added(tmp_path):
    report, model = skeleton(tmp_path)
    assert ensure_latest_report_button(report) is False
    result = generate(report, model, request_id="req1")
    assert result["back_button_added"] is False and result["latest_button_updated"] is False
    assert nav_buttons(report, result["page_id"], "backToAssistant") == []


# ------------------------------------------------------------------ saved reports
from app.powerbi_project_generator import (export_report_page, find_report_page,  # noqa: E402
                                           restore_report_page)


def page_files(report, page_id):
    folder = report / "definition" / "pages" / page_id
    return {p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8") for p in folder.rglob("*.json")}


def page_order(report):
    return json.loads((report / "definition" / "pages" / "pages.json").read_text())["pageOrder"]


def test_the_save_visual_sits_right_of_the_back_button(tmp_path):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    result = generate(report, model, request_id="req1", session_id="s1")
    assert result["save_visual_added"] is True
    visuals = report / "definition" / "pages" / result["page_id"] / "visuals"
    save = json.loads((visuals / "aiSaveReport" / "visual.json").read_text(encoding="utf-8"))
    (back,) = nav_buttons(report, result["page_id"], "backToAssistant")
    props = save["visual"]["objects"]["feedback"][0]["properties"]
    assert save["visual"]["visualType"] == CHATBOT_VISUAL_TYPE
    assert props["mode"]["expr"]["Literal"]["Value"] == "'save'"
    assert props["requestId"]["expr"]["Literal"]["Value"] == "'req1'"
    assert save["position"]["y"] == back["position"]["y"]
    assert save["position"]["x"] == back["position"]["x"] + back["position"]["width"] + 15
    assert set(save) <= CONTAINER_KEYS and set(save["visual"]) <= VISUAL_KEYS
    # The save visual must not be mistaken for the main chatbot page.
    assert gen._find_chatbot_page(report / "definition" / "pages", page_order(report))[0] == "p1"


def test_a_saved_page_is_restored_exactly_after_it_was_deleted(tmp_path):
    report, model = skeleton(tmp_path)
    with_chatbot(report)
    result = generate(report, model, request_id="req1", session_id="s1")
    page_id = result["page_id"]
    original = page_files(report, page_id)
    saved = export_report_page(report_dir=report, model_dir=model, request_id="req1", view_name="vw_1_abc")
    assert saved["files"] == original and saved["page_name"] == "Monthly Revenue"
    assert saved["visual_type"] == "lineChart" and saved["table_tmdl"]
    model_text = (model / "definition" / "model.tmdl").read_text()

    # The user deletes the page and the table in Desktop, which then saves the project.
    import shutil
    shutil.rmtree(report / "definition" / "pages" / page_id)
    pages_file = report / "definition" / "pages" / "pages.json"
    pages_file.write_text(json.dumps({"pageOrder": ["p1"]}))
    (model / "definition" / "tables" / "billing vw_1_abc.tmdl").unlink()
    (model / "definition" / "model.tmdl").write_text(model_text.replace("ref table 'billing vw_1_abc'\n", ""))
    assert find_report_page(report, "req1") is None

    out = restore_report_page(report_dir=report, model_dir=model, saved=dict(saved, request_id="req1"))
    assert out["status"] == "generated_pending_reload_and_refresh" and out["page_id"] == page_id
    assert out["table_restored"] is True and out["model_table"] == "billing vw_1_abc"
    assert page_files(report, page_id) == original                   # looks exactly the same
    assert page_order(report) == ["p1", page_id]
    assert "ref table 'billing vw_1_abc'" in (model / "definition" / "model.tmdl").read_text()
    assert link_of(latest(report))["navigationSection"] == f"'{page_id}'"
    assert find_report_page(report, "req1") == page_id


def test_restore_reuses_a_page_that_is_still_there(tmp_path):
    report, model = skeleton(tmp_path)
    result = generate(report, model, request_id="req1")
    saved = export_report_page(report_dir=report, model_dir=model, request_id="req1", view_name="vw_1_abc")
    before = page_order(report)
    out = restore_report_page(report_dir=report, model_dir=model, saved=dict(saved, request_id="req1"))
    assert out["status"] == "already_present" and out["page_id"] == result["page_id"]
    assert page_order(report) == before


def test_restore_gets_a_new_tab_name_when_the_old_one_is_taken(tmp_path):
    report, model = skeleton(tmp_path)
    result = generate(report, model, request_id="req1")
    saved = export_report_page(report_dir=report, model_dir=model, request_id="req1", view_name="vw_1_abc")
    import shutil
    shutil.rmtree(report / "definition" / "pages" / result["page_id"])
    (report / "definition" / "pages" / "pages.json").write_text(json.dumps({"pageOrder": ["p1"]}))
    generate(report, model, request_id="req2", reuse_existing_table=True)   # takes "Monthly Revenue"
    out = restore_report_page(report_dir=report, model_dir=model, saved=dict(saved, request_id="req1"))
    assert out["page_name"] == "Monthly Revenue (2)" and out["table_restored"] is False


def test_restore_rejects_paths_outside_the_page_folder(tmp_path):
    report, model = skeleton(tmp_path)
    generate(report, model, request_id="req1")
    saved = export_report_page(report_dir=report, model_dir=model, request_id="req1", view_name="vw_1_abc")
    bad = dict(saved, request_id="other", page_id="gone",
               files=dict(saved["files"], **{"../../evil.json": "{}"}))
    with pytest.raises(PowerBIProjectError, match="invalid file entry"):
        restore_report_page(report_dir=report, model_dir=model, saved=bad)
    assert not (report / "definition" / "evil.json").exists()


def test_export_of_an_unknown_request_returns_none(tmp_path):
    report, model = skeleton(tmp_path)
    generate(report, model, request_id="req1")
    assert export_report_page(report_dir=report, model_dir=model, request_id="nope", view_name="vw_1_abc") is None
