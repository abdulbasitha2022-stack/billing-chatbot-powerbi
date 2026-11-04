"""Create a PBIR page with native visuals for a generated MySQL view.

This module edits a local Power BI Project (PBIP) on disk. It adds a TMDL table
that imports the named MySQL view, registers the table in model.tmdl, and adds a
new PBIR page containing a native chart plus a details table.

Power BI Desktop must reload external project changes and refresh the model before
new imported rows can be displayed. Always work on a saved, backed-up project.
"""
# BEGINNER GUIDE TO THE FILES THIS MODULE EDITS
# A Power BI Project (PBIP) is a Power BI report saved as a folder of plain text files instead
# of one binary .pbix file. It has two sub-folders:
#
#   <Name>.Report/              <- the "report" = pages and visuals (format called PBIR)
#     definition.pbir           <- says which semantic model the report uses ("byPath")
#     definition/pages/pages.json
#                               <- list of page ids in display order ("pageOrder") + active page
#     definition/pages/<pageId>/page.json
#                               <- one page: its display name, width, height
#     definition/pages/<pageId>/visuals/<visualId>/visual.json
#                               <- one visual on that page: type (lineChart, tableEx, button...),
#                                  position (x, y, z = stacking order, width, height) and which
#                                  table columns it shows ("query" / "projections")
#
#   <Name>.SemanticModel/       <- the "model" = tables and data sources (format called TMDL)
#     definition/model.tmdl     <- model-wide settings + "ref table '...'" lines listing every table
#     definition/tables/<table>.tmdl
#                               <- one table: its columns + a Power Query (M) "partition" saying
#                                  where the rows come from (here: a MySQL view, import mode)
#
# What generate_native_report_page() does for each new report:
#   1. writes tables/'billing vw_xxx'.tmdl (unless the table is being reused)
#   2. adds "ref table 'billing vw_xxx'" to model.tmdl
#   3. writes a new page folder with a chart, a detail table, a feedback chatbot visual, a
#      "Back to ..." button and a "Save report" chatbot visual next to it, and appends the page
#      id to pages.json
#   4. points the chatbot page's "Open latest report" button at the new page
# export_report_page() / restore_report_page() copy a generated page out of the project and back
# in again (the "Saved reports" feature, see app/saved_reports.py).
# Power BI Desktop does not notice these files by itself: powerbi_service.py later runs the
# PowerShell script to reload the project and import the view's rows.
#
# Called by: powerbi_service.PowerBIService (build_page and __init__).
# Calls: only the file system (json, shutil, pathlib). It never connects to MySQL or Power BI.
from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterable

# Log lines go to logs/backend_billing.log (handlers configured in logging_setup.py).
log = logging.getLogger(__name__)


# A "simple" SQL/TMDL name: letter or underscore first, then letters/digits/underscores.
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Power BI's internal names for the built-in visuals this module knows how to write.
# "tableEx" is the normal Power BI "Table" visual.
SUPPORTED_VISUAL_TYPES = {"lineChart", "clusteredColumnChart", "barChart", "pieChart", "donutChart", "tableEx"}
VIEW_RE = re.compile(r"^vw_[A-Za-z0-9_]+$")
# The custom visual's id (from billingChatbot's pbiviz.json). Used to find the chatbot page and
# to place a feedback copy of the chatbot on generated pages.
CHATBOT_VISUAL_TYPE = "billingChatbot79170476B0CA440F9F0C35FF45CF4C13"
# JSON schema URL written into every visual.json ("$schema"); Desktop uses it to know the file version.
VISUAL_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json"
# Native navigation buttons this module owns, recognised by this annotation (not by position).
NAV_ANNOTATION = "billingChatbot.navigationButton"
LATEST_BUTTON, BACK_BUTTON = "latestReport", "backToAssistant"
# "Open latest report" always sits in the chatbot page's top-right corner, above every other visual.
LATEST_BUTTON_WIDTH, LATEST_BUTTON_HEIGHT, LATEST_BUTTON_MARGIN = 140.0, 70, 10.0
# Reflections brand colours for generated pages (the chatbot visual's stylesheet uses the same).
BRAND_GREEN, BRAND_GREEN_DARK, INK, MUTED, LINE = "#7AC143", "#4E8A24", "#1A1A1A", "#5F6B7A", "#E3E8EE"
# Generated page layout, in page pixels, top to bottom:
#   header (title + "Generated ... · Source view ...") -> chart -> detail table -> toolbar.
# The toolbar is one row: [Back to AI Assistant] [Save report] ........ [Was this useful? 👍 👎]
PAGE_MARGIN, HEADER_HEIGHT, TOOLBAR_HEIGHT, LAYOUT_GAP = 24, 72, 64, 16
BACK_BUTTON_WIDTH, BACK_BUTTON_HEIGHT = 260, TOOLBAR_HEIGHT
SAVE_VISUAL_WIDTH, SAVE_VISUAL_GAP = 200, 12
FEEDBACK_VISUAL_WIDTH = 780
# Saved page files are stored by their path inside the page folder ("page.json",
# "visuals/<id>/visual.json"). Only plain names are accepted, so a stored path can never point
# outside the page folder when it is written back.
SAVED_FILE_RE = re.compile(r"^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*\.json$")
PAGE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


# Custom exception type. Every expected problem in this module (missing file, bad JSON, bad
# column name...) is raised as this, so callers (powerbi_service.py / chatbot_api.py) can tell
# "known Power BI project problem" apart from a real programming bug.
class PowerBIProjectError(RuntimeError):
    """A safe, user-facing problem while modifying a Power BI project."""


# Random hex string (default 20 chars) used as a page id / visual id. PBIR folder names are
# these ids, so they must be unique but need not be meaningful.
def _uuid_hex(length: int = 20) -> str:
    return uuid.uuid4().hex[:length]


# Read a JSON file into a dict. The two expected failures (file missing, file not valid JSON)
# are converted into PowerBIProjectError with a clear message; `from exc` keeps the original cause.
def _json_read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PowerBIProjectError(f"Required Power BI project file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PowerBIProjectError(f"Invalid JSON in {path}: {exc}") from exc


# Write a dict as pretty JSON (2-space indent, real Unicode characters, trailing newline) - the
# same style Power BI Desktop itself uses. Creates missing parent folders (e.g. a new visual's folder).
def _json_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# Check that a column name is a simple identifier and return it unchanged. Column names are later
# pasted into TMDL text and visual JSON, so odd characters are refused rather than escaped.
# `kind` is only used in the error message (e.g. "column name").
def _valid_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name):
        raise PowerBIProjectError(
            f"Unsupported {kind} '{name}'. Generated view columns must use simple SQL identifiers "
            "(letters, digits and underscores, starting with a letter or underscore)."
        )
    return name


# Input: a MySQL type string such as "decimal(12,2)" or "varchar(50)".
# Output: (TMDL dataType, formatString or None), e.g. ("double", None), ("int64", "0").
# Anything unknown (varchar, text, enum...) becomes "string".
def _powerbi_type(mysql_type: str) -> tuple[str, str | None]:
    """Map SHOW COLUMNS MySQL types into a practical Power BI/TMDL type."""
    t = (mysql_type or "").lower().strip()
    # Drop the size part: "decimal(12,2)" -> "decimal". Note: extra words stay, so
    # "int(11) unsigned" becomes "int unsigned", which matches none of the sets below -> "string".
    base = re.sub(r"\(.*\)", "", t).strip()
    if base in {"date", "datetime", "timestamp", "time"}:
        # Time-only values aren't ideal in reports, but Power BI's datetime type is the closest.
        return "dateTime", "General Date"
    if base in {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "year", "bit"}:
        return "int64", "0"
    if base in {"decimal", "numeric", "float", "double", "real"}:
        return "double", None
    if base in {"bool", "boolean"}:
        return "boolean", None
    return "string", None


