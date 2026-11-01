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
    feedback = [json.loads(path.read_text()) for path in visuals
                if json.loads(path.read_text()).get("visual", {}).get("visualType") ==
                "billingChatbot79170476B0CA440F9F0C35FF45CF4C13"]
    assert len(feedback) == 1
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
