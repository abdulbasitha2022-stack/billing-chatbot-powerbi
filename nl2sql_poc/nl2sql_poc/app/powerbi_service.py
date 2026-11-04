"""Power BI project generation + Desktop automation, behind one small service object.

Order of operations for a new page:
  1. build_page():    save pending Desktop edits (prepare), then write the PBIR page / TMDL table
  2. reload_refresh(): ask Desktop to reload the project and refresh the new table

Step 2 makes Power BI Desktop re-create every visual (including the chatbot), so the API runs it
AFTER the chat reply has been stored and sent. The chatbot restores its conversation afterwards.
"""
# WHAT THIS FILE DOES (beginner overview)
# - It is the single "Power BI" object the Flask backend talks to. chatbot_api.py creates one
#   PowerBIService(cfg, BASE, store) at start-up and keeps it as `service.powerbi`.
# - chatbot_api.py calls:
#     * build_page(...)      -> right after the SQL has been saved as a MySQL view (report_store.py)
#     * reload_refresh(...)  -> later, on a background thread, so the chat answer is not delayed
#     * enabled / automation_enabled / reload_delay -> to decide what to do at all
# - It calls:
#     * powerbi_project_generator.py  -> writes the new page/table files into the PBIP folder on disk
#     * powerbi_desktop_automation.ps1 -> a PowerShell script (run as a child process) that talks to
#       the open Power BI Desktop window: save it, reload it, refresh the new table's data
#     * the report store (`store`)    -> to read the view's column names/types and the MySQL server name
# - Settings come from the `powerbi:` section of config_billing.yaml.
# - Log lines from `log` below go to logs/backend_billing.log; the PowerShell script writes its
#   own step-by-step log to logs/powerbi_automation.log.
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from .powerbi_project_generator import (PowerBIProjectError, ensure_latest_report_button,
                                        export_report_page, generate_native_report_page,
                                        hide_generated_tables, restore_report_page)

# Module logger; the handlers set up in logging_setup.py send it to logs/backend_billing.log.
log = logging.getLogger(__name__)
AUTOMATION_LOG_MAX_BYTES = 5 * 1024 * 1024    # powerbi_automation.log is moved to .1 above this