# True if the column can be summed on a chart's value axis (int64 or double in Power BI).
def _is_numeric(column: dict) -> bool:
    return _powerbi_type(column.get("type", ""))[0] in {"int64", "double"}


# True if the column looks like time: a real date type, OR a name containing month/date/year/...
# (the name check catches text columns like "2026-10"). Used to prefer a line chart over time.
def _is_temporal(column: dict) -> bool:
    t = (column.get("type") or "").lower().strip()
    base = re.sub(r"\(.*\)", "", t).strip()
    if base in {"date", "datetime", "timestamp"}:
        return True
    # Groq may return a month key like '2026-10' as VARCHAR. It still works as a categorical axis.
    name = column.get("name", "").lower()
    return any(token in name for token in ("month", "date", "period", "timestamp", "year", "week"))


# "auto" chart picking. Words used here:
#   dimension = the category / X-axis column (e.g. month, refund_reason)
#   measure   = the number that is summed on the Y-axis (e.g. revenue_usd)
# Returns (dimension name or None, measure name or None, visual type).
# Rules: no numbers -> table; a time column -> line chart; a text column -> column chart;
# otherwise -> table.
def _choose_fields(columns: list[dict]) -> tuple[str | None, str | None, str]:
    """Choose dimension, measure, and native chart type from output schema."""
    numeric = [column for column in columns if _is_numeric(column)]
    if not numeric:
        return None, None, "tableEx"

    temporal = [column for column in columns if _is_temporal(column)]
    # Non-numeric columns are possible categories. (Note: `temporal` may include a numeric
    # column such as an int "year", because _is_temporal also checks the name.)
    dimension_candidates = [column for column in columns if column not in numeric]
    # Temporal columns take precedence for time-series reports.
    if temporal:
        dim = temporal[0]
        measure = _pick_measure(numeric)
        return dim["name"], measure["name"], "lineChart"

    # Prefer descriptive string fields over IDs and long technical key fields.
    categories = [c for c in dimension_candidates if _powerbi_type(c.get("type", ""))[0] == "string"]
    categories = [c for c in categories if not c["name"].lower().endswith("_id")]
    if not categories:
        categories = dimension_candidates
    if categories:
        dim = categories[0]
        measure = _pick_measure(numeric)
        return dim["name"], measure["name"], "clusteredColumnChart"

    # No dimension: still provide a native table visual of the result.
    return None, numeric[0]["name"], "tableEx"


# Pick the "most money-like" numeric column as the chart's value.
# Each column scores the length of the longest priority word found in its name (longer match =
# more specific, e.g. "revenue" beats "_usd"). Sorting by -score puts the best first; ties are
# broken alphabetically so the choice is stable. Columns with no match score 0.
def _pick_measure(numeric_columns: list[dict]) -> dict:
    priorities = ("_usd", "revenue", "refund", "amount", "count", "rate", "_pct", "total")
    return sorted(
        numeric_columns,
        key=lambda c: (
            -max((len(p) for p in priorities if p in c["name"].lower()), default=0),
            c["name"].lower(),
        ),
    )[0]


# Combines the user's/LLM's chart request with the automatic choice above.
# "auto" (or None) -> automatic choice; an unknown type -> error; a chart without a usable
# category+number pair -> falls back to a table instead of drawing a meaningless chart.
def _select_requested_visual(columns: list[dict], requested_visual_type: str | None) -> tuple[str | None, str | None, str]:
    """Honor an explicit visual choice when the result has a usable category and measure."""
    dimension, measure, automatic_type = _choose_fields(columns)
    requested = (requested_visual_type or "auto").strip()
    if requested == "auto":
        return dimension, measure, automatic_type
    if requested not in SUPPORTED_VISUAL_TYPES:
        raise PowerBIProjectError(
            f"Unsupported visual type '{requested}'. Choose auto, lineChart, clusteredColumnChart, "
            "barChart, pieChart, donutChart, or tableEx."
        )
    if requested == "tableEx":
        return dimension, measure, "tableEx"
    if dimension and measure:
        return dimension, measure, requested
    # Do not invent chart mappings when the query lacks a category or numeric measure.
    return dimension, measure, "tableEx"


# --- Building blocks for visual.json -------------------------------------------------------
# In PBIR, a visual says which fields it shows through "projections". Each projection names a
# table ("Entity" = the model table, e.g. "billing vw_xxx") and a column ("Property").
#   queryRef       - Power BI's internal id for the field, "Table.column"
#   nativeQueryRef - the label shown to the user
# _projection_column: a plain column (used as chart category or table column).
def _projection_column(entity: str, field_name: str, active: bool = True) -> dict:
    result = {
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": entity}},
                "Property": field_name,
            }
        },
        "queryRef": f"{entity}.{field_name}",
        "nativeQueryRef": field_name,
    }
    if active:
        result["active"] = True
    return result


# _projection_sum: the same column wrapped in an aggregation, i.e. "Sum of <column>".
# In Power BI's query format, aggregation "Function": 0 means SUM.
def _projection_sum(entity: str, field_name: str) -> dict:
    return {
        "field": {
            "Aggregation": {
                "Expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": entity}},
                        "Property": field_name,
                    }
                },
                "Function": 0,
            }
        },
        "queryRef": f"Sum({entity}.{field_name})",
        "nativeQueryRef": f"Sum of {field_name}",
    }


# Readable label from a column name: "revenue_usd" -> "Revenue USD", "month_start" -> "Month Start".
def _label(column: str) -> str:
    return " ".join(w.upper() if w in ("usd", "id") else w.capitalize() for w in column.split("_") if w)


# Card look shared by the generated charts and tables (visualContainerObjects): white
# background, light rounded border, and a title in the brand ink colour.
def _container_style(title: str | None = None) -> dict:
    style = {
        "background": [{"properties": {"show": _literal("true"), "color": _color("#FFFFFF")}}],
        "border": [{"properties": {"show": _literal("true"), "color": _color(LINE), "radius": _literal("8D")}}],
    }
    if title:
        style["title"] = [{"properties": {"show": _literal("true"), "text": _text_literal(title),
                                          "fontColor": _color(INK), "fontSize": _literal("12D")}}]
    return style


# The page header: a native text box with the report title and a grey line below it. A text
# box can be selected and copied in Power BI Desktop, so the source view name is easy to copy.
def _header_textbox_json(name: str, title: str, subtitle: str,
                         x: float, y: float, width: float, height: float) -> dict:
    return {
        "$schema": VISUAL_SCHEMA,
        "name": name,
        "position": {"x": x, "y": y, "z": 50, "height": height, "width": width, "tabOrder": 0},
        "visual": {
            "visualType": "textbox",
            "objects": {"general": [{"properties": {"paragraphs": [
                {"textRuns": [{"value": title, "textStyle": {"fontWeight": "bold", "fontSize": "20pt",
                                                            "color": INK}}]},
                {"textRuns": [{"value": subtitle, "textStyle": {"fontSize": "10pt", "color": MUTED}}]},
            ]}}]},
            "drillFilterOtherVisuals": True,
        },
    }


