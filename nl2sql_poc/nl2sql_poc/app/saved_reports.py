"""Saved reports: keep a generated Power BI page so its owner can open it again later.

"Save report" (a button next to "Back to AI Assistant" on every generated page) copies the page's
files and its model table's TMDL into the saved_reports table. "Open" in the chat's "Saved reports"
list writes them back if the page was removed from the report, so it looks exactly like it did
when it was saved. The data is NOT frozen: the page imports the report's live MySQL view.

Only the owner sees, opens or deletes a saved report:
  * a verified user (identity.user_id) owns the reports saved under that user_id;
  * without a user_id (demo mode, or no identity provider) rows without a user_id match by role.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# Who calls it: chatbot_api.py (ChatService.save_report / saved_reports / open_saved_report /
#   delete_saved_report, behind the /reports/... routes). The Power BI reload after a restore is
#   scheduled by ChatService, because it reuses the chat session's "busy" handling.
# What it calls: report_store.py (metadata, view registry, saved_reports table) and
#   powerbi_service.py (export_page / restore_page, which hold the project lock).
# Every public method returns (payload, http_status) like ChatService does.
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import logging

from .clock import to_utc_naive, utc_now
from .guardrails import GuardrailError
from .identity import Identity
from .util import json_safe

log = logging.getLogger("chatbot.saved_reports")   # -> logs/backend_billing.log

DENIED = "Your role is not authorized to access this information."
NOT_FOUND = "That saved report was not found."


def owns(row: dict, identity: Identity) -> bool:
    """Does this metadata / saved_reports row belong to the caller? (owner rule: see module doc)"""
    if identity.user_id:
        return row.get("user_id") == identity.user_id
    return row.get("user_id") is None and row.get("user_role") == identity.user_role


class SavedReports:
    # check_sql(sql, role) -> the validated SQL text; raises GuardrailError when the role may not
    # read the report's tables. ChatService passes a wrapper around guardrails.validate_sql.
    def __init__(self, store, powerbi, check_sql, clock=None):
        self.store = store
        self.powerbi = powerbi
        self.check_sql = check_sql
        self.clock = clock or utc_now

    # ------------------------------------------------------------------ save
    def save(self, request_id: str, identity: Identity):
        """Copy the generated page of request_id into saved_reports (once; a second save is a no-op)."""
        record = self.store.fetch_metadata(request_id)
        if record is None or not owns(record, identity):
            log.warning("Save refused: request %s not found for this user", request_id)
            return {"error": "That report was not found."}, 404
        if record.get("request_status") != "completed" or not record.get("view_name"):
            return {"error": "Only a completed report can be saved."}, 409
        existing = self.store.fetch_saved_report(request_id)
        if existing is not None:
            return self._saved_payload(existing, "already_saved"), 200
        if not self.powerbi.enabled:
            return {"error": "Power BI page generation is turned off, so there is no page to save."}, 409

        exported = self.powerbi.export_page(request_id, record["view_name"])
        if exported is None:
            return {"error": "The report page was not found in the Power BI project."}, 404
        try:
            view = self.store.fetch_view_record(record["view_name"]) or {}
        except Exception:
            # The SQL is only needed for the role re-check / view recreation when opening.
            log.exception("Could not read the view registry for %s", record["view_name"])
            view = {}
        row = {
            "request_id": request_id, "session_id": record.get("session_id"),
            "user_id": record.get("user_id"), "user_role": record.get("user_role"),
            "title": exported["page_name"], "page_id": exported["page_id"],
            "visual_type": exported.get("visual_type"), "view_name": record["view_name"],
            "model_table": exported["model_table"], "sql_text": view.get("sql_text"),
            "request_text": view.get("question"), "files": exported["files"],
            "table_tmdl": exported.get("table_tmdl"), "saved_at": to_utc_naive(self.clock()),
        }
        try:
            self.store.insert_saved_report(row)
        except Exception:
            # Two quick clicks: the second INSERT hits the primary key. Anything else is real.
            existing = self.store.fetch_saved_report(request_id)
            if existing is None:
                raise
            return self._saved_payload(existing, "already_saved"), 200
        log.info("Report %s saved as %r (%d page files)", request_id, row["title"], len(row["files"]))
        return self._saved_payload(row, "saved"), 201

    @staticmethod
    def _saved_payload(row, status):
        return json_safe({"status": status, "saved": True, "request_id": row["request_id"],
                          "title": row["title"], "saved_at": row["saved_at"]})

    # ------------------------------------------------------------------ read
    def is_saved(self, request_id: str, identity: Identity):
        """For the Save button: has the caller already saved this report?"""
        row = self.store.fetch_saved_report(request_id)
        saved = row is not None and owns(row, identity)
        return json_safe({"request_id": request_id, "saved": saved,
                          "saved_at": row["saved_at"] if saved else None}), 200

    def list(self, identity: Identity):
        rows = self.store.list_saved_reports(identity.user_id, identity.user_role)
        return json_safe({"reports": rows}), 200

    # ------------------------------------------------------------------ open
    def open(self, request_id: str, identity: Identity):
        """Make the saved page available in the report again.

        Returns (payload, status, project). project is restore_report_page()'s result when the page
        was written back (the caller then reloads Desktop), else None."""
        row = self.store.fetch_saved_report(request_id)
        if row is None or not owns(row, identity):
            return {"error": NOT_FOUND}, 404, None
        if not self.powerbi.enabled:
            return {"error": "Power BI page generation is turned off in the configuration."}, 409, None
        # The role must still be allowed to read the report's tables (permissions may have changed).
        validated_sql = None
        if row.get("sql_text"):
            try:
                validated_sql = self.check_sql(row["sql_text"], identity.user_role)
            except GuardrailError as exc:
                log.warning("Opening saved report %s refused for role %s: %s", request_id, identity.user_role, exc)
                return {"error": DENIED}, 403, None
        else:
            log.warning("Saved report %s has no SQL; the role re-check is skipped", request_id)
        # Views are never dropped by the chatbot, but one may have been removed by hand.
        if not self.store.view_defined(row["view_name"]):
            if not validated_sql:
                return {"error": "The data behind this report no longer exists."}, 409, None
            log.info("Recreating missing view %s for saved report %s", row["view_name"], request_id)
            self.store.create_view(validated_sql, row["view_name"])

        project = self.powerbi.restore_page(row)
        try:
            self.store.touch_saved_report(request_id, to_utc_naive(self.clock()))
        except Exception:
            log.exception("Could not record when saved report %s was opened", request_id)
        page = project.get("page_name") or row["title"]
        if project.get("status") == "already_present":
            message = f"“{page}” is already in the report. Click its page tab to view it."
            return {"status": "already_present", "request_id": request_id, "page_name": page,
                    "message": message}, 200, None
        return {"status": "restored", "request_id": request_id, "page_name": page,
                "message": f"Restored the saved report “{page}”."}, 200, project

    # ------------------------------------------------------------------ delete
    def delete(self, request_id: str, identity: Identity):
        """Remove the saved copy only; the page (if still in the report) and the view stay."""
        row = self.store.fetch_saved_report(request_id)
        if row is None or not owns(row, identity):
            return {"error": NOT_FOUND}, 404
        self.store.delete_saved_report(request_id)
        log.info("Saved report %s deleted", request_id)
        return {"status": "deleted", "request_id": request_id}, 200