# PowerBIService: a small wrapper that hides all Power BI details from chatbot_api.py.
# Inputs when created:
#   cfg      - the whole parsed config_billing.yaml (only its "powerbi" section is used)
#   base_dir - the backend root folder (used to find app/*.ps1 and logs/)
#   store    - the ReportStore from report_store.py (gives view columns + MySQL server/database)
# It keeps no per-request state; the only shared thing is `self.lock`.
class PowerBIService:
    def __init__(self, cfg: dict, base_dir, store):
        # `self.cfg` = the "powerbi:" settings (enabled, report_dir, model_dir, include_detail_table...).
        # `or {}` guards against the YAML key existing but being empty (None).
        self.cfg = cfg.get("powerbi", {}) or {}
        # Nested "powerbi.desktop_automation:" settings (enabled, timeout_seconds, reload_delay_seconds).
        self.automation_cfg = self.cfg.get("desktop_automation", {}) or {}
        self.base = Path(base_dir)
        self.store = store
        # Flask can serve several requests at once (and reloads run on background threads).
        # Two threads writing pages.json / model.tmdl, or reloading Desktop at the same time,
        # could corrupt the project, so every disk edit and every Desktop reload holds this lock.
        self.lock = threading.Lock()      # one Power BI project edit / reload at a time
        # On start-up, make sure the chatbot page already has its "Open latest report" button
        # (a grey "No report generated yet" placeholder if nothing was generated before).
        if self.enabled and self.cfg.get("report_dir"):
            # Before any report exists the chatbot page shows "No report generated yet".
            # Power BI Desktop shows it after its next project (re)load.
            try:
                ensure_latest_report_button(self.cfg["report_dir"])
            except Exception:
                # A missing/broken project folder must not stop the whole backend from starting,
                # so any error is only logged (with traceback) and start-up continues.
                log.exception("Could not add the 'Open latest report' button to the chatbot page")
        # Keep the Data pane to the original tables: hide AI-generated tables made before
        # new ones were created hidden. Does nothing when they are hidden already.
        if self.enabled and self.cfg.get("model_dir"):
            try:
                hide_generated_tables(self.cfg["model_dir"])
            except Exception:
                log.exception("Could not hide the generated tables in the Data pane")

    # The three properties below are read-only "computed settings". Using @property means callers
    # write `service.powerbi.enabled` (no brackets) and always see the current config value.

    # enabled: master switch (powerbi.enabled). False = never touch the Power BI project at all.
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", False))

    # automation_enabled: True only if page generation AND Desktop automation are both switched on.
    # When False, pages are still written to disk but the user must reload Power BI Desktop by hand.
    @property
    def automation_enabled(self) -> bool:
        return self.enabled and bool(self.automation_cfg.get("enabled", False))

    # reload_delay: seconds chatbot_api.py waits before reloading Desktop, so the chat reply
    # reaches the visual first (the reload re-creates the chatbot visual).
    @property
    def reload_delay(self) -> float:
        return float(self.automation_cfg.get("reload_delay_seconds", 2))

    # ------------------------------------------------------------------ Desktop helper
    # Called by run_desktop_automation() before each script run. Simple log rotation: once the
    # file is larger than 5 MB it is renamed to "powerbi_automation.log.1" (replacing any older .1),
    # and the script starts a fresh file. OSError (file locked/permission) only produces a warning
    # because a big log file is not a reason to fail the user's request.
    def _rotate_automation_log(self):
        """The PowerShell script only appends; keep its step log (logs/powerbi_automation.log) bounded."""
        path = self.base / "logs" / "powerbi_automation.log"
        try:
            if path.is_file() and path.stat().st_size > AUTOMATION_LOG_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError as exc:
            log.warning("Could not rotate %s: %s", path, exc)

    # run_desktop_automation: runs app/powerbi_desktop_automation.ps1 once and returns its result.
    # Inputs:  action     - "status", "prepare" (save Desktop if it has unsaved edits) or
    #                       "reload_refresh" (reload the project and refresh one table's data)
    #          table_name - the model table to refresh (only needed for "reload_refresh")
    # Output:  the dict the script printed as JSON on its last stdout line, e.g. {"success": true, ...}
    # Raises:  PowerBIProjectError on any failure (script missing, timeout, non-zero exit, bad JSON).
    # Called by build_page() ("prepare") and reload_refresh() ("reload_refresh"), always under self.lock.
    def run_desktop_automation(self, action, table_name=None):
        timeout_seconds = int(self.automation_cfg.get("timeout_seconds", 180))
        script_path = self.base / "app" / "powerbi_desktop_automation.ps1"
        if not script_path.is_file():
            raise PowerBIProjectError(f"Power BI Desktop automation script not found: {script_path}")
        # Build the command line as a list (no shell string), so paths with spaces need no quoting.
        #   POWERSHELL_EXE env var lets you use e.g. pwsh.exe instead of Windows PowerShell 5.1.
        #   -NoProfile: skip the user's profile scripts (faster, predictable).
        #   -ExecutionPolicy Bypass: allow this unsigned local script to run for this process only.
        #   -Action/-ReportDir/-TimeoutSeconds/-TableName map to the script's param(...) block.
        command = [os.getenv("POWERSHELL_EXE", "powershell.exe"), "-NoProfile",
                   "-ExecutionPolicy", "Bypass", "-File", str(script_path),
                   "-Action", action, "-ReportDir", str(self.cfg["report_dir"]),
                   "-TimeoutSeconds", str(timeout_seconds)]
        if table_name:
            command.extend(["-TableName", str(table_name)])
        self._rotate_automation_log()
        log.info("Desktop automation '%s' started (table %s, timeout %d s)", action, table_name or "-",
                 timeout_seconds)
        started = time.perf_counter()
        # Small helper: milliseconds since `started`, used in the log messages below.
        elapsed = lambda: (time.perf_counter() - started) * 1000
        try:
            # Start PowerShell as a child process and wait for it to finish.
            #   capture_output=True: collect stdout/stderr instead of printing them.
            #   text/encoding/errors: decode the output as UTF-8; bad bytes become "?" not a crash.
            #   timeout: the script has its own internal timeout; +30 s gives it time to report its
            #            own error first. Python only kills it if it hangs beyond that.
            #   check=False: a non-zero exit code is handled manually below, not as an exception.
            completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=timeout_seconds + 30, check=False)
        # The two failures that can happen while *starting/waiting* for PowerShell are turned into
        # PowerBIProjectError (the "safe, user-facing" error type) with `from exc` so the original
        # cause is kept in the traceback written to backend_billing.log.
        except subprocess.TimeoutExpired as exc:
            log.error("Desktop automation '%s' timed out after %d ms "
                      "(see logs/powerbi_automation.log for the last step)", action, elapsed())
            raise PowerBIProjectError(
                f"Power BI Desktop automation timed out during '{action}'. "
                "Check Power BI Desktop and the model refresh status before retrying.") from exc
        except OSError as exc:
            # OSError here usually means powershell.exe (or POWERSHELL_EXE) could not be found/started.
            log.error("Could not launch PowerShell (%s) for '%s': %s", command[0], action, exc)
            raise PowerBIProjectError(
                f"Could not launch PowerShell for Power BI Desktop automation: {exc}") from exc

        # Protocol with the script: it may print other things, but its LAST non-empty stdout line
        # is always one JSON object ({"success": true/false, ...}). So only that line is parsed.
        lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        result = None
        if lines:
            try:
                result = json.loads(lines[-1])
            except json.JSONDecodeError:
                # Not JSON (e.g. PowerShell crashed before printing it) -> treated as failure below.
                result = None
        # Success needs all three: exit code 0, a JSON object, and "success": true in it.
        if completed.returncode != 0 or not isinstance(result, dict) or not result.get("success"):
            # Find the most useful error text: the script's own "error"/"message" field first,
            # otherwise the tail (last 2500 chars) of stderr/stdout.
            details = ""
            if isinstance(result, dict):
                details = str(result.get("error") or result.get("message") or "")
            if not details:
                details = (completed.stderr or completed.stdout or "No diagnostic output was returned.").strip()[-2500:]
            log.error("Desktop automation '%s' failed after %d ms (exit code %s): %s "
                      "(step details: logs/powerbi_automation.log)",
                      action, elapsed(), completed.returncode, details)
            if completed.stderr and completed.stderr.strip():
                # Full stderr only at DEBUG level, so the normal log stays short.
                log.debug("Desktop automation stderr:\n%s", completed.stderr.strip()[-4000:])
            raise PowerBIProjectError(f"Power BI Desktop automation '{action}' failed: {details}")
        log.info("Desktop automation '%s' finished in %d ms", action, elapsed())
        return result

    # ------------------------------------------------------------------ page generation
    # build_page: step 1 of "show the user a report".
    # Called by chatbot_api.py (_build_page) after the SQL result was saved as a MySQL view.
    # Inputs:  view_name  - e.g. "vw_monthly_revenue_ab12"; title - text used for the page name;
    #          requested_visual_type - "auto", "pieChart", ... (from the LLM's plan);
    #          reuse_existing_table  - True when only the chart type changes (same data, new page);
    #          request_id / session_id - stored in the page's feedback visual.
    # Output:  the dict from generate_native_report_page() (page_id, model_table, ...), or
    #          {"status": "disabled"} when Power BI generation is switched off.
    def build_page(self, view_name, title, requested_visual_type=None, reuse_existing_table=False, request_id=None, session_id=None):
        """Write the page (and table) into the PBIP project. Does NOT reload Desktop."""
        if not self.enabled:
            log.info("Power BI generation is disabled; no page for %s", view_name)
            return {"status": "disabled",
                    "message": "Power BI project generation is disabled in config_billing.yaml."}
        report_dir, model_dir = self.cfg.get("report_dir"), self.cfg.get("model_dir")
        if not report_dir or not model_dir:
            raise PowerBIProjectError(
                "powerbi.enabled is true but report_dir/model_dir are missing from config_billing.yaml.")
        # Defence in depth: the view name ends up in file names and in Power Query code, so only
        # the safe "vw_" + letters/digits/underscores shape created by report_store.py is accepted.
        if not re.fullmatch(r"vw_[A-Za-z0-9_]+", view_name):
            raise PowerBIProjectError("Generated view name has an unexpected format.")
        # Hold the lock for "save Desktop + write files" so no reload/other page write sneaks in between.
        with self.lock:
            if self.automation_enabled:
                # Save pending UI edits (e.g. deleted pages) before the generator writes to disk.
                # Otherwise the later reload would load our files and throw away the user's unsaved
                # edits (or a later Save in Desktop would overwrite our new page).
                self.run_desktop_automation("prepare")
            # [{"name": ..., "type": ...}, ...] read from MySQL (SHOW COLUMNS) for the saved view.
            columns = self.store.view_columns(view_name)
            return generate_native_report_page(
                report_dir=report_dir, model_dir=model_dir, view_name=view_name, title=title,
                columns=columns, mysql_server=self.store.server, database=self.store.database,
                include_detail_table=bool(self.cfg.get("include_detail_table", True)),
                requested_visual_type=requested_visual_type,
                reuse_existing_table=reuse_existing_table, request_id=request_id, session_id=session_id)

    # ------------------------------------------------------------------ saved reports
    # export_page / restore_page: called by app/saved_reports.py. Both first save pending Desktop
    # edits ("prepare", like build_page) so the files on disk match what the user sees.
    def _project_dirs(self):
        report_dir, model_dir = self.cfg.get("report_dir"), self.cfg.get("model_dir")
        if not report_dir or not model_dir:
            raise PowerBIProjectError(
                "powerbi.enabled is true but report_dir/model_dir are missing from config_billing.yaml.")
        return report_dir, model_dir

    def export_page(self, request_id, view_name):
        """Copy of the page generated for request_id (see export_report_page), or None if not found."""
        report_dir, model_dir = self._project_dirs()
        with self.lock:
            if self.automation_enabled:
                # A failed save only means unsaved Desktop edits are not in the copy; the page
                # on disk is still the one that was generated, so carry on.
                try:
                    self.run_desktop_automation("prepare")
                except PowerBIProjectError as exc:
                    log.warning("Saving Desktop before exporting the page failed; using the files on disk: %s", exc)
            return export_report_page(report_dir=report_dir, model_dir=model_dir,
                                      request_id=request_id, view_name=view_name)

    def restore_page(self, saved: dict) -> dict:
        """Write a saved page back into the project (see restore_report_page). Does NOT reload Desktop."""
        report_dir, model_dir = self._project_dirs()
        with self.lock:
            if self.automation_enabled:
                # Must succeed: the reload that follows would otherwise discard unsaved edits.
                self.run_desktop_automation("prepare")
            return restore_report_page(report_dir=report_dir, model_dir=model_dir, saved=saved)

    # reload_refresh: step 2. Called by chatbot_api.py on a background thread after the reply was sent
    # (and directly in one synchronous path). `generated` is build_page()'s result; its "model_table"
    # (e.g. "billing vw_...") is the table Desktop must refresh. Returns the script's JSON result.
    # Same lock as build_page, so a reload never runs while another page is being written.
    def reload_refresh(self, generated: dict) -> dict:
        with self.lock:
            return self.run_desktop_automation("reload_refresh", table_name=generated["model_table"])