def _chart_visual_json(entity: str, dimension: str, measure: str, visual_type: str,
                       visual_id: str, x: int, y: int, width: int, height: int,
                       sort_direction: str) -> dict:
    # Builds the whole visual.json content for one chart (line/column/bar/pie/donut).
    # "Category" bucket = X-axis / slices, "Y" bucket = the summed value.
    # x, y, width, height are in page pixels; the caller decides the layout.
    category = _projection_column(entity, dimension)
    measure_projection = _projection_sum(entity, measure)
    # Line charts are sorted by the time axis (so the line goes left-to-right in time);
    # other charts are sorted by the value (biggest bars/slices first when "Descending").
    sort_field = category["field"] if visual_type == "lineChart" else measure_projection["field"]
    visual_query = {
        "queryState": {
            "Category": {"projections": [category]},
            "Y": {"projections": [measure_projection]},
        },
        "sortDefinition": {
            "sort": [{"field": sort_field, "direction": sort_direction}],
            "isDefaultSort": True,
        },
    }
    visual = {"visualType": visual_type, "query": visual_query, "drillFilterOtherVisuals": True,
              "visualContainerObjects": _container_style(f"{_label(measure)} by {_label(dimension)}")}
    # One-series charts are drawn in the brand green; pie/donut slices keep the theme palette
    # (a single fill would make every slice the same colour).
    if visual_type not in ("pieChart", "donutChart"):
        visual["objects"] = {"dataPoint": [{"properties": {"fill": _color(BRAND_GREEN)}}]}
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json",
        "name": visual_id,
        # z = drawing layer (higher is on top); tabOrder = keyboard focus order.
        "position": {"x": x, "y": y, "z": 0, "height": height, "width": width, "tabOrder": 0},
        "visual": visual,
    }


def _table_visual_json(entity: str, columns: list[dict], visual_id: str,
                       x: int, y: int, width: int, height: int, tab_order: int = 1) -> dict:
    # visual.json for a native Table ("tableEx") listing every column of the view, unsummarised.
    projections = [_projection_column(entity, c["name"]) for c in columns]
    return {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.13.0/schema.json",
        "name": visual_id,
        "position": {"x": x, "y": y, "z": 1, "height": height, "width": width, "tabOrder": tab_order},
        "visual": {
            "visualType": "tableEx",
            "query": {"queryState": {"Values": {"projections": projections}}},
            # Column headers in a light brand tint.
            "objects": {"columnHeaders": [{"properties": {"backColor": _color("#F1F8EA"),
                                                          "fontColor": _color(INK)}}]},
            "visualContainerObjects": _container_style("Details"),
            "drillFilterOtherVisuals": True,
        },
    }


# How a name is written in TMDL: simple names as-is, other names inside single quotes
# (a quote inside the name is doubled, like in SQL).
def _tmdl_ref(name: str) -> str:
    if IDENTIFIER_RE.fullmatch(name):
        return name
    return "'" + name.replace("'", "''") + "'"


# Builds the text of tables/<table_entity>.tmdl. TMDL is indentation-based (tabs), a bit like YAML:
#   table 'billing vw_xxx'          <- the table
#       isHidden                     <- kept out of Desktop's Data pane (visuals still use it)
#       column revenue_usd           <- one block per view column: type, format, how to summarise
#       partition ... = m            <- where the rows come from, written in Power Query "M":
#           MySQL.Database(server, db) then pick the view by name; "mode: import" = copy the rows
#           into Power BI's in-memory model (they appear only after a refresh).
# lineageTag = a random GUID Power BI uses to track each object; any new unique value is fine.
# Returns the whole file as one string; generate_native_report_page() writes it to disk.
def _table_tmdl(table_entity: str, view_name: str, columns: list[dict], server: str, database: str) -> str:
    # Name of the M variable that holds the view's rows (must be a valid M identifier).
    safe_var = "q_" + re.sub(r"[^A-Za-z0-9_]", "_", view_name)
    # Power Query M string escaping uses doubled quotes.
    server_m = server.replace('"', '""')
    database_m = database.replace('"', '""')
    lines = [f"table '{table_entity}'", "\tisHidden", f"\tlineageTag: {uuid.uuid4()}", ""]
    for col in columns:
        name = col["name"]
        dtype, fmt = _powerbi_type(col.get("type", ""))
        lines.extend([
            f"\tcolumn {_tmdl_ref(name)}",
            f"\t\tdataType: {dtype}",
        ])
        if fmt:
            lines.append(f"\t\tformatString: {fmt}")
        # summarizeBy: numbers default to Sum when dragged onto a visual; text columns are not summarised.
        lines.extend([
            f"\t\tlineageTag: {uuid.uuid4()}",
            f"\t\tsummarizeBy: {'sum' if dtype in {'int64', 'double'} else 'none'}",
            f"\t\tsourceColumn: {_tmdl_ref(name)}",
            "",
        ])
        if dtype == "double":
            lines.append("\t\tannotation PBI_FormatHint = {\"isGeneralNumber\":true}")
            lines.append("")
    # The partition block. Note the f-string's doubled braces "{{" "}}" produce literal { } in M,
    # where Source{[Schema=...,Item=...]}[Data] means "the row for this view, then its data".
    lines.extend([
        f"\tpartition '{table_entity}' = m",
        "\t\tmode: import",
        "\t\tsource =",
        "\t\t\t\tlet",
        f'\t\t\t\t\tSource = MySQL.Database("{server_m}", "{database_m}", [ReturnSingleDatabase=true]),',
        f'\t\t\t\t\t{safe_var} = Source{{[Schema="{database_m}",Item="{view_name}"]}}[Data]',
        "\t\t\t\tin",
        f"\t\t\t\t\t{safe_var}",
        "",
        "\tannotation PBI_ResultType = Table",
        "",
    ])
    return "\n".join(lines)


# The same table TMDL with the table-level "isHidden" line (one tab, right under the "table ..."
# line, where Desktop writes it). Unchanged if the table is already hidden. Column-level
# isHidden lines have two tabs, so they are not mistaken for it.
def _hide_table_tmdl(text: str) -> str:
    lines = text.split("\n")
    if not lines or not lines[0].startswith("table ") or "\tisHidden" in (l.rstrip("\r") for l in lines):
        return text
    return "\n".join([lines[0], "\tisHidden", *lines[1:]])


def hide_generated_tables(model_dir: str | Path) -> list[str]:
    """Hide every AI-generated table ('billing vw_...') in the Data pane; original tables are not
    touched. Returns the names of the tables changed (empty if all were hidden already)."""
    # Called once by PowerBIService.__init__ at backend start-up, so tables generated before
    # tables were hidden (and anything Desktop un-hid on save) are hidden again. Desktop shows the
    # change after its next project reload.
    tables_dir = Path(model_dir).expanduser().resolve() / "definition" / "tables"
    changed = []
    for table_file in sorted(tables_dir.glob("billing vw_*.tmdl")):
        if not VIEW_RE.fullmatch(table_file.stem.removeprefix("billing ")):
            continue
        text = table_file.read_text(encoding="utf-8")
        hidden = _hide_table_tmdl(text)
        if hidden != text:
            table_file.write_text(hidden, encoding="utf-8")
            changed.append(table_file.stem)
    log.info("Hid %d generated table(s) in the Data pane", len(changed))
    return changed


