#!/usr/bin/env python3
"""Adds the "no data -> don't build a blank page" check to your existing files.

Run it from the nl2sql_poc folder (the one that contains chatbot_api.py):

    python apply_no_data_patch.py

It only ADDS lines at exact anchor points, saves a backup next to every file it changes
(name.bak_before_no_data), and changes NOTHING if any anchor cannot be found (for example because
you edited that part). Safe to run twice.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MARKER = "no_data_message"

# (file, [(anchor, text inserted AFTER the anchor, required)])
EDITS = {
    "app/conversation.py": [
        ("from .util import fmt_number, humanize, is_number, json_safe\n",
         "from .coverage import DataCoverage, no_data_message\n", True),
        ('        self.ask_chart = bool((cfg.get("chat", {}) or {}).get("ask_chart_type", True))\n',
         '        # A query that matches no rows is reported (with the data\'s date range) instead of\n'
         '        # becoming a blank Power BI page. Set chat.skip_empty_reports: false to turn off.\n'
         '        self.skip_empty_reports = bool((cfg.get("chat", {}) or {}).get("skip_empty_reports", True))\n'
         '        self.coverage = DataCoverage.from_config(self.pipeline.db, cfg)\n', True),
        ("        result.cache_read_tokens += usage.cache_read_tokens\n",
         '        if result.status == "ok" and not result.rows and self.skip_empty_reports:\n'
         '            reply, chips = no_data_message(self.coverage, result.sql)\n'
         '            outcome = Outcome(kind="no_data", intent=intent, request=request, title=title,\n'
         '                              chart_type=chart or "auto", assumptions=assumptions, result=result,\n'
         '                              confidence=confidence, reply=reply, elapsed_ms=elapsed())\n'
         '            outcome.suggestions = chips\n'
         '            return outcome\n', True),
    ],
    "chatbot_api.py": [
        ('            elif outcome.kind in ("failed", "error"):\n'
         '                payload, status = self._failed(session, user_text, outcome)\n',
         '            elif outcome.kind == "no_data":\n'
         '                payload, status = self._no_data(session, user_text, outcome)\n', True),
        ("    def _failed(self, session, user_text, outcome):\n", None, True),   # handled specially below
    ],
    "config_billing.yaml": [
        ("  ask_chart_type: true", None, False),                                  # handled specially below
    ],
    "tests/test_chat_service.py": [
        ('def test_empty_result_is_called_out():\n'
         '    service, _, _, _ = make([REPORT, SQL], db=FakeDB(rows=[]))\n'
         '    payload, _ = chat(service, "monthly revenue")\n'
         '    assert "no rows" in payload["message"]\n'
         '    chips = payload["messages"][-1]["meta"]["suggestions"]\n'
         '    assert [c["label"] for c in chips] == ["Explain this report"]      # no chart chips for empty data\n',
         None, False),                                                           # replaced below
    ],
}

NO_DATA_METHOD = '''    def _no_data(self, session, user_text, outcome):
        """The query ran but matched no rows: say so (plus the data's date range) and build no page."""
        session.runtime_ms += outcome.elapsed_ms
        request_id = session.request_id
        try:
            request_id = self._ensure_row(session)
            self._safe_update(request_id, resolved_request=outcome.request, sql_source=self.source,
                              generated_sql=outcome.result.sql or None,
                              assumptions=" | ".join(outcome.assumptions) or None,
                              request_status="failed", result_row_count=0,
                              sql_runtime_ms=outcome.result.sql_runtime_ms,
                              total_runtime_ms=session.runtime_ms,
                              error_message="Query returned no rows - no view or page was created")
        except Exception:
            log.exception("Could not record the empty result")
        chips = getattr(outcome, "suggestions", None) or []
        session.add_message("assistant", outcome.reply, kind="text",
                            meta={"suggestions": chips} if chips else None)
        session.remember(user_text, f'{outcome.reply} [The request "{outcome.request}" returned no '
                                    "rows; no page was created.]")
        session.end_thread()
        return {"status": "no_data", "message": outcome.reply, "request_id": request_id,
                "result_row_count": 0}, 200

'''

NEW_TEST = '''def test_empty_result_is_called_out():
    service, _, store, powerbi = make([REPORT, SQL], db=FakeDB(rows=[]))
    payload, _ = chat(service, "monthly revenue")
    assert payload["status"] == "no_data" and "no rows" in payload["message"]
    assert not store.views and not powerbi.pages                  # no blank page is built
'''


def main() -> int:
    if not (ROOT / "app" / "coverage.py").is_file():
        print("STOP: app/coverage.py is missing. Copy it into the app folder first, then run this again.")
        return 1
    new_contents, notes = {}, []
    for rel, edits in EDITS.items():
        path = ROOT / rel
        if not path.is_file():
            if rel in ("app/conversation.py", "chatbot_api.py"):
                print(f"STOP: {rel} not found next to this script.")
                return 1
            notes.append(f"skipped {rel} (file not found)")
            continue
        text = path.read_text(encoding="utf-8")
        if rel in ("app/conversation.py", "chatbot_api.py") and (MARKER in text or "_no_data" in text):
            print(f"{rel}: already patched, nothing to do.")
            return 0
        for anchor, addition, required in edits:
            if text.count(anchor) != 1:
                if required:
                    print(f"STOP: in {rel} I could not find this line exactly once:\n    {anchor.strip()[:90]}\n"
                          "Nothing was changed. (You may have edited that spot; send me the lines around it.)")
                    return 1
                notes.append(f"skipped an optional edit in {rel} (anchor not found)")
                continue
            if rel == "chatbot_api.py" and anchor.startswith("    def _failed"):
                text = text.replace(anchor, NO_DATA_METHOD + anchor)
            elif rel == "config_billing.yaml":
                line_end = text.index("\n", text.index(anchor))
                text = (text[:line_end + 1]
                        + "  skip_empty_reports: true          # a request that matches no rows gets a message, not a blank page\n"
                        + text[line_end + 1:])
            elif rel == "tests/test_chat_service.py":
                text = text.replace(anchor, NEW_TEST)
            else:
                text = text.replace(anchor, anchor + addition)
        new_contents[path] = text
    for path, text in new_contents.items():
        backup = path.with_name(path.name + ".bak_before_no_data")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(text, encoding="utf-8")
        print(f"patched {path.relative_to(ROOT)}  (backup: {backup.name})")
    for note in notes:
        print("note:", note)
    print("\nDone. Restart the backend (python chatbot_api.py).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