# Registers a new table in model.tmdl. Two text edits (model.tmdl is not JSON, so plain string work):
#   1. add the line "ref table '<name>'" - without it Power BI ignores tables/<name>.tmdl
#   2. add the name to the "PBI_QueryOrder" annotation (the order of queries in Power Query editor)
# Both edits are skipped if already present, so calling it twice is harmless.
def _update_model_file(model_file: Path, table_entity: str) -> None:
    text = model_file.read_text(encoding="utf-8")
    ref_line = f"ref table '{table_entity}'"
    if ref_line not in text:
        # Put the new ref just before the culture line (where Desktop keeps its table refs);
        # if that line is not there, append it at the end instead.
        marker = "ref cultureInfo en-IN"
        if marker in text:
            text = text.replace(marker, ref_line + "\n" + marker, 1)
        else:
            text = text.rstrip() + "\n" + ref_line + "\n"

    # Find the line `annotation PBI_QueryOrder = [...]`; group(1) is the JSON list part.
    # (?m) makes ^ and $ match at each line, not only at the start/end of the file.
    match = re.search(r"(?m)^annotation PBI_QueryOrder = (\[.*\])\s*$", text)
    if match:
        try:
            order = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise PowerBIProjectError(f"Cannot update PBI_QueryOrder in {model_file}: {exc}") from exc
        if table_entity not in order:
            order.append(table_entity)
            # Replace only the list part of that line, keeping everything around it untouched.
            text = text[:match.start(1)] + json.dumps(order, ensure_ascii=False, separators=(",", ":")) + text[match.end(1):]
    else:
        # Query order is an optional annotation. Add one if the project lacks it.
        # (It is inserted before the time-intelligence annotation; if that line is also missing,
        # str.replace changes nothing and the model simply has no PBI_QueryOrder.)
        text = text.replace("annotation __PBI_TimeIntelligenceEnabled", f"annotation PBI_QueryOrder = {json.dumps([table_entity])}\n\nannotation __PBI_TimeIntelligenceEnabled", 1)
    model_file.write_text(text, encoding="utf-8")


# Turn the user's request/title into a tidy page tab name: collapse whitespace, drop a leading
# "please show/display/create/make/give me", cut to 60 chars, trim stray punctuation.
# Uses `fallback` (e.g. "Report vw_xxx") if nothing is left.
def _safe_display_name(title: str, fallback: str) -> str:
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    title = re.sub(r"^(please\s+)?(show|display|create|make|give me)\s+", "", title, flags=re.I)
    title = title[:60].strip(" .-_")
    return title or fallback


# Copy the existing files we are about to modify (model.tmdl, pages.json) into a new
# timestamped folder <parent>/.powerbi_generated_backups/<date_time_microseconds>/.
# Returns that folder; generate_native_report_page() restores from it if writing fails.
# Old backup folders are never deleted automatically.
def _backup_files(paths: Iterable[Path], parent: Path) -> Path:
    backup_dir = parent / ".powerbi_generated_backups" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_dir.mkdir(parents=True, exist_ok=True)
    for path in paths:
        if path.exists():
            # Include path name and original basename; each source file copied once.
            dest = backup_dir / f"{path.parent.name}__{path.name}"
            shutil.copy2(path, dest)
    return backup_dir


# Undo a failed edit: copy the backed-up model.tmdl / pages.json (names end in "__model.tmdl" /
# "__pages.json", see _backup_files) back over the possibly half-edited ones.
def _restore_backups(backup_dir: Path, model_file: Path, pages_meta_file: Path) -> None:
    for backup in backup_dir.iterdir():
        if backup.name.endswith("__model.tmdl"):
            shutil.copy2(backup, model_file)
        elif backup.name.endswith("__pages.json"):
            shutil.copy2(backup, pages_meta_file)


# Display names of the pages listed in pageOrder (used to keep page tab names unique).
def _page_names(pages_dir: Path, page_order: list) -> set:
    names = set()
    for page_id in page_order:
        path = pages_dir / page_id / "page.json"
        if path.exists():
            names.add(_json_read(path).get("displayName", ""))
    return names


# Page tab names should be unique: if "Monthly Revenue" exists, use "Monthly Revenue (2)",
# "(3)", ... (shortening the base so the whole name stays within 60 characters).
def _unique_display_name(display_name: str, existing_names: set) -> str:
    base_display = display_name
    suffix = 2
    while display_name in existing_names:
        tail = f" ({suffix})"
        display_name = base_display[: max(1, 60 - len(tail))] + tail
        suffix += 1
    return display_name


# ---------------------------------------------------------------------- navigation buttons
# Custom visuals (like the chatbot) cannot switch report pages by themselves, so this module
# writes Power BI's own "actionButton" visuals: "Open latest report" on the chatbot page and
# "Back to <chatbot page>" on each generated page.
#
# In visual.json, formatting values are not plain JSON values but small expressions:
#   {"expr": {"Literal": {"Value": "true"}}}      <- boolean/number literal
#   {"expr": {"Literal": {"Value": "'text'"}}}    <- text literal (quoted with ' inside the string)
# These three helpers build those shapes.
def _literal(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


# Text literal: wrap in single quotes and double any quote inside the text.
def _text_literal(text: str) -> dict:
    return _literal("'" + text.replace("'", "''") + "'")


# Solid colour value, e.g. _color("#118DFF") for Power BI blue.
def _color(hex_color: str) -> dict:
    return {"solid": {"color": _text_literal(hex_color)}}


def _nav_button_json(name: str, role: str, text: str, target_page: str | None, tooltip: str,
                     x: float, y: float, width: float, height: int, z: int = 200) -> dict:
    """Native Power BI button. With a target it navigates to that page; without one it is a
    grey, action-less placeholder. "Open latest report" is the brand-green primary button;
    "Back to ..." is a white secondary button, matching the toolbar on generated pages."""
    # Inputs: name = visual id/folder name; role = LATEST_BUTTON or BACK_BUTTON (stored in the
    # annotation below so the button can be found again later); target_page = page id to open.
    # "visualLink" is the button's Action setting. PageNavigation + navigationSection = "go to page".
    link = {"show": _literal("true" if target_page else "false")}
    if target_page:
        link.update(type=_text_literal("PageNavigation"), navigationSection=_text_literal(target_page))
    link["tooltip"] = _text_literal(tooltip)
    # (fill, text colour): primary = brand green, secondary = white, placeholder = grey.
    if not target_page:
        fill, ink = "#EEF0F2", MUTED
    elif role == BACK_BUTTON:
        fill, ink = "#FFFFFF", INK
    else:
        fill, ink = BRAND_GREEN, INK
    return {
        "$schema": VISUAL_SCHEMA,
        "name": name,
        "position": {"x": x, "y": y, "z": z, "height": height, "width": width, "tabOrder": 20},
        "visual": {
            "visualType": "actionButton",
            "objects": {
                "icon": [{"properties": {"shapeType": _text_literal("blank")}, "selector": {"id": "default"}}],
                "text": [
                    {"properties": {"show": _literal("true")}},
                    {"properties": {"text": _text_literal(text), "fontColor": _color(ink),
                                    "bold": _literal("true")},
                     "selector": {"id": "default"}},
                ],
                "fill": [
                    {"properties": {"show": _literal("true")}},
                    {"properties": {"fillColor": _color(fill), "transparency": _literal("0D")},
                     "selector": {"id": "default"}},
                ],
            },
            "visualContainerObjects": {
                "visualLink": [{"properties": link}],
                "border": [{"properties": {"show": _literal("true"), "color": _color(LINE if fill != BRAND_GREEN else BRAND_GREEN),
                                           "radius": _literal("8D")}}],
            },
            "drillFilterOtherVisuals": True,
        },
        # Our own marker: lets _find_nav_button() recognise buttons this module created,
        # even if the user moved or renamed them in Desktop.
        "annotations": [{"name": NAV_ANNOTATION, "value": role}],
    }


def _chatbot_mode_visual_json(name: str, mode: str, request_id: str, session_id: str | None, *,
                              x: float, y: float, z: int, width: float, height: float, tab_order: int) -> dict:
    """A copy of the chatbot custom visual that runs in `mode` ("feedback" or "save") for one report."""
    # No queryState/projections: the mode is driven entirely by the "feedback" object (declared
    # in billingChatbot/capabilities.json), so Power BI shows no field-name header and the visual
    # does not need a data field to be dragged in.
    return {
        "$schema": VISUAL_SCHEMA,
        "name": name,
        "position": {"x": x, "y": y, "z": z, "height": height, "width": width, "tabOrder": tab_order},
        "visual": {
            "visualType": CHATBOT_VISUAL_TYPE,
            "objects": {"feedback": [{"properties": {
                "mode": _text_literal(mode),
                "requestId": _text_literal(request_id),
                "sessionId": _text_literal(session_id or ""),
            }}]},
            # No Power BI frame around it (padding, border, background, title): the visual
            # draws its own bar / button edge to edge.
            "visualContainerObjects": {
                "padding": [{"properties": {side: _literal("0D") for side in ("top", "bottom", "left", "right")}}],
                "background": [{"properties": {"show": _literal("false")}}],
                "border": [{"properties": {"show": _literal("false")}}],
                "title": [{"properties": {"show": _literal("false")}}],
            },
            "drillFilterOtherVisuals": True,
        },
    }


def _json_write_atomic(path: Path, value: dict) -> None:
    """Replace a file in one step, so a failed write leaves the previous version intact."""
    # Write to "visual.json.tmp" first, then rename over the real file. A rename is atomic, so
    # Desktop never sees a half-written file.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _find_chatbot_page(pages_dir: Path, page_order: list) -> tuple[str, dict, dict] | None:
    """(page_id, page.json, chatbot visual.json) of the page hosting the chat (not feedback) visual."""
    # Look through pages in display order and open every visual.json. The chatbot page is the
    # first page with a chatbot custom visual WITHOUT a "feedback" object (generated report pages
    # also contain a chatbot visual, but in feedback mode). Returns None if there is none.
    for page_id in page_order:
        page_file = pages_dir / page_id / "page.json"
        if not page_file.is_file():
            continue
        for visual_file in sorted((pages_dir / page_id / "visuals").glob("*/visual.json")):
            try:
                visual = json.loads(visual_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # An unreadable/broken visual is just skipped; it cannot be the chatbot we want.
                continue
            body = visual.get("visual") or {}
            if body.get("visualType") == CHATBOT_VISUAL_TYPE and "feedback" not in (body.get("objects") or {}):
                return page_id, _json_read(page_file), visual
    return None


# Return the visual.json path of the button on this page whose annotation has the given role
# ("latestReport" / "backToAssistant"), or None if the page has no such button.
def _find_nav_button(page_dir: Path, role: str) -> Path | None:
    for visual_file in sorted((page_dir / "visuals").glob("*/visual.json")):
        try:
            visual = json.loads(visual_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if any(a.get("name") == NAV_ANNOTATION and a.get("value") == role
               for a in visual.get("annotations") or []):
            return visual_file
    return None


def _latest_button_box(page: dict, page_dir: Path, button_file: Path | None) -> tuple[float, float, float, int, int]:
    """(x, y, width, height, z): a fixed slot in the page's top-right corner, independent of where
    the chat visual is, layered above every other visual on the page so it is never hidden."""
    page_width = float(page.get("width", 1920))
    # Find the highest z (layer) used by any OTHER visual on the page (minimum 199), then put the
    # button one layer above it. The button itself is excluded so its z does not keep growing.
    top_z = 199
    for visual_file in (page_dir / "visuals").glob("*/visual.json"):
        if button_file is not None and visual_file == button_file:
            continue
        try:
            z = (json.loads(visual_file.read_text(encoding="utf-8")).get("position") or {}).get("z", 0)
            top_z = max(top_z, int(float(z)))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            # Unreadable file or a z that is not a number: ignore that visual.
            continue
    # Right edge minus button width minus margin; never negative on very narrow pages.
    x = max(0.0, page_width - LATEST_BUTTON_WIDTH - LATEST_BUTTON_MARGIN)
    return x, LATEST_BUTTON_MARGIN, LATEST_BUTTON_WIDTH, LATEST_BUTTON_HEIGHT, top_z + 1


def _write_latest_button(pages_dir: Path, chatbot: tuple[str, dict, dict],
                         target_page: str | None, target_name: str | None, *, replace: bool) -> bool:
    """Create (or, with replace=True, repoint) the chatbot page's "Open latest report" button.
    Without replace, an existing button keeps its label and target and is only moved back into
    its fixed slot. True if the file was written."""
    # `chatbot` is the tuple from _find_chatbot_page(); `_` = the chatbot visual (not needed here).
    # Called with replace=False by ensure_latest_report_button() at start-up, and with
    # replace=True by generate_native_report_page() once a new page has been written.
    page_id, page, _ = chatbot
    page_dir = pages_dir / page_id
    existing = _find_nav_button(page_dir, LATEST_BUTTON)
    x, y, width, height, z = _latest_button_box(page, page_dir, existing)
    if existing and not replace:
        # Only fix the position (keep label/target and other position keys like tabOrder).
        # `{**old, ...}` copies the old position dict and overrides the listed keys.
        button = _json_read(existing)
        position = {**(button.get("position") or {}), "x": x, "y": y, "z": z, "height": height, "width": width}
        if button.get("position") == position:
            # Already in the right place: write nothing, so the file (and Desktop) is not touched.
            return False
        button["position"] = position
        _json_write_atomic(existing, button)
        return True
    # (Re)write the whole button. Reuse the existing visual's id/file if there is one, otherwise
    # create it under the fixed id "aiOpenLatestReport".
    name = _json_read(existing)["name"] if existing else "aiOpenLatestReport"
    path = existing or page_dir / "visuals" / name / "visual.json"
    if target_page:
        text, tooltip = "Open latest report", f"Open the latest report: {target_name}"
    else:
        text, tooltip = "No report generated yet", "No report generated yet"
    _json_write_atomic(path, _nav_button_json(name, LATEST_BUTTON, text, target_page, tooltip,
                                              x, y, width, height, z))
    return True


def ensure_latest_report_button(report_dir: str | Path) -> bool:
    """Add the "No report generated yet" placeholder to the chatbot page when it has no
    "Open latest report" button, or move an existing one back into its fixed slot (its label and
    target are kept). True if anything was written."""
    # Public helper called once by PowerBIService.__init__ at backend start-up.
    # pages.json "pageOrder" = list of page ids (folder names under definition/pages) in tab order.
    pages_dir = Path(report_dir).expanduser().resolve() / "definition" / "pages"
    pages_meta = _json_read(pages_dir / "pages.json")
    chatbot = _find_chatbot_page(pages_dir, list(pages_meta.get("pageOrder", [])))
    if chatbot is None:
        log.warning("No page with the chatbot visual found in %s; 'Open latest report' button not placed",
                    pages_dir)
        return False
    written = _write_latest_button(pages_dir, chatbot, None, None, replace=False)
    log.info("'Open latest report' button %s", "placed/moved" if written else "already in place")
    return written


def generate_native_report_page(
    *,
    report_dir: str | Path,
    model_dir: str | Path,
    view_name: str,
    title: str,
    columns: list[dict],
    mysql_server: str = "localhost:3306",
    database: str = "billing",
    include_detail_table: bool = True,
    requested_visual_type: str | None = "auto",
    reuse_existing_table: bool = False,
    request_id: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Add a table for ``view_name`` to the TMDL model and a new page with native visuals.

    ``columns`` must look like ``[{"name": "refund_reason", "type": "varchar(50)"}, ...]``.
    With ``reuse_existing_table=True`` (used when the user only asks for a different chart type)
    the view's table must already exist in the model; only a new page is added.

    This function edits project files, but does not open Power BI Desktop or refresh the imported data.
    """
    # MAIN ENTRY POINT of this module, called by PowerBIService.build_page() (inside its lock).
    # The `*` in the signature means every argument must be passed by name.
    # Output: a dict describing what was created; "model_table" from it is later passed to
    # PowerBIService.reload_refresh(). Raises PowerBIProjectError for any expected problem.
    #
    # Phase 1: validate inputs and project layout (nothing is written yet).
    # resolve() makes absolute, normalised paths so they can be compared reliably below.
    report_dir = Path(report_dir).expanduser().resolve()
    model_dir = Path(model_dir).expanduser().resolve()
    if not VIEW_RE.fullmatch(view_name):
        raise PowerBIProjectError("view_name must be a generated vw_ name containing only letters, digits and underscores")
    if not report_dir.is_dir() or not model_dir.is_dir():
        raise PowerBIProjectError("Both report_dir and model_dir must point to existing Power BI project folders")

    report_def = report_dir / "definition"
    pages_dir = report_def / "pages"
    pages_meta_file = pages_dir / "pages.json"
    model_def = model_dir / "definition"
    model_file = model_def / "model.tmdl"
    tables_dir = model_def / "tables"
    if not pages_dir.is_dir() or not tables_dir.is_dir() or not model_file.is_file():
        raise PowerBIProjectError("Project folders are missing PBIR pages or TMDL model definitions")

    # Confirm the report's relative semantic-model reference resolves to the model being edited.
    definition_pbir = _json_read(report_dir / "definition.pbir")
    by_path = definition_pbir.get("datasetReference", {}).get("byPath", {}).get("path")
    if by_path:
        resolved_model = (report_dir / by_path).resolve()
        if resolved_model != model_dir:
            raise PowerBIProjectError(
                f"Report definition.pbir points to '{resolved_model}', not the supplied model folder '{model_dir}'."
            )

    # Clean the column list: every name must be a simple identifier and unique (Power BI tables
    # cannot have two columns with the same name). Missing types default to varchar(255) -> string.
    if not columns:
        raise PowerBIProjectError(f"MySQL view '{view_name}' returned no columns")
    normalized_cols = []
    seen = set()
    for raw in columns:
        name = _valid_identifier(str(raw.get("name", "")), "column name")
        if name in seen:
            raise PowerBIProjectError(f"Duplicate column name '{name}' in generated view")
        seen.add(name)
        normalized_cols.append({"name": name, "type": str(raw.get("type", "varchar(255)"))})

    # Name of the model table, e.g. "billing vw_monthly_revenue_ab12" (contains a space, which is
    # why it is quoted as 'billing vw_...' in TMDL). Its file is tables/billing vw_....tmdl.
    table_entity = f"billing {view_name}"
    table_file = tables_dir / f"{table_entity}.tmdl"
    # The table "exists" if either its file is there or model.tmdl already references it.
    # New view -> table must NOT exist yet; chart-type change (reuse) -> its file MUST exist.
    table_exists = table_file.exists() or f"ref table '{table_entity}'" in model_file.read_text(encoding="utf-8")
    if reuse_existing_table and not table_file.exists():
        raise PowerBIProjectError(f"Cannot reuse '{view_name}': its table is not in the semantic model")
    if table_exists and not reuse_existing_table:
        raise PowerBIProjectError(f"The semantic model already contains generated view '{view_name}'")

    pages_meta = _json_read(pages_meta_file)
    page_order = list(pages_meta.get("pageOrder", []))
    # Pick the existing report dimensions and active schema version as templates.
    first_page = None
    page_width, page_height = 1920, 1080
    page_schema = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.1.0/schema.json"
    for page_id in page_order:
        candidate = pages_dir / page_id / "page.json"
        if candidate.exists():
            first_page = _json_read(candidate)
            page_width = int(first_page.get("width", page_width))
            page_height = int(first_page.get("height", page_height))
            page_schema = first_page.get("$schema", page_schema)
            break
    if first_page is None:
        raise PowerBIProjectError("No existing report page definitions were found")

    display_name = _unique_display_name(_safe_display_name(title, f"Report {view_name}"),
                                        _page_names(pages_dir, page_order))

    # Random page id = the new page's folder name (retry in the very unlikely case it is taken).
    page_id = _uuid_hex(20)
    while (pages_dir / page_id).exists():
        page_id = _uuid_hex(20)
    page_folder = pages_dir / page_id
    visuals_dir = page_folder / "visuals"

    # Phase 2: write the NEW page's visual.json files. These are brand-new files, so if anything
    # fails later the whole page folder is simply deleted (see the rollback below).
    dimension, measure, chart_type = _select_requested_visual(normalized_cols, requested_visual_type)
    log.info("Page %r for %s: visual=%s (requested %s) dimension=%s measure=%s, %d columns",
             display_name, view_name, chart_type, requested_visual_type or "auto", dimension, measure,
             len(normalized_cols))
    visual_id = _uuid_hex(20)
    # Paths of every visual.json written, returned to the caller for logging/debugging.
    visual_files = []
    # Layout (page pixels), top to bottom: header, chart, detail table, toolbar - all with the
    # same margins. With a detail table the chart takes ~58% of the content height.
    content_x, content_w = PAGE_MARGIN, page_width - 2 * PAGE_MARGIN
    content_top = PAGE_MARGIN + HEADER_HEIGHT + LAYOUT_GAP
    toolbar_y = page_height - PAGE_MARGIN - TOOLBAR_HEIGHT
    content_bottom = toolbar_y - LAYOUT_GAP
    content_h = max(160, content_bottom - content_top)

    header_path = visuals_dir / "aiReportHeader" / "visual.json"
    _json_write(header_path, _header_textbox_json(
        "aiReportHeader", display_name,
        f"Generated {datetime.now():%d %b %Y, %H:%M}  ·  Source view: {view_name}",
        x=content_x, y=PAGE_MARGIN, width=content_w, height=HEADER_HEIGHT))
    visual_files.append(str(header_path))

    if dimension and measure and chart_type in SUPPORTED_VISUAL_TYPES - {"tableEx"}:
        sort_direction = "Ascending" if chart_type == "lineChart" else "Descending"
        chart_h = max(160, int((content_h - LAYOUT_GAP) * 0.58)) if include_detail_table else content_h
        chart_json = _chart_visual_json(
            table_entity, dimension, measure, chart_type, visual_id,
            x=content_x, y=content_top, width=content_w, height=chart_h,
            sort_direction=sort_direction,
        )
        chart_path = visuals_dir / visual_id / "visual.json"
        _json_write(chart_path, chart_json)
        visual_files.append(str(chart_path))
        if include_detail_table:
            # Second visual: a table with all columns, below the chart.
            table_id = _uuid_hex(20)
            table_y = content_top + chart_h + LAYOUT_GAP
            table_json = _table_visual_json(
                table_entity, normalized_cols, table_id,
                x=content_x, y=table_y, width=content_w,
                height=max(100, content_bottom - table_y), tab_order=1,
            )
            table_path = visuals_dir / table_id / "visual.json"
            _json_write(table_path, table_json)
            visual_files.append(str(table_path))
    else:
        # For a result without a usable dimension+measure pair, produce a detail table rather
        # than inventing a potentially misleading chart.
        table_json = _table_visual_json(
            table_entity, normalized_cols, visual_id,
            x=content_x, y=content_top, width=content_w, height=content_h,
        )
        table_path = visuals_dir / visual_id / "visual.json"
        _json_write(table_path, table_json)
        visual_files.append(str(table_path))

    # The toolbar row. Custom visuals cannot switch pages themselves, hence a native
    # "Back to <chatbot page>" button; "Save report" and the feedback bar are copies of the
    # chatbot custom visual (a native button cannot call the backend). The "feedback" formatting
    # object (declared in billingChatbot/capabilities.json) carries mode/requestId/sessionId.
    # Note: page_order does not contain the new page yet, so this finds the main chatbot page.
    chatbot = _find_chatbot_page(pages_dir, page_order)
    if chatbot is not None:
        # chatbot = (page_id, page.json dict, visual dict); [1] is page.json, [0] is the page id.
        chatbot_name = chatbot[1].get("displayName") or "AI Assistant"
        back_name = "aiBackToAssistant"
        back_json = _nav_button_json(back_name, BACK_BUTTON, f"← Back to {chatbot_name}"[:60], chatbot[0],
                                     f"Return to the {chatbot_name} page",
                                     x=PAGE_MARGIN, y=toolbar_y, width=BACK_BUTTON_WIDTH, height=BACK_BUTTON_HEIGHT)
        back_path = visuals_dir / back_name / "visual.json"
        _json_write(back_path, back_json)
        visual_files.append(str(back_path))

    if request_id:
        save_id = "aiSaveReport"
        save_x = PAGE_MARGIN + BACK_BUTTON_WIDTH + SAVE_VISUAL_GAP
        save_json = _chatbot_mode_visual_json(save_id, "save", request_id, session_id,
                                              x=save_x, y=toolbar_y, z=200,
                                              width=SAVE_VISUAL_WIDTH, height=TOOLBAR_HEIGHT, tab_order=21)
        save_path = visuals_dir / save_id / "visual.json"
        _json_write(save_path, save_json)
        visual_files.append(str(save_path))

        # "Was this useful?" bar, right-aligned in the same row.
        feedback_id = _uuid_hex(20)
        fb_width = max(320, min(FEEDBACK_VISUAL_WIDTH,
                                page_width - PAGE_MARGIN - (save_x + SAVE_VISUAL_WIDTH + LAYOUT_GAP)))
        feedback_json = _chatbot_mode_visual_json(feedback_id, "feedback", request_id, session_id,
                                                  x=page_width - PAGE_MARGIN - fb_width, y=toolbar_y, z=100,
                                                  width=fb_width, height=TOOLBAR_HEIGHT, tab_order=22)
        feedback_path = visuals_dir / feedback_id / "visual.json"
        _json_write(feedback_path, feedback_json)
        visual_files.append(str(feedback_path))

    # page.json for the new page. "FitToPage" scales the page to the window; size/schema are
    # copied from the first existing page so all pages look consistent.
    page_json = {
        "$schema": page_schema,
        "name": page_id,
        "displayName": display_name,
        "displayOption": "FitToPage",
        "height": page_height,
        "width": page_width,
    }
    page_file = page_folder / "page.json"
    _json_write(page_file, page_json)

    # Phase 3: edit the SHARED files (model.tmdl, pages.json) that already existed.
    # Backup current metadata before editing it. Generated files are new and don't need backups.
    # The backup folder is created next to the .Report folder (report_dir.parent).
    backup_dir = _backup_files([model_file, pages_meta_file], report_dir.parent)
    try:
        if not reuse_existing_table:
            table_text = _table_tmdl(table_entity, view_name, normalized_cols, mysql_server, database)
            table_file.write_text(table_text, encoding="utf-8")
            _update_model_file(model_file, table_entity)
        # Append the new page as the last tab. Without this entry Desktop would not show the page.
        if page_id not in page_order:
            page_order.append(page_id)
        pages_meta["pageOrder"] = page_order
        # Keep the user's current page active (normally the chatbot page) so the completion
        # message remains visible. The new report is still added to pageOrder and can be opened by name.
        _json_write(pages_meta_file, pages_meta)
        log.info("Wrote %s and page %s (%d visuals) to the project",
                 "reused table" if reuse_existing_table else f"table file {table_file.name}",
                 page_id, len(visual_files))
    except Exception:
        # Best-effort rollback of model metadata; generated page/table are removed to avoid partial output.
        log.exception("Writing the model/page metadata failed; rolling back from %s", backup_dir)
        # Undo: delete the new page folder and new table file, then put the backed-up
        # model.tmdl / pages.json back.
        try:
            shutil.rmtree(page_folder, ignore_errors=True)
            if not reuse_existing_table:
                table_file.unlink(missing_ok=True)
            _restore_backups(backup_dir, model_file, pages_meta_file)
        finally:
            # A bare `raise` re-raises the ORIGINAL error, so the caller still sees why it failed
            # (the `finally` makes this happen even if the rollback itself fails part-way).
            raise

    # Phase 4: point the chatbot page's "Open latest report" button at the new page.
    # Only now is the page complete, so only now does "Open latest report" move to it. Any
    # failure above leaves the button on the previous successful report.
    latest_button_updated = False
    if chatbot is not None:
        try:
            latest_button_updated = _write_latest_button(pages_dir, chatbot, page_id, display_name, replace=True)
            log.info("'Open latest report' button now targets %r", display_name)
        except (OSError, PowerBIProjectError, KeyError, TypeError, ValueError) as exc:
            # The report page itself is fine; the button keeps its old target.
            log.warning("Could not point 'Open latest report' at the new page: %s: %s", type(exc).__name__, exc)
            latest_button_updated = False
    else:
        log.warning("No chatbot page found: no back button, 'Open latest report' not updated")

    # Summary for the caller. The "status" value tells chatbot_api.py a reload is still needed;
    # "model_table" is the table the PowerShell script must refresh;
    # "needs_model_refresh" is False when the table (and its data) was reused.
    return {
        "status": "generated_pending_reload_and_refresh",
        "view_name": view_name,
        "model_table": table_entity,
        "page_id": page_id,
        "page_name": display_name,
        "visual_type": chart_type,
        "requested_visual_type": requested_visual_type or "auto",
        "dimension_field": dimension,
        "measure_field": measure,
        "visual_files": visual_files,
        "feedback_visual_added": bool(request_id),
        "save_visual_added": bool(request_id),
        "back_button_added": chatbot is not None,
        "latest_button_updated": latest_button_updated,
        "table_definition": str(table_file),
        "backup_dir": str(backup_dir),
        "needs_powerbi_reload": True,
        "needs_model_refresh": not reuse_existing_table,
        "reused_table": reuse_existing_table,
    }


# ---------------------------------------------------------------------- saved reports
# A saved report is a copy of one generated page's files (page.json + every visual.json) and of
# its model table's .tmdl file, kept in MySQL by app/saved_reports.py. Restoring writes them back
# unchanged, so the page looks exactly like it did when it was saved. The table still imports the
# live MySQL view, so the numbers are current after the refresh.

# The request id stored in a page's feedback/save chatbot visual, or None if the page has none.
def _page_request_id(page_dir: Path) -> str | None:
    for visual_file in sorted((page_dir / "visuals").glob("*/visual.json")):
        try:
            body = json.loads(visual_file.read_text(encoding="utf-8")).get("visual") or {}
            if body.get("visualType") != CHATBOT_VISUAL_TYPE:
                continue
            for entry in (body.get("objects") or {}).get("feedback") or []:
                value = entry["properties"]["requestId"]["expr"]["Literal"]["Value"]
                if isinstance(value, str) and len(value) >= 2:
                    return value[1:-1].replace("''", "'")     # undo _text_literal
        except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
            continue
    return None


# The chart type shown on a page: its first chart visual, else "tableEx" for a table-only page.
def _page_visual_type(page_dir: Path) -> str | None:
    types = []
    for visual_file in sorted((page_dir / "visuals").glob("*/visual.json")):
        try:
            types.append((json.loads(visual_file.read_text(encoding="utf-8")).get("visual") or {}).get("visualType"))
        except (OSError, json.JSONDecodeError):
            continue
    charts = [t for t in types if t in SUPPORTED_VISUAL_TYPES - {"tableEx"}]
    return charts[0] if charts else ("tableEx" if "tableEx" in types else None)


def find_report_page(report_dir: str | Path, request_id: str) -> str | None:
    """Id of the page (listed in pages.json) whose feedback/save visual belongs to request_id."""
    pages_dir = Path(report_dir).expanduser().resolve() / "definition" / "pages"
    pages_meta = _json_read(pages_dir / "pages.json")
    for page_id in pages_meta.get("pageOrder", []):
        page_dir = pages_dir / page_id
        if (page_dir / "page.json").is_file() and _page_request_id(page_dir) == request_id:
            return page_id
    return None


def export_report_page(*, report_dir: str | Path, model_dir: str | Path, request_id: str,
                       view_name: str) -> dict | None:
    """Copy of the generated page for request_id: its files as text plus the model table's TMDL.

    Returns None when no page of the project belongs to that request."""
    # Called by PowerBIService.export_page() (inside its lock) when the user clicks "Save report".
    if not VIEW_RE.fullmatch(view_name or ""):
        raise PowerBIProjectError("view_name must be a generated vw_ name containing only letters, digits and underscores")
    report_dir = Path(report_dir).expanduser().resolve()
    model_dir = Path(model_dir).expanduser().resolve()
    page_id = find_report_page(report_dir, request_id)
    if page_id is None:
        return None
    page_dir = report_dir / "definition" / "pages" / page_id
    # {"page.json": "...", "visuals/<id>/visual.json": "..."}: the exact text of every file.
    files = {}
    for path in sorted(page_dir.rglob("*.json")):
        relative = path.relative_to(page_dir).as_posix()
        if SAVED_FILE_RE.fullmatch(relative):
            files[relative] = path.read_text(encoding="utf-8")
    table_entity = f"billing {view_name}"
    table_file = model_dir / "definition" / "tables" / f"{table_entity}.tmdl"
    page = _json_read(page_dir / "page.json")
    log.info("Exported page %s (%d files) for request %s", page_id, len(files), request_id)
    return {
        "page_id": page_id,
        "page_name": page.get("displayName") or page_id,
        "visual_type": _page_visual_type(page_dir),
        "view_name": view_name,
        "model_table": table_entity,
        "files": files,
        "table_tmdl": table_file.read_text(encoding="utf-8") if table_file.is_file() else None,
    }


def restore_report_page(*, report_dir: str | Path, model_dir: str | Path, saved: dict) -> dict:
    """Put a page saved by export_report_page() back into the project.

    If the page is still in the report nothing is written (status "already_present"). Otherwise
    the page files are written back unchanged (with a new page id / tab name only if the old one
    is taken now), the model table is added again when it was removed, and the chatbot page's
    "Open latest report" button is pointed at the page (status "generated_pending_reload_and_refresh").
    """
    # Called by PowerBIService.restore_page() (inside its lock) when the user opens a saved report.
    report_dir = Path(report_dir).expanduser().resolve()
    model_dir = Path(model_dir).expanduser().resolve()
    view_name = saved.get("view_name") or ""
    if not VIEW_RE.fullmatch(view_name):
        raise PowerBIProjectError("The saved report has an invalid view name")
    pages_dir = report_dir / "definition" / "pages"
    pages_meta_file = pages_dir / "pages.json"
    model_file = model_dir / "definition" / "model.tmdl"
    tables_dir = model_dir / "definition" / "tables"
    if not pages_dir.is_dir() or not tables_dir.is_dir() or not model_file.is_file():
        raise PowerBIProjectError("Project folders are missing PBIR pages or TMDL model definitions")
    table_entity = f"billing {view_name}"
    pages_meta = _json_read(pages_meta_file)
    page_order = list(pages_meta.get("pageOrder", []))

    # Still there (under its saved id, or under any id if it was restored before): reuse it.
    page_id = str(saved.get("page_id") or "")
    if not (page_id in page_order and (pages_dir / page_id / "page.json").is_file()):
        page_id = find_report_page(report_dir, saved["request_id"]) if saved.get("request_id") else None
    if page_id:
        name = _json_read(pages_dir / page_id / "page.json").get("displayName") or page_id
        log.info("Saved report page %s (%r) is still in the report; nothing restored", page_id, name)
        return {"status": "already_present", "page_id": page_id, "page_name": name,
                "view_name": view_name, "model_table": table_entity}

    # Check every stored file before writing anything: plain relative paths and valid JSON only.
    files = saved.get("files") or {}
    if "page.json" not in files:
        raise PowerBIProjectError("The saved report has no page definition")
    for relative, text in files.items():
        if not isinstance(relative, str) or not SAVED_FILE_RE.fullmatch(relative) or not isinstance(text, str):
            raise PowerBIProjectError(f"The saved report contains an invalid file entry: {relative!r}")
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise PowerBIProjectError(f"The saved report file {relative} is not valid JSON: {exc}") from exc

    table_file = tables_dir / f"{table_entity}.tmdl"
    table_missing = not table_file.is_file()
    ref_missing = f"ref table '{table_entity}'" not in model_file.read_text(encoding="utf-8")
    table_tmdl = saved.get("table_tmdl")
    if table_missing and not table_tmdl:
        raise PowerBIProjectError(f"The model table for '{view_name}' is gone and was not saved")

    # Same id as before when it is free; page.json "name" must match the folder name.
    page_id = str(saved.get("page_id") or "")
    if not PAGE_ID_RE.fullmatch(page_id) or (pages_dir / page_id).exists():
        page_id = _uuid_hex(20)
        while (pages_dir / page_id).exists():
            page_id = _uuid_hex(20)
    page = json.loads(files["page.json"])
    page["name"] = page_id
    page["displayName"] = _unique_display_name(
        page.get("displayName") or saved.get("page_name") or f"Report {view_name}",
        _page_names(pages_dir, page_order))
    page_folder = pages_dir / page_id
    for relative, text in files.items():
        if relative == "page.json":
            _json_write(page_folder / "page.json", page)
        else:
            target = page_folder / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    backup_dir = _backup_files([model_file, pages_meta_file], report_dir.parent)
    try:
        if table_missing:
            # Reports saved before generated tables were hidden come back hidden too.
            table_file.write_text(_hide_table_tmdl(table_tmdl), encoding="utf-8")
        if table_missing or ref_missing:
            _update_model_file(model_file, table_entity)
        page_order.append(page_id)
        pages_meta["pageOrder"] = page_order
        _json_write(pages_meta_file, pages_meta)
        log.info("Restored saved page %s (%r)%s", page_id, page["displayName"],
                 " and its model table" if table_missing else "")
    except Exception:
        log.exception("Restoring the saved page failed; rolling back from %s", backup_dir)
        try:
            shutil.rmtree(page_folder, ignore_errors=True)
            if table_missing:
                table_file.unlink(missing_ok=True)
            _restore_backups(backup_dir, model_file, pages_meta_file)
        finally:
            raise

    latest_button_updated = False
    chatbot = _find_chatbot_page(pages_dir, page_order)
    if chatbot is not None:
        try:
            latest_button_updated = _write_latest_button(pages_dir, chatbot, page_id, page["displayName"], replace=True)
        except (OSError, PowerBIProjectError, KeyError, TypeError, ValueError) as exc:
            log.warning("Could not point 'Open latest report' at the restored page: %s: %s", type(exc).__name__, exc)
    return {
        "status": "generated_pending_reload_and_refresh",
        "page_id": page_id,
        "page_name": page["displayName"],
        "view_name": view_name,
        "model_table": table_entity,
        "table_restored": table_missing,
        "latest_button_updated": latest_button_updated,
        "backup_dir": str(backup_dir),
    }
