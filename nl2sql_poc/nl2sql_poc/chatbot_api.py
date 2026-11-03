#!/usr/bin/env python3
"""Flask API for the billing-report chatbot (what the Power BI visual talks to).

One conversational endpoint, POST /chat. For every message the assistant (app/conversation.py)
decides whether to chat, explain the last report, ask ONE clarifying question, build a new or
modified report, or just switch the chart type. The conversation (UI transcript, LLM memory, last
report) is kept per session on the server, so the visual can restore it after Power BI Desktop
reloads the project - the visual only has to remember its session id.

Endpoints
  POST /chat                         {session_id?, message, visual_type?, action?, choice?, platform?}
                                     action: change_chart | choose_chart | feedback_followup(choice=return|finish)
                                     409 {session_ended: true} when the session has ended
  POST /feedback                     {session_id, request_id, satisfaction: satisfied|unsatisfied}
  POST /session/<id>/end             {reason?}  end the session (history and records are kept)
  GET  /session/<id>/transcript      ?after=<last message id>   -> restore / poll the conversation
  GET  /session/latest               most recent ACTIVE session (fallback when the visual has no storage)
  POST /session/<id>/reset           old "New chat": now ends the session instead of deleting it
  GET  /session/<id>/progress        what the running turn is doing ("Report generation in progress...")
  GET  /session/<id>                 LLM memory (debugging)
  GET  /audit/sessions               every recorded conversation (newest first)
  GET  /audit/sessions/<id>          all questions and answers of one conversation (?format=text)
  GET  /audit/requests/<request_id>  one report request: its metadata + the questions/answers behind it
  POST /revise-report                {session_id, visual_type}  -> chart change without chatting
  POST /complete-report              manual-SQL test hook
  GET  /requests/<request_id>        metadata row
  POST /reports/save                 {request_id, demo_role?}  "Save report" button on a generated page
  GET  /reports/saved                ?demo_role=  the caller's saved reports (newest first)
  GET  /reports/saved/<request_id>   ?demo_role=  is this report saved? (the Save button's state)
  POST /reports/saved/<request_id>/open    {session_id?, demo_role?}  put the page back if it was removed
  POST /reports/saved/<request_id>/delete  {demo_role?}  forget the saved copy (page and view stay)
  GET  /health
Run:  python chatbot_api.py   (127.0.0.1:5056; set CHATBOT_HOST / CHATBOT_PORT / FLASK_DEBUG=1)
"""
# =====================================================================================
# HOW THE WHOLE PROJECT FITS TOGETHER  (start reading here)
# =====================================================================================
# This file is the ENTRY POINT of the backend. `python chatbot_api.py` runs main() at the
# bottom, which builds the Flask web app (create_app) and starts it on 127.0.0.1:5056.
#
# The modules, and what each one is responsible for:
#   billingChatbot/src/visual.ts           Power BI custom visual: the chat window the user types in.
#                                          It only talks HTTP to this file (POST /chat, GET /transcript ...).
#   chatbot_api.py  (this file)            HTTP endpoints + ChatService: validates input, checks who the
#                                          user is, keeps the per-session state, records metadata rows,
#                                          creates the view, builds the Power BI page, schedules the reload.
#   app/session.py                         ChatSession (one conversation held in memory) + SessionStore.
#   app/conversation.py                    ConversationEngine: asks the LLM to PLAN each turn (chat, explain,
#                                          clarify, build report, change chart) and returns an `Outcome`.
#   app/pipeline.py                        Pipeline: LLM prompts, SQL generation, runs the query, JSONL logs.
#   app/llm.py                             The LLM provider client (which model / API is used).
#   app/guardrails.py + app/access.py      Safety checks on SQL (read-only, allowed tables, row limit)
#                                          and role-based table permissions (CEO / Developer / Analyst).
#   app/db.py                              Runs the SQL read-only on MySQL.
#   app/report_store.py                    Saves the SQL as a MySQL VIEW and keeps the metadata table
#                                          (one row per report request: status, timings, feedback).
#   app/powerbi_project_generator.py       Writes the new Power BI report page / table to disk.
#   app/powerbi_service.py + .ps1 script   Tells Power BI Desktop to reload + refresh (PowerShell automation).
#   app/identity.py, app/audit.py,         Trusted identity, conversation recording, time helpers,
#   app/clock.py, app/naming.py, ...       view-name generation, logging setup.
#
# The path of ONE /chat request ("show revenue by month"):
#   1. visual.ts sends POST /chat {session_id, message, visual_type}.
#   2. chat() (Flask route in create_app) -> ChatService.handle_chat(): validates the body, resolves
#      the trusted identity, finds/creates the ChatSession and takes session.lock.
#   3. _handle_locked() -> ConversationEngine.handle() (app/conversation.py):
#        a. LLM call #1 "plan": what does the user want? (chat / clarify / report / ...)
#        b. if a report is ready -> Pipeline.generate_sql(): LLM call #2 writes the SQL,
#           guardrails.validate_sql() checks it, db.py runs it read-only -> rows.
#      It returns an Outcome(kind="report" | "clarify" | "reply" | "denied" | ...).
#   4. _finish_turn() dispatches on outcome.kind. For "report", _build_report():
#        - validates the SQL again for this role, creates the MySQL VIEW (_create_view),
#        - builds the Power BI page on disk (_build_page -> powerbi_service),
#        - updates the metadata row to "completed" / "failed",
#        - adds the assistant message to the session transcript,
#        - _schedule_reload(): a BACKGROUND thread later reloads Power BI Desktop (session.busy=True).
#   5. The HTTP reply goes back to the visual straight away (it does not wait for the reload).
#   6. The visual polls GET /session/<id>/progress (while /chat runs) and
#      GET /session/<id>/transcript (afterwards) to show the "Power BI has reloaded" message
#      that the background thread adds when it finishes (session.busy becomes False).
#
# Logging: every `log.xxx(...)` line in this file goes to the console AND, when configured,
# to logs/backend_billing.log (see app/logging_setup.py and the `logging:` block of
# config_billing.yaml). Each line carries sid=<session id> rid=<request id>.
# =====================================================================================
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, request

from app.audit import ConversationRecorder, format_conversation_text
from app.clock import app_timezone, to_day_id, to_utc_naive, utc_now
from app.conversation import DENIED_REPLY, ConversationEngine, Outcome, quick_take
from app.guardrails import GuardrailError, validate_sql
from app.identity import Identity, IdentityResolver
from app.logging_setup import bind_request, bind_session, clear_context, configure_logging
from app.naming import new_request_id, unique_view_name
from app.pipeline import VISUAL_TYPES, normalize_chart_type
from app.powerbi_project_generator import PowerBIProjectError
from app.report_store import ViewExistsError
from app.saved_reports import SavedReports
from app.session import ChatSession, SessionStore
from app.util import json_safe
from app.access import AccessController

# ---- module-level constants ----
BASE = Path(__file__).parent                       # backend folder; relative paths (logs/, config) start here
DEFAULT_CONFIG = BASE / "config_billing.yaml"      # used unless CHATBOT_CONFIG points elsewhere
ALLOWED_VISUAL_TYPES = set(VISUAL_TYPES)           # chart types the visual may request (a set: fast "in" checks)
# The "chatbot" logger: lines go to the console and logs/backend_billing.log (configured in main()).
log = logging.getLogger("chatbot")

# Human-readable names for the Power BI visual type ids (used in replies and page titles).
VISUAL_LABELS = {
    "auto": "Auto-select", "clusteredColumnChart": "Column chart", "barChart": "Bar chart",
    "lineChart": "Line chart", "pieChart": "Pie chart", "donutChart": "Donut chart",
    "tableEx": "Table",
}
# "Switch chart" buttons shown under a finished report (see _suggestions).
CHART_CHIPS = [("clusteredColumnChart", "Column"), ("barChart", "Bar"), ("lineChart", "Line"),
               ("pieChart", "Pie"), ("donutChart", "Donut"), ("tableEx", "Table only")]
# Buttons for the chart question that closes the clarification phase.
CHOICE_CHIPS = [{"label": label, "action": "choose_chart", "visual_type": visual_type, "message": label}
                for visual_type, label in [("auto", "Auto"), ("clusteredColumnChart", "Column chart"),
                                           ("barChart", "Bar chart"), ("lineChart", "Line chart"),
                                           ("pieChart", "Pie chart"), ("donutChart", "Donut chart"),
                                           ("tableEx", "Table only")]]


# Accepted reasons for ending a session; anything else is stored as "user" (see end_session).
END_REASONS = ("user", "new_chat", "finished_after_unsatisfied", "reset")
# The last message shown in the chat when a session ends.
SESSION_ENDED_TEXT = ("Session ended. Your conversation and report records are saved. "
                      "Start a new conversation whenever you're ready.")


# Turns a visual type id (e.g. "barChart") into a friendly label ("Bar chart"); None means "auto".
# Unknown ids fall back to the word "Chart".
def visual_label(visual_type) -> str:
    return VISUAL_LABELS.get(visual_type or "auto", "Chart")


# ChatService = the "business logic" behind every HTTP endpoint. The Flask routes in create_app()
# are thin wrappers: they read the JSON body and call one method here. Every public method
# returns a tuple (payload_dict, http_status) so it can be tested without a web server.
# One ChatService object is created at startup and shared by all requests (and threads).
class ChatService:
    # Inputs: cfg (the parsed YAML config), pipeline (app/pipeline.py: LLM + DB), store
    # (app/report_store.py: views + metadata table), powerbi (app/powerbi_service.py).
    # The optional arguments let tests inject fakes (e.g. a `background` that runs jobs inline,
    # a fixed `clock`).
    def __init__(self, cfg, pipeline, store, powerbi, sessions=None, background=None, recorder=None,
                 identity=None, clock=None):
        self.cfg = cfg
        self.recorder = recorder or ConversationRecorder(store, None)   # writes every Q&A to the audit log
        self.pipeline = pipeline
        self.store = store
        self.powerbi = powerbi
        self.sessions = sessions or SessionStore()        # in-memory dict of session_id -> ChatSession
        self.engine = ConversationEngine(pipeline, cfg)   # the LLM "brain" that plans each turn
        # Hook: the engine calls this just before it starts writing SQL, so we can mark the
        # metadata row "in_progress" (see _on_generation_started below).
        self.engine.on_generation_started = self._on_generation_started
        self.dialect = pipeline.db.dialect                # e.g. "mysql"; needed by the SQL parser in guardrails
        self.allowed_tables = cfg["db"]["allowed_tables"]  # global whitelist of tables SQL may read
        # AccessController knows which role may read which table. It is built from the live
        # database when there is one; tests with a fake DB get an empty controller instead.
        db_engine = getattr(pipeline.db, "engine", None)
        self.access = AccessController.from_engine(db_engine) if db_engine is not None else AccessController([])
        self.max_rows = int(cfg["db"].get("max_rows", 1000))   # row cap enforced by the guardrails
        self.source = f"{pipeline.llm.name}_api"           # stored as sql_source in the metadata row
        # Optional `chat:` section of the config; every setting has a default.
        chat_cfg = cfg.get("chat", {}) or {}
        self.allow_mock_sql = bool(chat_cfg.get("allow_mock_sql", True))   # enables the manual-SQL test hooks
        self.resume_latest = bool(chat_cfg.get("resume_latest_session", True))   # enables GET /session/latest
        self.resume_window_s = float(chat_cfg.get("resume_window_minutes", 480)) * 60   # minutes -> seconds
        self.end_wait_s = float(chat_cfg.get("end_wait_seconds", 2))   # how long "End" waits for a running turn
        self.tz = app_timezone(cfg)                  # local timezone, used to compute day_id
        self.clock = clock or utc_now                # "now" function (replaceable in tests)
        self.identity = identity or IdentityResolver(cfg)   # works out user/role from trusted headers
        self._background = background or self._spawn   # how background jobs are started (thread by default)
        # "Save report" / "Saved reports" (app/saved_reports.py). The lambdas read self.access and
        # self.clock at call time, so replacing them later (tests) also affects saved reports.
        self.saved = SavedReports(
            store, powerbi,
            check_sql=lambda sql, role: validate_sql(sql, self.dialect, self.allowed_tables, self.max_rows,
                                                     role=role, access=self.access).sql,
            clock=lambda: self.clock())

    # Default way to run a background job: a new daemon thread. "daemon" means the thread will
    # not keep the Python process alive when the server is shut down.
    @staticmethod
    def _spawn(fn):
        threading.Thread(target=fn, daemon=True, name="powerbi-reload").start()

    # ================================================================== /chat
    # Called by: the POST /chat route (and revise_report). Input: the JSON body sent by the
    # visual + the trusted identity. Output: (payload, http_status).
    # Steps: 1) validate every field (400 Bad Request on bad input), 2) check the demo role
    # (403 Forbidden), 3) find or create the session, 4) refuse if it belongs to someone else
    # or has ended (403 / 409 Conflict), 5) run the turn while holding the session lock.
    def handle_chat(self, body: dict, identity: Identity | None = None):
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return {"error": "message is required"}, 400
        message = message.strip()
        if len(message) > 2000:
            return {"error": "message is too long"}, 400
        session_id = body.get("session_id")
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()
                                       or len(session_id) > 64):
            return {"error": "session_id must be a non-empty string of 64 characters or fewer"}, 400
        visual_type = body.get("visual_type") or "auto"
        if not isinstance(visual_type, str) or visual_type not in ALLOWED_VISUAL_TYPES:
            return {"error": "visual_type must be one of: " + ", ".join(VISUAL_TYPES)}, 400
        action = body.get("action")
        if action not in (None, "change_chart", "choose_chart", "feedback_followup"):
            return {"error": "unsupported action"}, 400
        if action == "feedback_followup" and body.get("choice") not in ("return", "finish"):
            return {"error": "choice must be 'return' or 'finish'"}, 400
        # NOTE: a user name sent by the client (the old `requested_by` field) is deliberately
        # ignored. Identity comes only from a trusted source (see app/identity.py).

        # No identity passed (e.g. a direct call in tests): an anonymous identity, only the platform known.
        identity = identity or Identity(platform=self.identity.platform_for(body))
        # bind_session: from now on every log line of this request shows sid=<session id>.
        if session_id:
            bind_session(session_id.strip())
        # Demo mode: the user picks a role (CEO / Developer / Analyst) in the visual instead of
        # logging in. Without a valid role the chat is refused with 403 Forbidden.
        if self.identity.demo_role_selection:
            if not identity.demo_role:
                log.warning("Chat refused: no valid demo role was selected")
                return {"error": "Choose a valid demo role before starting or continuing a chat."}, 403
            # The visual did not send a role this time: reuse the role already stored on the session.
            if not identity.user_role and body.get("demo_role") is None and session_id:
                existing = self.sessions.get(session_id.strip())
                if existing and existing.demo_role_selected:
                    identity = Identity(user_role=existing.user_role,
                                        platform=identity.platform, demo_role=True)
            if identity.user_role not in ("CEO", "Developer", "Analyst"):
                log.warning("Chat refused: unknown demo role %r", identity.user_role)
                return {"error": "Choose a valid demo role before starting or continuing a chat."}, 403
        # Unknown or missing id -> a brand-new ChatSession (in memory only, see app/session.py).
        session = self.sessions.get_or_create(session_id.strip() if session_id else None)
        bind_session(session.session_id)
        bind_request(session.request_id)      # the open request being clarified, else none
        # A role cannot change in the middle of a conversation (it would mix permissions): 409.
        if (identity.demo_role and session.user_role
                and identity.user_role != session.user_role):
            log.warning("Chat refused: demo role change %s -> %s inside a session",
                        session.user_role, identity.user_role)
            return {"error": "A demo role cannot change during a chat. Start a new chat to switch roles."}, 409
        session.clock = self.clock
        # Make sure this user owns the session (returns a 403 tuple if not, else None).
        denied = self._claim_session(session, identity)
        if denied:
            return denied
        if session.ended:
            log.info("Chat refused: the session has ended")
            return self._ended_response(session)
        log.info("Chat message received: action=%s visual_type=%s role=%s chars=%d new_session=%s",
                 action or "-", visual_type, identity.user_role or session.user_role or "-",
                 len(message), not session.transcript)
        log.debug("Chat message text: %s", message)   # full text only at DEBUG level (can be sensitive)
        started = time.perf_counter()                  # high-resolution timer for the "in N ms" log line
        # session.lock (an RLock) makes sure only ONE request works on a session at a time. Flask
        # runs with threaded=True, so two messages (or the background reload) could otherwise
        # change the same transcript simultaneously. A second request simply waits here.
        with session.lock:
            if session.ended:                 # ended while this request was waiting for the lock
                return self._ended_response(session)
            try:
                payload, status = self._handle_locked(session, message, visual_type, action, body,identity)
                log.info("Chat turn finished: status=%s http=%s intent=%s in %d ms",
                         payload.get("status"), status, payload.get("intent") or "-",
                         (time.perf_counter() - started) * 1000)
                return payload, status
            finally:
                # Runs even if an exception escapes: clear the "what am I doing" text that the
                # visual polls via /progress, so it never shows a stale spinner message.
                session.progress = None
                session.turn_started = None

    # Called by handle_chat. Records who owns the session (first verified user wins) and copies
    # the role / platform onto it. Returns None when allowed, or a (payload, 403) tuple.
    def _claim_session(self, session: ChatSession, identity: Identity):
        """A session belongs to the first VERIFIED user who uses it; nobody else may use it."""
        if session.user_id and identity.user_id != session.user_id:
            log.warning("Chat refused: the session belongs to another user")
            return {"error": "This session belongs to another user."}, 403
        if identity.user_id and not session.user_id:
            session.user_id = identity.user_id
        if identity.user_role:
            session.user_role = identity.user_role
        if identity.demo_role:
            session.demo_role_selected = True
        if not session.platform:
            session.platform = identity.platform
        return None

    # Standard reply for any message sent to an ended session. HTTP 409 Conflict tells the
    # visual "your request conflicts with the session state"; it then offers a new chat.
    def _ended_response(self, session):
        return {"status": "session_ended", "session_ended": True, "session_id": session.session_id,
                "message": "This session has ended. Please start a new conversation."}, 409

    # Called by handle_chat while session.lock is held. Decides HOW to handle the message:
    #   - mock_sql in the body   -> test hook, skips the LLM entirely
    #   - action=feedback_followup -> the "Return / Finish" buttons after "Unsatisfied"
    #   - action=change_chart    -> chart switch button: no LLM needed, outcome built here
    #   - action=choose_chart    -> answer to "which chart?" question, goes to the engine
    #   - no action (normal text) -> ConversationEngine.handle() asks the LLM what to do
    # Every branch except the first two ends in _finish_turn(), which acts on the Outcome.
    def _handle_locked(self, session, message, visual_type, action, body,identity):
        # Remember the id of the last message BEFORE this turn, so we can later send back
        # only the messages this turn added (session.messages_after(first_new_id)).
        first_new_id = session.last_id
        session.turn_started = time.perf_counter()
        session.add_message("user", message)
        # session.progress: short status text the visual shows while waiting (GET /progress).
        session.progress = "Understanding your request…"

        mock_sql = body.get("mock_sql")
        if isinstance(mock_sql, str) and mock_sql.strip() and self.allow_mock_sql:
            return self._mock_sql_turn(session, message, mock_sql, first_new_id,identity)

        if action == "feedback_followup":
            if not self._can_reuse_report(session, identity):
                return {"error": "Your role is not authorized to access this information."}, 403
            return self._followup_turn(session, message, body.get("choice"), first_new_id)

        if action == "change_chart":
            # Re-check permissions: the role must still be allowed to read the last report's tables.
            if session.last_report and not self._can_reuse_report(session, identity):
                return {"error": "Your role is not authorized to access this information."}, 403
            chart = normalize_chart_type(visual_type)
            if chart in (None, "auto"):
                outcome = Outcome(kind="reply", intent="chat",
                                  reply="Which chart would you like: column, bar, line, pie, donut or table?")
            elif not session.last_report:
                outcome = Outcome(kind="reply", intent="chat",
                                  reply="I don't have a report to change yet. What would you like to see?")
            else:
                outcome = Outcome(kind="chart_change", intent="change_chart", chart_type=chart)
        # The role always comes from the session, which _claim_session fills only from the
        # trusted identity (IdentityResolver); never from the message body.
        elif action == "choose_chart":
            outcome = self.engine.handle(
                session,
                message,
                chart_choice=visual_type,
                role=session.user_role,
                access=self.access,
            )
        else:
            outcome = self.engine.handle(
                session,
                message,
                preferred_chart=visual_type,
                role=session.user_role,
                access=self.access,
            )
        return self._finish_turn(session, message, outcome, first_new_id)

    def _can_reuse_report(self, session, identity):
        """Recheck permissions before an operation reuses a report's data or SQL."""
        # Runs the stored SQL through the same guardrail as a new query, but for THIS role.
        # validate_sql raises GuardrailError when the role may not read a table -> return False.
        if not identity or not identity.user_role or not session.last_report:
            return False
        try:
            validate_sql(session.last_report.get("sql", ""), self.dialect,
                         self.allowed_tables, self.max_rows,
                         role=identity.user_role, access=self.access)
            return True
        except GuardrailError as exc:
            log.warning("Role %s may not reuse the last report: %s", identity.user_role, exc)
            return False

    # ------------------------------------------------------------------ turn dispatch
    # Called by _handle_locked with the Outcome decided by the engine. Each outcome.kind has
    # its own handler that updates the session, the metadata row and returns (payload, status).
    # Plain chat replies ("reply" / anything else) are handled inline in the final `else`.
    # Output: the payload wrapped by _payload() (adds session id, new messages, busy, ...).
    def _finish_turn(self, session: ChatSession, user_text: str, outcome: Outcome, first_new_id: int):
        try:
            if outcome.kind == "report":
                payload, status = self._build_report(session, user_text, outcome)
            elif outcome.kind == "chart_change":
                payload, status = self._change_chart(session, user_text, outcome)
            elif outcome.kind == "clarify":
                payload, status = self._clarify(session, user_text, outcome)
            elif outcome.kind == "choose_chart":
                payload, status = self._ask_chart(session, user_text, outcome)
            elif outcome.kind == "no_data":
                payload, status = self._no_data(session, user_text, outcome)
            elif outcome.kind == "denied":
                payload, status = self._denied(session, user_text, outcome)
            elif outcome.kind in ("failed", "error"):
                payload, status = self._failed(session, user_text, outcome)
            else:
                # add_message -> the UI transcript; remember -> the LLM's memory of the chat;
                # log_turn -> one JSON line in the requests JSONL log (app/pipeline.py).
                session.add_message("assistant", outcome.reply)
                session.remember(user_text, outcome.reply)
                self.pipeline.log_turn(question=user_text, status="chat", intent=outcome.intent,
                                       prompt_score=outcome.confidence, latency_s=outcome.elapsed_ms / 1000)
                payload, status = {"status": "chat", "message": outcome.reply}, 200
        except Exception as exc:   # never lose the conversation because a side effect failed
            # Anything unexpected (DB down while creating the view, disk error, a bug...) lands
            # here. log.exception writes the full traceback to logs/backend_billing.log; the
            # user gets a friendly message, the metadata row is marked failed, and HTTP 500.
            log.exception("Turn failed after planning")
            text = ("Something went wrong while finishing that request. Please try again. "
                    "The details are in the backend log.")
            failed_request_id = session.request_id
            if failed_request_id:
                self._finish_row(session, failed_request_id, "failed",
                                 error=f"Unexpected error: {type(exc).__name__}: {str(exc)[:500]}")
            session.add_message("assistant", text, kind="error")
            session.remember(user_text, text)
            session.end_thread()     # forget the half-finished request so the next message starts fresh
            payload, status = {"status": "error", "message": text, "request_id": failed_request_id}, 500
        # session.end_requested is set by end_session() when the user clicked End but could not
        # get the lock because this turn was running. We honour it now that the turn is done.
        if session.end_requested and not session.ended:      # End was clicked while this ran
            self._end_session(session, session.end_reason or "user")
        return self._payload(session, first_new_id, outcome, payload), status

    # Builds the final JSON for /chat: copies the handler's payload, adds the fields every reply
    # carries (session_id, the new transcript messages, busy, session_ended, provider ...),
    # and records the new messages in the audit log. json_safe() converts values such as
    # dates and Decimals into something jsonify() can serialise.
    def _payload(self, session, first_new_id, outcome, payload):
        payload = dict(payload)                       # copy, so the caller's dict is not modified
        payload.setdefault("reply", payload.get("message"))
        entries = session.messages_after(first_new_id)
        self.recorder.record(session, entries, request_id=payload.get("request_id"),
                             intent=outcome.intent)
        payload.update(session_id=session.session_id, intent=outcome.intent,
                       messages=json_safe(entries), busy=session.busy,
                       session_ended=session.ended, prompt_score=outcome.confidence,
                       provider=self.pipeline.llm.name)
        return json_safe(payload)

    # ------------------------------------------------------------------ timing
    # A report request can span several turns (clarifying questions). Timing excludes the time
    # the user spends typing: session.runtime_ms banks the processing time of earlier turns.
    # _turn_ms: milliseconds since the current turn arrived (0 if no turn is running).
    def _turn_ms(self, session) -> int:
        return int((time.perf_counter() - session.turn_started) * 1000) if session.turn_started else 0

    def _response_ms(self, session) -> int:
        """Processing time of the whole request so far: earlier turns of the same request (their
        arrival-to-response time, excluding the user's thinking time) plus this turn."""
        return session.runtime_ms + self._turn_ms(session)

    def _close_turn(self, session) -> None:
        """The request continues in a later turn: bank this turn's time."""
        session.runtime_ms += self._turn_ms(session)

    # ------------------------------------------------------------------ request metadata rows
    # ONE row per report-generation request, created once and then only updated.
    #   initiated   row created (request recognised; may still be clarifying / waiting for a chart)
    #   in_progress SQL generation started / view being created
    #   completed   view created and the Power BI page generated (or page generation disabled)
    #   failed      anything went wrong; error_message says what
    # Returns the request_id of the session's open report request, creating its metadata row
    # (INSERT into the metadata table via report_store) the first time it is needed.
    # session.request_id = id of the report request currently being clarified/built (None when idle).
    def _ensure_row(self, session: ChatSession) -> str:
        if session.request_id is None:
            # new_request_id(): a random 12-character hex id (the metadata table's primary key,
            # also used in the view name), unique without asking the database.
            started = session.thread_started or self.clock()
            request_id = new_request_id()
            bind_request(request_id)
            self.store.insert_metadata(
                session_id=session.session_id, request_id=request_id, user_id=session.user_id,
                user_role=session.user_role, platform=session.platform or self.identity.default_platform,
                sql_source=self.source, request_ts=to_utc_naive(started),
                day_id=to_day_id(started, self.tz), request_status="initiated")
            session.request_id = request_id
            log.info("Report request created (status initiated)")
        bind_request(session.request_id)
        return session.request_id

    # UPDATE the metadata row, but never let a database error break the user's turn: the
    # failure is only logged (with traceback) to logs/backend_billing.log.
    def _safe_update(self, request_id, **fields):
        try:
            self.store.update_metadata(request_id, **fields)
            log.debug("Metadata %s updated: %s", request_id,
                      ", ".join(f"{k}={v}" for k, v in fields.items() if k != "error_message"))
        except Exception:
            log.exception("Could not update metadata for %s", request_id)

    # Writes the FINAL state of a request row: status (completed/failed), total response time,
    # optional view name and error text (cut to 4000 chars to fit the column).
    def _set_final(self, request_id, status, response_ms, view_name=None, error=None):
        fields = {"request_status": status, "response_time_ms": int(response_ms),
                  "error_message": error[:4000] if error else None}
        if view_name:
            fields["view_name"] = view_name
        if error:
            log.warning("Request %s finished as %s after %d ms: %s", request_id, status, response_ms, error[:500])
        else:
            log.info("Request %s finished as %s after %d ms", request_id, status, response_ms)
        self._safe_update(request_id, **fields)

    # Same as _set_final, but works out the response time from the session's timers by default.
    def _finish_row(self, session, request_id, status, *, view_name=None, error=None, response_ms=None):
        self._set_final(request_id, status,
                        self._response_ms(session) if response_ms is None else response_ms,
                        view_name=view_name, error=error)

    def _on_generation_started(self, session: ChatSession) -> None:
        """Engine hook: SQL generation is starting, so the request is now in progress."""
        try:
            self._safe_update(self._ensure_row(session), request_status="in_progress")
        except Exception:
            log.exception("Could not mark the request in progress")

    # ------------------------------------------------------------------ clarify / failure
    # outcome.kind == "clarify": the LLM needs ONE more detail. The request stays open (no
    # end_thread), so the user's next answer continues the same request and metadata row.
    def _clarify(self, session, user_text, outcome):
        self._close_turn(session)      # bank this turn's time; the user's typing time is not counted
        try:
            self._ensure_row(session)
        except Exception:
            log.exception("Could not record the clarification request")
        log.info("Asking a clarifying question (#%d, score %s)", session.clarify_count, outcome.confidence)
        session.add_message("assistant", outcome.reply, kind="clarify")
        session.remember(user_text, outcome.reply)
        self.pipeline.log_turn(question=user_text, status="clarify", intent="clarify",
                               prompt_score=outcome.confidence, latency_s=outcome.elapsed_ms / 1000)
        return {"status": "clarify", "message": outcome.reply, "request_id": session.request_id}, 200

    def _ask_chart(self, session, user_text, outcome):
        """Everything is clarified; ask for the chart type. No SQL has run yet."""
        self._close_turn(session)
        try:
            self._ensure_row(session)
        except Exception:
            log.exception("Could not record the chart question")
        log.info("Request clarified; asking for the chart type")
        # meta.suggestions = clickable chart buttons (CHOICE_CHIPS) the visual renders under the message.
        session.add_message("assistant", outcome.reply, kind="clarify",
                            meta={"suggestions": CHOICE_CHIPS})
        session.remember(user_text, outcome.reply)
        self.pipeline.log_turn(question=user_text, status="awaiting_chart_choice",
                               intent="choose_chart", prompt_score=outcome.confidence,
                               latency_s=outcome.elapsed_ms / 1000)
        return {"status": "awaiting_chart_choice", "message": outcome.reply,
                "request_id": session.request_id}, 200

    def _no_data(self, session, user_text, outcome):
        """The query ran but matched no rows: say so (plus the data's date range) and build no page."""
        request_id = session.request_id
        try:
            request_id = self._ensure_row(session)
            self._finish_row(session, request_id, "failed",
                             error="Query returned no rows - no view or page was created")
        except Exception:
            log.exception("Could not record the empty result")
        log.info("Query returned no rows; no view or page created")
        chips = getattr(outcome, "suggestions", None) or []
        session.add_message("assistant", outcome.reply, kind="text",
                            meta={"suggestions": chips} if chips else None)
        # The [bracketed note] is only for the LLM's memory, so it knows no page exists.
        session.remember(user_text, f'{outcome.reply} [The request "{outcome.request}" returned no '
                                    "rows; no page was created.]")
        session.end_thread()           # the request is finished: clear request_id and clarification state
        return {"status": "no_data", "message": outcome.reply, "request_id": request_id,
                "result_row_count": 0}, 200

    # outcome.kind "failed" (the LLM could not produce working SQL -> HTTP 400) or "error"
    # (a technical problem such as the LLM API being down -> HTTP 500). Marks the row failed.
    def _failed(self, session, user_text, outcome):
        status = "error" if outcome.kind == "error" else "failed"
        request_id = session.request_id
        try:
            if outcome.result is not None:        # a report attempt: make sure it has its row
                request_id = self._ensure_row(session)
            if request_id:
                kind = "Technical error" if status == "error" else "Query generation failed"
                self._finish_row(session, request_id, "failed", error=f"{kind}: {outcome.error}")
        except Exception:
            log.exception("Could not record the failed request")
        # Picks the log level: ERROR for technical errors, WARNING for "could not answer".
        (log.error if status == "error" else log.warning)("Turn %s: %s", status, outcome.error)
        # When result exists, the pipeline already logged the SQL attempt itself.
        if outcome.result is None:
            self.pipeline.log_turn(question=user_text, status="error", intent="error", error=outcome.error)
        session.add_message("assistant", outcome.reply, kind="error")
        session.remember(user_text, outcome.reply)
        session.end_thread()
        return ({"status": status, "message": outcome.reply, "request_id": request_id},
                500 if status == "error" else 400)

    def _denied(self, session, user_text, outcome):
        """The request needs data the role may not read: close it and drop everything pending
        (clarification thread, chart question) so a follow-up answer cannot continue it."""
        request_id = session.request_id
        try:
            if outcome.result is not None:        # refused by the SQL guardrail: record the attempt
                request_id = self._ensure_row(session)
            if request_id:
                self._finish_row(session, request_id, "failed", error=outcome.error)
        except Exception:
            log.exception("Could not record the denied request")
        log.warning("Access denied for role %s: %s", session.user_role, outcome.error)
        self.pipeline.log_turn(question=user_text, status="denied", intent="denied", error=outcome.error,
                               latency_s=outcome.elapsed_ms / 1000)
        session.add_message("assistant", outcome.reply, kind="error")
        session.remember(user_text, f"{outcome.reply} [Access denied. This request is closed; "
                                    "do not continue it or ask about it again.]")
        session.end_thread()
        return {"status": "failed", "access_denied": True, "message": outcome.reply,
                "request_id": request_id}, 400

    # ------------------------------------------------------------------ new / modified report
    # True if a MySQL view with this name already exists. If the check itself fails we assume
    # the name is free; create_view will raise ViewExistsError later if it really was taken.
    def _view_taken(self, name: str) -> bool:
        try:
            return bool(self.store.view_exists(name))
        except Exception as exc:
            log.debug("Could not check whether view %s exists (%s); assuming it is free", name, exc)
            return False

    def _create_view(self, session, request_id, title, day, sql, question, assumptions,
                     session_id=None, used=None):
        """Pick the final view name, create the view, register it. Returns (name, ms).

        The SAME name is used for the CREATE VIEW, the registry, the metadata row and the reply."""
        # Called by _build_report (with a session) and complete_manual_report (session=None).
        session_id = session_id or (session.session_id if session is not None else None)
        # Names we already know are unusable: passed in + the views of this session's reports.
        taken_names = set(used or ())
        if session is not None:
            taken_names |= {r.get("view_name") for r in session.reports}
            session.progress = "Report generation in progress: saving the report view…"
        # Up to 3 attempts: another request may create a view with the same name between our
        # check and our CREATE VIEW (a "race"); then we mark it taken and try the next name.
        for _ in range(3):
            name = unique_view_name(title, day, request_id,
                                    lambda n: n in taken_names or self._view_taken(n))
            started = time.perf_counter()
            try:
                self.store.create_view(sql, name)
            except ViewExistsError:           # lost a race for the name: try the next suffix
                log.warning("View name %s was taken meanwhile; trying the next one", name)
                taken_names.add(name)
                continue
            elapsed = round((time.perf_counter() - started) * 1000)
            log.info("Created view %s in %d ms", name, elapsed)
            # Register the view (question, SQL, assumptions) and put its name on the metadata row.
            self.store.log_view(session_id, question, sql, name, assumptions)
            self._safe_update(request_id, view_name=name, request_status="in_progress")
            return name, elapsed
        log.error("Could not find an unused view name for %r after 3 tries", title)
        raise RuntimeError("Could not find an unused view name")

    # outcome.kind == "report": the engine already generated SQL and ran it (outcome.result holds
    # sql, columns, rows). This method turns that into a saved report:
    #   validate SQL again -> CREATE VIEW -> build Power BI page -> finish metadata row ->
    #   store last_report on the session -> add the chat message -> schedule the Desktop reload.
    # Returns (payload, 200), or (payload, 400) if the safety check rejects the SQL.
    def _build_report(self, session, user_text, outcome):
        result = outcome.result
        request_id = self._ensure_row(session)
        title = outcome.title or report_title_from_request(outcome.request)
        # Merge both assumption lists; dict.fromkeys removes duplicates but keeps the order.
        assumptions = list(dict.fromkeys(list(result.assumptions or []) + list(outcome.assumptions or [])))
        day = to_day_id(session.thread_started or self.clock(), self.tz)
        # Defence in depth: the SQL is checked again for this role right before it becomes a
        # permanent VIEW. GuardrailError = unsafe SQL or a table the role may not read.
        try:
            validated = validate_sql(result.sql, self.dialect, self.allowed_tables, self.max_rows,
                                     role=session.user_role, access=self.access)
            view_name, view_ms = self._create_view(session, request_id, title, day, validated.sql,
                                                   outcome.request, assumptions)
        except GuardrailError as exc:
            log.warning("Generated SQL rejected by the safety check before creating the view: %s", exc)
            self._finish_row(session, request_id, "failed", error=f"Safety check failed: {exc}")
            text = (DENIED_REPLY if "not authorized" in str(exc).lower() else
                    f"That query didn't pass the safety checks: {exc}. Could you rephrase the request?")
            session.add_message("assistant", text, kind="error")
            session.remember(user_text, text)
            session.end_thread()
            return {"status": "failed", "message": text, "request_id": request_id}, 400

        session.progress = "Report generation in progress: building the Power BI page…"
        # _build_page never raises: on failure it returns a dict with status "error".
        project = self._build_page(view_name, title, outcome.chart_type, request_id=request_id, session_id=session.session_id)
        # "_detail" holds the technical error text: saved to the DB row, removed from the reply.
        detail = project.pop("_detail", None)
        project["request_id"] = request_id
        page_ok = project.get("status") != "error"
        # The view exists either way; the request only counts as completed if the page was made.
        self._finish_row(
            session, request_id, "completed" if page_ok else "failed", view_name=view_name,
            error=None if page_ok else f"Power BI page was not created: {detail or project.get('message')}")
        response_ms = self._response_ms(session)

        columns = list(result.columns)
        rows = [[json_safe(v) for v in row] for row in result.rows]
        # session.last_report: everything about the newest report. Used later to explain it,
        # modify it, switch its chart, and for permission re-checks (_can_reuse_report).
        report = {
            "title": title, "request": outcome.request, "sql": validated.sql,
            "assumptions": assumptions, "columns": columns, "rows": rows,
            "truncated": bool(result.truncated), "view_name": view_name,
            "page_name": project.get("page_name"),
            "visual_type": project.get("visual_type") or outcome.chart_type or "auto",
            "dimension_field": project.get("dimension_field"),
            "measure_field": project.get("measure_field"), "request_id": request_id,
            "model_table": project.get("model_table"),
        }
        session.last_report = report
        session.reports.append({"title": title, "page_name": report["page_name"],
                                "request": outcome.request, "view_name": view_name,
                                "request_id": request_id})
        del session.reports[:-10]      # keep only the 10 most recent report summaries

        text = self._report_message(outcome, project, report)
        # history_note is the short version stored in the LLM's memory (not shown to the user).
        history_note = (f'{outcome.reply or "Built the report."} [Built Power BI page '
                        f'"{report["page_name"] or title}" for: {outcome.request}]')
        # Satisfied/Unsatisfied buttons are only offered when a page really exists.
        feedback_ok = bool(project.get("page_name")) and page_ok
        session.add_message("assistant", text, kind="report", meta={
            "page_name": report["page_name"], "view_name": view_name,
            "chart_type": report["visual_type"], "request_id": request_id,
            "feedback_enabled": feedback_ok, "feedback": None,
            "suggestions": self._suggestions(report)})
        session.remember(user_text, history_note)
        session.end_thread()                       # request done: the next message starts a new one
        self._schedule_reload(session, project)    # Desktop reload runs later, in a background thread

        # First 10 rows as [{column: value}] for a quick preview in the reply.
        preview = [dict(zip(columns, row)) for row in rows[:10]]
        return {
            "status": "ok", "message": text, "request_id": request_id, "view_name": view_name,
            "request_status": "completed" if page_ok else "failed",
            "original_request": outcome.request, "sql": validated.sql, "assumptions": assumptions,
            "result_row_count": len(rows), "truncated": bool(result.truncated),
            "sql_runtime_ms": result.sql_runtime_ms, "view_creation_runtime_ms": view_ms,
            "response_time_ms": response_ms, "total_runtime_ms": response_ms,
            "preview_columns": columns, "preview_rows": preview, "powerbi_project": project,
        }, 200

    # Asks PowerBIService (app/powerbi_service.py -> powerbi_project_generator.py) to write a new
    # report page for `view_name` into the Power BI project on disk. reuse=True means the model
    # table for this view already exists (chart change), so only a new page is added.
    # Output: a dict describing the page (status, page_name, visual_type, model_table ...).
    # Any exception is caught and turned into {"status": "error", ...} so callers can carry on.
    def _build_page(self, view_name, title, chart_type, reuse=False, request_id=None, session_id=None) -> dict:
        log.info("Building Power BI page for view=%s chart=%s reuse_table=%s",
                 view_name, chart_type or "auto", reuse)
        started = time.perf_counter()
        try:
            project = self.powerbi.build_page(view_name, title, chart_type, reuse_existing_table=reuse,
                                              request_id=request_id, session_id=session_id)
            log.info("Power BI page step finished: status=%s page=%r visual=%s in %d ms",
                     project.get("status"), project.get("page_name"), project.get("visual_type"),
                     (time.perf_counter() - started) * 1000)
            return project
        except Exception as exc:
            log.exception("Power BI page generation failed for view=%s", view_name)
            return {"status": "error", "view_name": view_name,
                    "message": "Power BI page generation failed. The details are in the backend log.",
                    "_detail": f"{type(exc).__name__}: {str(exc)[:1500]}"}

    # ------------------------------------------------------------------ chart-only change
    def _change_chart(self, session, user_text, outcome):
        """A different chart of the SAME view is a separate report page, so it is its own request."""
        # Called by _finish_turn for outcome.kind "chart_change". No SQL and no new view: the
        # existing view is reused, only a new Power BI page with another visual type is built.
        last = session.last_report
        label = visual_label(outcome.chart_type)
        # base_title = the original title, so repeated switches give "X - Bar chart", not "X - Line chart - Bar chart".
        base_title = last.get("base_title") or last["title"]
        title = f"{base_title} - {label}"
        started = self.clock()
        request_id = new_request_id()
        bind_request(request_id)
        log.info("Chart change of view %s to %s (new request)", last["view_name"], outcome.chart_type)
        try:
            self.store.insert_metadata(
                session_id=session.session_id, request_id=request_id, user_id=session.user_id,
                user_role=session.user_role, platform=session.platform or self.identity.default_platform,
                sql_source="chart_change", request_ts=to_utc_naive(started),
                day_id=to_day_id(started, self.tz), request_status="initiated")
            self._safe_update(request_id, request_status="in_progress", view_name=last["view_name"])
        except Exception:
            # The metadata DB failed: still build the page, just without a request row
            # (request_id None also disables the feedback buttons below).
            log.exception("Could not record the chart change")
            request_id = None
        session.progress = "Switching the chart: building the Power BI page…"
        project = self._build_page(last["view_name"], title, outcome.chart_type, reuse=True,
                                   request_id=request_id, session_id=session.session_id)
        detail = project.pop("_detail", None)
        page_ok = project.get("status") != "error"
        if request_id:
            project["request_id"] = request_id
            self._set_final(request_id, "completed" if page_ok else "failed", self._turn_ms(session),
                            view_name=last["view_name"],
                            error=None if page_ok else f"Power BI page was not created: {detail or project.get('message')}")
        # Point last_report at the new page so the next chart switch / feedback uses it.
        if project.get("page_name"):
            last.update(page_name=project["page_name"], base_title=base_title,
                        visual_type=project.get("visual_type") or outcome.chart_type)
            if request_id:
                last["request_id"] = request_id
        text = self._report_message(outcome, project, last, chart_change=True)
        feedback_ok = bool(request_id) and bool(project.get("page_name")) and page_ok
        session.add_message("assistant", text, kind="report", meta={
            "page_name": project.get("page_name"), "view_name": last["view_name"],
            "chart_type": last["visual_type"], "request_id": request_id,
            "feedback_enabled": feedback_ok, "feedback": None,
            "suggestions": self._suggestions(last)})
        session.remember(user_text, f'Switched the report to a {label.lower()} on page '
                                    f'"{project.get("page_name") or title}".')
        self._schedule_reload(session, project)
        return {"status": "ok", "message": text, "request_id": request_id,
                "view_name": last["view_name"], "powerbi_project": project}, 200

    # ------------------------------------------------------------------ message building
    # Builds the Markdown text of the assistant's "report ready" message: a headline that
    # depends on the page status, then the LLM's reply, a quick_take() summary of the data,
    # a note if a different chart type had to be used, and the assumptions made.
    def _report_message(self, outcome, project, report, chart_change=False) -> str:
        page = project.get("page_name")
        state = project.get("status")
        if state == "disabled":
            head = ("**Your data is ready**, but Power BI page generation is turned off in the "
                    "configuration, so no page was created.")
        elif state == "error" or not page:
            head = ("**I prepared the data but couldn't create the Power BI page.** "
                    f"{project.get('message') or 'The backend did not confirm the page.'}")
        else:
            head = f"**Report generated** on the page “{page}”."
            if project.get("model_table"):
                head += f" Table: `{project['model_table']}`."
            head += " Click **Refresh** to view the charts."
        parts = [head]
        if chart_change:
            parts.append(f"Same data, shown as a {visual_label(outcome.chart_type).lower()}.")
            requested, shown = outcome.chart_type, project.get("visual_type")
            if shown and requested not in ("auto", shown):
                parts.append(f"(This result has no numeric measure to plot, so I used a {visual_label(shown).lower()}.)")
        else:
            if outcome.reply:
                parts.append(outcome.reply)
            parts.append(quick_take(report))
            requested, shown = outcome.chart_type, project.get("visual_type")
            if shown and requested not in ("auto", shown):
                parts.append(f"(This result can't be drawn as a {visual_label(requested).lower()}, "
                             f"so I used a {visual_label(shown).lower()}.)")
            if report.get("assumptions"):
                parts.append("Assumptions: " + "; ".join(report["assumptions"]))
        return "\n\n".join(p for p in parts if p)    # paragraphs separated by blank lines; empty parts skipped

    # Clickable chips under a report message: "Explain this report" plus one button per other
    # chart type (only when the report has rows to draw).
    def _suggestions(self, report) -> list:
        chips = [{"label": "Explain this report", "message": "Explain this report"}]
        if report["rows"]:
            current = report.get("visual_type")
            for visual_type, label in CHART_CHIPS:
                if visual_type != current:
                    chips.append({"label": label, "action": "change_chart", "visual_type": visual_type,
                                  "message": f"Show it as a {label.lower().replace(' only', '')} chart"
                                             if visual_type != "tableEx" else "Show it as a table"})
        return chips

    # ------------------------------------------------------------------ Power BI reload (background)
    # Power BI Desktop must reload the project to show the new page, which takes a long time
    # (PowerShell UI automation, see app/powerbi_desktop_automation.ps1). So we do NOT do it
    # inside the HTTP request: the chat reply returns immediately and a background job does it.
    # session.busy = True while that job runs; the visual sees it via /transcript and /progress.
    def _schedule_reload(self, session, project):
        # Only pages that were generated and still need a reload qualify.
        if project.get("status") != "generated_pending_reload_and_refresh":
            log.debug("No Power BI reload needed (page status %s)", project.get("status"))
            return
        if not self.powerbi.automation_enabled:
            log.info("Desktop automation is off: reload Power BI Desktop manually to see the page")
            return
        session.busy = True
        # Copy the values the job needs into local variables now; the job is a closure that
        # runs later, possibly after session.request_id has already been cleared.
        page = project.get("page_name")
        session_id, request_id = session.session_id, project.get("request_id")
        log.info("Power BI reload/refresh scheduled for page %r (table %s) in %.1f s",
                 page, project.get("model_table"), self.powerbi.reload_delay)

        # The function that runs in the background thread.
        def job():
            bind_session(session_id)                  # log context does not follow the thread
            bind_request(request_id)
            time.sleep(self.powerbi.reload_delay)     # let the chat reply reach the visual first
            started = time.perf_counter()
            log.info("Power BI reload/refresh started for page %r", page)
            try:
                self.powerbi.reload_refresh(project)
                log.info("Power BI reload/refresh finished for page %r in %d ms",
                         page, (time.perf_counter() - started) * 1000)
                text = (f"Power BI has reloaded and refreshed “{page}”. "
                        "The charts should be visible now.")
                kind = "status"
            except Exception:
                # Failure must not crash the thread silently: log the traceback. No chat message
                # is added (text = None); the user can still reload Power BI Desktop by hand.
                log.exception("Power BI reload/refresh failed for page %r after %d ms "
                              "(step details: logs/powerbi_automation.log)",
                              page, (time.perf_counter() - started) * 1000)
                text = None

            # Take the session lock before touching the transcript: a /chat request may be
            # running on the same session at this moment (another thread).
            with session.lock:
                if text:
                    session.add_message("assistant", text, kind=kind)
                session.busy = False          # the visual stops showing "Power BI is reloading"

        self._background(job)                 # start it (a daemon thread, or inline in tests)


    # ================================================================== session lifecycle
    def _end_session(self, session: ChatSession, reason: str):
        """Mark the session ended (history and records are kept). Caller must hold session.lock."""
        # Called by end_session, _finish_turn (deferred End) and _followup_turn ("Finish session").
        # Returns the "Session ended" transcript entry, or None if it was already ended.
        if session.ended:
            return None
        log.info("Session ended (reason: %s)", reason)
        if session.request_id:       # a report request was still open: close its row cleanly
            self._finish_row(session, session.request_id, "failed",
                             error="Session ended before the report was completed",
                             response_ms=session.runtime_ms)
        session.end_thread()         # also clears a pending chart choice
        # session.ended: from now on every message is refused with 409. The session is NOT
        # deleted, so its transcript can still be read (and audited).
        session.ended, session.ended_at = True, time.time()
        session.end_reason, session.end_requested = reason, False
        entry = session.add_message("assistant", SESSION_ENDED_TEXT, kind="status",
                                    meta={"session_ended": True, "reason": reason})
        self.recorder.record(session, [entry], request_id=None, intent="session_end")
        return entry

    def end_session(self, session_id, reason=None, identity: Identity | None = None):
        """End a session. If a request is still running, the session ends right after it finishes
        (so the report and its database records are never cut in half)."""
        # Called by POST /session/<id>/end and reset(). Status codes: 400 bad id, 404 unknown
        # session, 403 someone else's session, 200 ended, 202 Accepted = "will end shortly".
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 64:
            return {"error": "session_id must be a non-empty string of 64 characters or fewer"}, 400
        bind_session(session_id.strip())
        session = self.sessions.get(session_id.strip())
        if session is None:
            log.info("End requested for an unknown session")
            return {"error": "session not found"}, 404
        if session.user_id and (identity or Identity()).user_id != session.user_id:
            log.warning("End refused: the session belongs to another user")
            return {"error": "This session belongs to another user."}, 403
        if session.ended:
            return {"ended": True, "already_ended": True, "session_id": session.session_id}, 200
        reason = reason if reason in END_REASONS else "user"
        session.end_requested, session.end_reason = True, reason
        # Setting the flag BEFORE trying the lock means a running turn will see it and end the
        # session itself when it finishes, so the request is never lost in between.
        # acquire(timeout=...) waits at most end_wait_s seconds instead of blocking forever like
        # `with session.lock`. True = we got the lock (end now); False = a turn is still running.
        if session.lock.acquire(timeout=self.end_wait_s):
            try:
                if not session.ended:
                    self._end_session(session, reason)
            finally:
                session.lock.release()
        else:
            log.info("End requested while a request is running; it ends when that finishes")
        if session.ended:
            # Send back the "Session ended" message so the visual can show it right away.
            ended_entries = [m for m in session.transcript if (m.get("meta") or {}).get("session_ended")]
            return json_safe({"ended": True, "session_id": session.session_id,
                              "messages": ended_entries[-1:]}), 200
        return {"ended": False, "pending": True, "session_id": session.session_id,
                "message": "Your current request is still running. "
                           "The session will end as soon as it finishes."}, 202

    def _followup_turn(self, session, message, choice, first_new_id):
        """The two buttons shown after 'Unsatisfied': Return to conversation / Finish session."""
        # Called by _handle_locked (lock held). No LLM call: the reply text is fixed.
        last = session.last_report or {}
        if choice == "finish":
            entry = self._end_session(session, "finished_after_unsatisfied")
            text = entry["text"] if entry else SESSION_ENDED_TEXT
            payload = {"status": "session_ended", "message": text}
        else:
            text = ("Sure, let's improve it. What would you like to change: the time period, a filter, "
                    "the grouping, the metric, or the chart type?")
            session.add_message("assistant", text, kind="text",
                                meta={"suggestions": self._suggestions(last)} if last else None)
            page = last.get("page_name") or last.get("title") or "the last report"
            session.remember(message, f'{text} [The user was unsatisfied with the report "{page}" '
                                      "and wants to improve it.]")
            payload = {"status": "chat", "message": text}
        outcome = Outcome(kind="reply", intent="feedback_followup", reply=text)
        return self._payload(session, first_new_id, outcome, payload), 200

    # ================================================================== satisfaction feedback
    # Finds the transcript message of the report with this request_id (newest first), but only
    # if feedback buttons were enabled on it. None if there is no such report.
    @staticmethod
    def _find_report_entry(session, request_id):
        for entry in reversed(session.transcript):
            meta = entry.get("meta") or {}
            if entry.get("kind") == "report" and meta.get("request_id") == request_id \
                    and meta.get("feedback_enabled"):
                return entry
        return None

    def handle_feedback(self, body: dict, identity: Identity | None = None):
        """Save Satisfied (TRUE) / Unsatisfied (FALSE) on the report request's EXISTING metadata row.

        Last click wins: changing your mind updates the same row (no new record is ever created)
        until the session ends. Clicking the value that is already saved changes nothing."""
        # Called by POST /feedback.
        session_id, request_id = body.get("session_id"), body.get("request_id")
        satisfaction = body.get("satisfaction")
        # Backwards compatibility: older visuals sent {"satisfied": true/false} instead.
        if satisfaction is None and "satisfied" in body:
            satisfied_val = body.get("satisfied")
            if satisfied_val is True:
                satisfaction = "satisfied"
            elif satisfied_val is False:
                satisfaction = "unsatisfied"
        for value, name in ((session_id, "session_id"), (request_id, "request_id")):
            if not isinstance(value, str) or not value.strip() or len(value) > 64:
                return {"error": f"{name} must be a non-empty string of 64 characters or fewer"}, 400
        if satisfaction not in ("satisfied", "unsatisfied"):
            return {"error": "satisfaction must be 'satisfied' or 'unsatisfied'"}, 400
        bind_session(session_id.strip())
        bind_request(request_id.strip())
        log.info("Feedback received: %s", satisfaction)
        session = self.sessions.get(session_id.strip())
        if session is None:
            return {"error": "session not found"}, 404
        if session.user_id and (identity or Identity()).user_id != session.user_id:
            return {"error": "This session belongs to another user."}, 403
        if session.ended:
            return self._ended_response(session)
        # Same pattern as handle_chat: check `ended` before AND after taking the lock, because
        # the session may have ended while we were waiting for it.
        with session.lock:
            if session.ended:
                return self._ended_response(session)
            entry = self._find_report_entry(session, request_id.strip())
            if entry is None:
                return {"error": "No report with that request_id exists in this session."}, 404
            current = entry["meta"].get("feedback")
            label = "Satisfied" if satisfaction == "satisfied" else "Unsatisfied"
            if current == satisfaction:
                return {"status": "unchanged", "changed": False, "request_id": request_id,
                        "session_id": session.session_id, "satisfaction": satisfaction,
                        "customer_satisfaction": satisfaction == "satisfied",
                        "message": f"Your feedback is already recorded as {label}."}, 200
            value = satisfaction == "satisfied"      # True/False, stored in the metadata row
            # UPDATE the existing metadata row. A DB failure -> HTTP 500 and nothing changes in
            # the transcript, so the user can simply click again.
            try:
                saved = self.store.set_satisfaction(request_id.strip(), session.session_id, value)
            except Exception:
                log.exception("Could not save feedback for %s", request_id)
                return {"status": "error",
                        "message": "I couldn't save your feedback. Please try again."}, 500
            if not saved["found"]:
                log.warning("Feedback for a request that has no metadata row")
                return {"error": "That report request was not found."}, 404
            log.info("Feedback saved as %s (previous: %s)", satisfaction, current or "none")

            first_new_id = session.last_id
            # Remember the choice on the report message itself (the visual highlights the button)
            # and add the click + the assistant's answer to the transcript.
            entry["meta"]["feedback"] = satisfaction
            user_entry = session.add_message("user", label)
            lead = (f"Thanks! Your feedback is recorded as **{label}**." if current is None
                    else f"I've updated your feedback to **{label}**.")
            meta = None
            if value:
                text = lead
            else:
                # Unsatisfied: offer the two follow-up buttons handled by _followup_turn.
                text = lead + ("\n\nWould you like to return to the conversation with the assistant "
                               "to improve this report?")
                meta = {"suggestions": [
                    {"label": "Return to conversation", "action": "feedback_followup",
                     "choice": "return", "message": "Return to conversation"},
                    {"label": "Finish session", "action": "feedback_followup",
                     "choice": "finish", "message": "Finish session"}]}
            assistant_entry = session.add_message("assistant", text, kind="text", meta=meta)
            page = entry["meta"].get("page_name") or "the report"
            session.remember(f'I clicked "{label}" for the report "{page}".', text)
            self.recorder.record(session, [user_entry, assistant_entry], request_id=request_id.strip(),
                                 intent="feedback")
            return json_safe({
                "status": "ok", "changed": True, "request_id": request_id, "session_id": session.session_id,
                "satisfaction": satisfaction, "customer_satisfaction": value, "message": text,
                "messages": [user_entry, assistant_entry],
                "updated": [{"id": entry["id"], "meta": entry["meta"]}], "session_ended": False}), 200

    # ================================================================== saved reports
    # "Save report" on a generated page and the chat's "Saved reports" list. The work is done by
    # app/saved_reports.py; these methods check the input and the caller, turn failures into
    # replies, and (after a restore) reload Power BI Desktop like a new report does.
    @staticmethod
    def _bad_request_id(request_id):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 64:
            return {"error": "request_id must be a non-empty string of 64 characters or fewer"}, 400
        return None

    # In demo mode the chosen role IS the owner, so a valid one is required. Returns
    # (identity, None) or (identity, (payload, 403)).
    def _saved_identity(self, identity):
        identity = identity or Identity()
        if self.identity.demo_role_selection and identity.user_role not in ("CEO", "Developer", "Analyst"):
            return identity, ({"error": "Choose a valid demo role first."}, 403)
        return identity, None

    def save_report(self, body: dict, identity: Identity | None = None):
        """POST /reports/save: the "Save report" button on a generated report page."""
        request_id = body.get("request_id")
        identity, denied = self._saved_identity(identity)
        denied = self._bad_request_id(request_id) or denied
        if denied:
            return denied
        request_id = request_id.strip()
        bind_request(request_id)
        log.info("Save report requested")
        try:
            return self.saved.save(request_id, identity)
        except PowerBIProjectError as exc:
            log.warning("Saving report %s failed: %s", request_id, exc)
            return {"error": f"The report page could not be read: {exc}"}, 500
        except Exception:
            log.exception("Saving report %s failed", request_id)
            return {"error": "The report could not be saved. Please try again."}, 500

    def saved_report_status(self, request_id, identity: Identity | None = None):
        """GET /reports/saved/<request_id>: tells the Save button whether it is already saved."""
        identity, denied = self._saved_identity(identity)
        denied = self._bad_request_id(request_id) or denied
        if denied:
            return denied
        try:
            return self.saved.is_saved(request_id.strip(), identity)
        except Exception:
            log.exception("Could not read saved report %s", request_id)
            return {"error": "Saved reports are unavailable right now."}, 500

    def list_saved_reports(self, identity: Identity | None = None):
        """GET /reports/saved: the caller's saved reports, newest first."""
        identity, denied = self._saved_identity(identity)
        if denied:
            return denied
        try:
            return self.saved.list(identity)
        except Exception:
            log.exception("Could not list saved reports")
            return {"error": "Saved reports are unavailable right now."}, 500

    def delete_saved_report(self, request_id, identity: Identity | None = None):
        """POST /reports/saved/<request_id>/delete: forget the saved copy (page and view stay)."""
        identity, denied = self._saved_identity(identity)
        denied = self._bad_request_id(request_id) or denied
        if denied:
            return denied
        bind_request(request_id.strip())
        try:
            return self.saved.delete(request_id.strip(), identity)
        except Exception:
            log.exception("Could not delete saved report %s", request_id)
            return {"error": "The saved report could not be deleted. Please try again."}, 500

    def open_saved_report(self, request_id, body: dict, identity: Identity | None = None):
        """POST /reports/saved/<request_id>/open: put the saved page back if it was removed.

        body.session_id (optional) = the chat page's session: the restore message and the Desktop
        reload are then shown in that chat, exactly like a newly generated report."""
        identity, denied = self._saved_identity(identity)
        denied = self._bad_request_id(request_id) or denied
        if denied:
            return denied
        request_id = request_id.strip()
        bind_request(request_id)
        log.info("Open saved report requested")
        try:
            payload, status, project = self.saved.open(request_id, identity)
        except PowerBIProjectError as exc:
            log.warning("Restoring saved report %s failed: %s", request_id, exc)
            return {"error": f"The saved report could not be restored: {exc}"}, 500
        except Exception:
            log.exception("Restoring saved report %s failed", request_id)
            return {"error": "The saved report could not be restored. Please try again."}, 500
        if project is None:
            return payload, status

        project["request_id"] = request_id       # log context of the reload job
        if self.powerbi.automation_enabled:
            payload["message"] += (" Power BI is reloading the report; when it is done, click "
                                   "**Open latest report** (top right) or the page tab to view it.")
        else:
            payload["message"] += " Reload the project in Power BI Desktop to see it."
        session = self._session_for_restore(body.get("session_id"), identity)
        if session is not None:
            with session.lock:
                first_new_id = session.last_id
                session.add_message("assistant", payload["message"], kind="status")
                self._schedule_reload(session, project)
                payload.update(session_id=session.session_id, busy=session.busy,
                               messages=session.messages_after(first_new_id))
        else:
            self._reload_without_session(project)
            payload["busy"] = False
        return json_safe(payload), status

    # The caller's own, still-active chat session with this id, or None.
    def _session_for_restore(self, session_id, identity: Identity):
        if not isinstance(session_id, str) or not session_id.strip():
            return None
        session = self.sessions.get(session_id.strip())
        if session is None or session.ended:
            return None
        if (session.user_id or None) != (identity.user_id or None) or session.user_role != identity.user_role:
            return None
        return session

    # Desktop reload after a restore when no chat session can show its progress.
    def _reload_without_session(self, project):
        if not self.powerbi.automation_enabled:
            return

        def job():
            bind_request(project.get("request_id"))
            time.sleep(self.powerbi.reload_delay)
            try:
                self.powerbi.reload_refresh(project)
                log.info("Power BI reload/refresh finished for restored page %r", project.get("page_name"))
            except Exception:
                log.exception("Power BI reload/refresh failed for restored page %r", project.get("page_name"))

        self._background(job)

    # ================================================================== other endpoints
    # GET /session/<id>/transcript. The visual uses it to restore the chat after Power BI
    # reloads, and to poll for new messages (e.g. the background "reloaded" message).
    # `after` = id of the last message the visual already has; only newer ones are returned.
    # busy / end_requested let the visual show "reloading..." or "ending..." states.
    def transcript(self, session_id, after=0):
        session = self.sessions.get(session_id)
        if session is None:
            return {"error": "session not found"}, 404
        with session.lock:
            return json_safe({"session_id": session_id, "busy": session.busy,
                              "ended": session.ended, "end_requested": session.end_requested,
                              "last_id": session.last_id,
                              "messages": session.messages_after(after)}), 200

    def progress(self, session_id):
        """Lock-free on purpose: the visual polls this WHILE /chat is still running."""
        # (If it took session.lock it would wait until /chat finished, which defeats the purpose.)
        session = self.sessions.get(session_id)
        if session is None:
            return {"error": "session not found"}, 404
        return {"progress": session.progress, "busy": session.busy, "ended": session.ended}, 200

    # ------------------------------------------------------------------ audit
    # Read-only views of the recorded conversations (ConversationRecorder, app/audit.py).
    # Unlike /transcript these survive a server restart because they are stored persistently.
    def audit_sessions(self, limit=50):
        return {"sessions": self.recorder.list_sessions(limit)}, 200

    def audit_session(self, session_id):
        rows = self.recorder.read_session(session_id)
        return ({"session_id": session_id, "messages": rows}, 200) if rows \
            else ({"error": "no recorded conversation for this session"}, 404)

    # One request: its metadata row (from the DB) + the recorded Q&A. A DB failure is logged
    # and treated as "no metadata", so the conversation part can still be returned.
    def audit_request(self, request_id):
        try:
            meta = self.store.fetch_status_row(request_id)
        except Exception:
            log.exception("Could not read request metadata")
            meta = None
        rows = self.recorder.read_request(request_id)
        if meta is None and not rows:
            return {"error": "request not found"}, 404
        return json_safe({"request_id": request_id, "metadata": meta, "conversation": rows}), 200

    def latest(self, identity: Identity | None = None):
        """Most recent ACTIVE session (an ended session is never resumed as the current one)."""
        # GET /session/latest: fallback for a visual that lost its stored session id. In demo
        # mode only sessions of the selected role are considered.
        if not self.resume_latest:
            return {"error": "disabled"}, 404
        role = None
        if self.identity.demo_role_selection:
            if not identity or not identity.demo_role or identity.user_role not in ("CEO", "Developer", "Analyst"):
                return {"error": "Choose a valid demo role before resuming a chat."}, 403
            role = identity.user_role
        session = self.sessions.latest(self.resume_window_s, user_role=role)
        if session is None:
            return {"error": "no recent session"}, 404
        # last_active: epoch seconds of the last message (the value latest() itself ranks by), so
        # the visual can pick the newest session across demo roles.
        return {"session_id": session.session_id, "last_active": session.updated}, 200

    def reset(self, session_id, identity: Identity | None = None):
        """Old "New chat" endpoint. It no longer deletes anything: it ends the session."""
        payload, status = self.end_session(session_id, "reset", identity)
        # Unknown session: report success anyway (there is nothing to reset) so old clients work.
        if status == 404:
            return {"ok": True, "deleted": False, "ended": False}, 200
        payload.update(ok=True, deleted=False)
        return payload, status

    # GET /session/<id> (debugging): the LLM's memory of the conversation (session.history),
    # i.e. what the model is shown, not the UI transcript.
    def history(self, session_id):
        session = self.sessions.get(session_id)
        if not session:
            return {"error": "not found"}, 404
        return {"session_id": session_id, "messages": session.history}, 200

    def revise_report(self, body, identity: Identity | None = None):
        """Chart-only change without going through the language model."""
        session_id, visual_type = body.get("session_id"), body.get("visual_type", "auto")
        if not isinstance(session_id, str) or not session_id.strip():
            return {"status": "failed", "message": "session_id is required"}, 400
        if visual_type not in ALLOWED_VISUAL_TYPES or visual_type == "auto":
            return {"status": "failed", "message": "Unsupported visual_type"}, 400
        session = self.sessions.get(session_id.strip())
        if not session or not session.last_report:
            return {"status": "failed",
                    "message": "The previous report is not available in this session."}, 409
        # Re-uses the normal /chat path with action=change_chart and a generated message.
        return self.handle_chat({"session_id": session.session_id, "action": "change_chart",
                                 "visual_type": visual_type,
                                 "message": f"Show it as a {visual_label(visual_type).lower()}"}, identity)

    # ------------------------------------------------------------------ manual-SQL test hooks
    # /chat with a "mock_sql" field (only when chat.allow_mock_sql is on): skips the LLM and
    # builds a report from the given SQL via complete_manual_report. Useful for testing the
    # view + Power BI part without paying for LLM calls.
    def _mock_sql_turn(self, session, message, sql, first_new_id,identity):
        session.start_thread(message)      # opens a new request thread (resets request_id, timers)
        request_id = self._ensure_row(session)
        payload, status = self.complete_manual_report(
            request_id,
            sql,
            resolved_request=message,
            role=identity.user_role,
            access=self.access,
            identity=identity,
        )
        text = payload.get("message") or payload.get("error") or "Done."
        session.add_message("assistant", text, kind="report" if status == 200 else "error")
        session.remember(message, text)
        session.end_thread()
        entries = session.messages_after(first_new_id)
        self.recorder.record(session, entries, request_id=request_id, intent="manual_sql")
        payload.update(session_id=session.session_id, reply=text, busy=False,
                       session_ended=session.ended, messages=json_safe(entries))
        return json_safe(payload), status

    def complete_manual_report(self, request_id, generated_sql, resolved_request=None, assumptions=None,role=None,
    access=None, identity=None):
        """Manual/mock SQL fallback; no LLM call. Runs the Desktop reload synchronously."""
        # Called by _mock_sql_turn and POST /complete-report. Same steps as a normal report
        # (validate -> run -> view -> page -> metadata), but synchronous and without a session.
        started = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - started) * 1000)   # ms since start, when called
        bind_request(request_id)
        log.info("Manual SQL report requested (role %s)", role or "-")
        # A known role is required, otherwise 403.
        if not role or access is None or not access.normalize_role(role):
            log.warning("Manual SQL report refused: no valid role")
            return {
                "status": "failed",
                "message": "Your role is not authorized to access this information. Please contact your administrator if you believe you need access.",
            }, 403
        # The request must already have a metadata row (created by _ensure_row).
        record = self.store.fetch_metadata(request_id)
        if record is None:
            return {"status": "failed", "error": "Unknown request_id"}, 404
        # Ownership check: in demo mode rows have no user_id; otherwise the user ids must match.
        # In both cases the caller's role must equal the role stored on the row.
        if identity is not None:
            if identity.demo_role:
                wrong_owner = record.get("user_id") is not None
            else:
                wrong_owner = not identity.user_id or identity.user_id != record.get("user_id")
            if wrong_owner or identity.user_role != record.get("user_role"):
                log.warning("Manual SQL report refused: the request belongs to another user")
                return {"status": "failed", "message": "This request belongs to another user."}, 403
        # A completed request cannot be completed twice (409 Conflict).
        if record["request_status"] not in ("initiated", "in_progress", "failed"):
            return {"status": "failed",
                    "error": f"Request is already in status '{record['request_status']}'"}, 409
        assumptions = assumptions or []
        if not isinstance(assumptions, list) or not all(isinstance(x, str) for x in assumptions):
            return {"status": "failed", "error": "assumptions must be a list of strings"}, 400
        if not isinstance(generated_sql, str) or not generated_sql.strip():
            return {"status": "failed", "error": "generated_sql is required"}, 400
        resolved_request = (resolved_request or "Manual SQL report").strip()
        try:
            self.store.update_metadata(request_id, request_status="in_progress", sql_source="manual_mock")
            validated = validate_sql(
                generated_sql,
                self.dialect,
                self.allowed_tables,
                self.max_rows,
                role=role,
                access=access,
            )
            # Run the validated SQL read-only (app/db.py) to get rows for the preview.
            q_started = time.perf_counter()
            query_result = self.pipeline.db.execute_readonly(validated.sql, self.max_rows)
            sql_ms = round((time.perf_counter() - q_started) * 1000)
            day = record.get("day_id") or to_day_id(self.clock(), self.tz)
            view_name, view_ms = self._create_view(None, request_id, resolved_request, day, validated.sql,
                                                   resolved_request, assumptions,
                                                   session_id=record["session_id"])
            project = self._build_page(view_name, resolved_request, None, request_id=request_id,
                                       session_id=record["session_id"])
            detail = project.pop("_detail", None)
            # Unlike _schedule_reload, the Desktop reload runs right here (synchronously), so
            # this HTTP call only returns after Power BI has reloaded.
            if project.get("status") == "generated_pending_reload_and_refresh" and self.powerbi.automation_enabled:
                try:
                    project["desktop_automation"] = self.powerbi.reload_refresh(project)
                    project["status"] = "generated_reloaded_refreshed"
                except Exception:
                    log.exception("Manual report: Power BI reload failed")
                    project["desktop_automation"] = {"success": False,
                                                     "message": "Automatic reload failed; see the backend log."}
            page_ok = project.get("status") != "error"
            total_ms = elapsed()
            self._set_final(request_id, "completed" if page_ok else "failed", total_ms, view_name=view_name,
                            error=None if page_ok else f"Power BI page was not created: {detail}")
            page = project.get("page_name")
            message = (f"Report generated on the page “{page}”. Click Refresh to view the charts."
                       if page else "Report view created (manual SQL); no Power BI page was created.")
            columns = list(query_result.columns)
            return {"request_id": request_id, "session_id": record["session_id"], "status": "ok",
                    "message": message, "view_name": view_name, "sql": validated.sql,
                    "assumptions": assumptions, "result_row_count": len(query_result.rows),
                    "truncated": query_result.truncated, "sql_runtime_ms": sql_ms,
                    "view_creation_runtime_ms": view_ms, "response_time_ms": total_ms,
                    "total_runtime_ms": total_ms, "preview_columns": columns,
                    "preview_rows": [dict(zip(columns, map(json_safe, r))) for r in query_result.rows[:10]],
                    "powerbi_project": project}, 200
        # Unsafe / unauthorized SQL -> 400; any other failure -> 500 with the traceback in
        # logs/backend_billing.log. Both mark the metadata row failed.
        except GuardrailError as exc:
            log.warning("Manual SQL rejected by the safety check: %s", exc)
            self._set_final(request_id, "failed", elapsed(), error=f"Safety check failed: {exc}")
            message = DENIED_REPLY if "not authorized" in str(exc).lower() else str(exc)
            return {"request_id": request_id, "status": "failed", "message": message}, 400
        except Exception as exc:
            log.exception("Manual report processing failed for request_id=%s", request_id)
            self._set_final(request_id, "failed", elapsed(),
                            error=f"Unexpected error: {type(exc).__name__}: {str(exc)[:500]}")
            return {"request_id": request_id, "status": "error",
                    "message": "Manual report processing failed. The details are in the backend log."}, 500


def report_title_from_request(request_text, fallback="Billing Report") -> str:
    """Readable page title from a request, used when the model gave no title."""
    # Called by _build_report. Example: "please show me revenue by month sorted by date"
    # -> "Revenue by Month". Each re.sub strips one kind of filler with a regular expression.
    import re
    title = re.sub(r"\s+", " ", str(request_text or "")).strip()     # collapse whitespace
    title = re.sub(r"^(?:please\s+)?(?:(?:can|could|would) you\s+)?", "", title, flags=re.I)
    title = re.sub(r"^(?:show me|show|display|create|generate|make|build|give me|provide|list|find)\s+",
                   "", title, flags=re.I)
    title = re.sub(r"\s*,?\s*(?:sorted|sort|ordered|order)\s+(?:by|from|in)\b.*$", "", title, flags=re.I)
    title = title.strip(" .?!,:;-_")
    if not title:
        return fallback
    # Title Case, but small words stay lower case unless they are the first word.
    small = {"a", "an", "the", "and", "or", "but", "for", "nor", "on", "at", "to", "from", "by",
             "of", "in", "with", "per", "as"}
    words = title.title().split()
    words = [w.lower() if i and w.lower() in small else w for i, w in enumerate(words)]
    title = " ".join(words)
    for acronym in ("Usd", "Id", "Api", "Sql", "Ai"):
        title = re.sub(rf"\b{acronym}\b", acronym.upper(), title)
    # Keep titles short (they become Power BI page names): cut at a word boundary.
    if len(title) > 54:
        title = title[:54].rsplit(" ", 1)[0].strip() or title[:54]
    return title or fallback


# ====================================================================== Flask wiring
def create_app(cfg_path=None, *, cfg=None, llm=None, db=None, store=None, powerbi=None,
               background=None) -> Flask:
    # This function creates and configures the Flask application.
    # Dependencies can be passed in manually, which is especially useful for offline testing.

    """Build the app. Every dependency can be injected, which is how the tests run offline."""
    # Called by main() (real server) and by the tests (with fake llm/db/store/powerbi).
    # Imports are done here, inside the function, so importing this module stays cheap.

    # Import the functions and classes needed to build the chatbot components.
    from app.db import build_db
    from app.llm import build_llm
    from app.pipeline import Pipeline
    from app.powerbi_service import PowerBIService
    from app.report_store import ReportStore

    # If no configuration was passed, load it from the YAML configuration file.
    if cfg is None:
        path = Path(cfg_path or os.getenv("CHATBOT_CONFIG") or DEFAULT_CONFIG)
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))

    # Wire the objects together (anything not injected is built from the config).
    # Create the report store if one wasn't provided.
    store = store or ReportStore()

    # Build the chatbot pipeline by connecting the AI model, database, and configuration.
    pipeline = Pipeline(llm or build_llm(cfg["llm"]), db or build_db(cfg["db"]), cfg, base_dir=BASE)

    # Create the Power BI service, which handles communication with Power BI.
    powerbi = powerbi or PowerBIService(cfg, BASE, store)

    # Get the location of the conversation log file from the configuration.
    log_file = (cfg.get("pipeline", {}) or {}).get("conversation_log_file")

    # Create a recorder to save conversation details.
    recorder = ConversationRecorder(store, BASE / log_file if log_file else None)

    # Create the main ChatService and give it all the components it needs.
    # ChatService handles the actual chatbot requests.
    service = ChatService(cfg, pipeline, store, powerbi, background=background, recorder=recorder)

    # This helper identifies the person making the request.
    def who(body=None):
        """Trusted identity of the caller (None-values when unknown). Never from the request body."""
        return service.identity.resolve(request.headers, body)

    # Actually create the Flask application.
    app = Flask(__name__)

    # Store the ChatService inside the Flask app so main() and tests can access it.
    app.config["SERVICE"] = service      # lets main() and tests reach the ChatService

    # Flask hook: runs before EVERY request handler.
    @app.before_request
    def log_context():
        # Clear old logging information because server threads can be reused.
        # Then attach the current session ID to the logs.
        # Server threads are reused: start every request with a clean sid/rid for the log lines.
        clear_context()
        bind_session((request.view_args or {}).get("session_id"))

    # Flask hook: runs after every handler and adds CORS headers. Without them the browser
    # inside Power BI would block the visual from reading our responses (different origin).
    @app.after_request
    def cors(response):
        # CORS allows the Power BI visual, running in a different origin, to communicate with Flask.
        # The visual runs in a sandboxed iframe, so any origin must be allowed (local POC only).
        response.headers["Access-Control-Allow-Origin"] = "*"

        # Allow the browser to send these headers with requests.
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

        # Tell the browser which HTTP methods are allowed.
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"

        # Allow private network access if the browser specifically requests it.
        if request.headers.get("Access-Control-Request-Private-Network"):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    # Converts a ChatService (payload, status) tuple into a Flask JSON response.
    def reply(result):
        # Separate the response data from its HTTP status code.
        payload, status = result

        # Convert the response data into JSON and send it back to the caller.
        return jsonify(payload), status

    # ---- routes: each one just reads the input and calls a ChatService method ----
    # Routes are the URLs that the Power BI visual or another client can call.

    # CHAT ROUTE: Receives a message from the user.
    @app.post("/chat")
    def chat():
        # silent=True prevents invalid JSON from causing an immediate error.
        # If no valid JSON is received, use an empty dictionary instead.
        # The ChatService will then return a clear error if the message is missing.
        # silent=True: invalid/missing JSON gives None instead of an error; `or {}` -> empty dict,
        # so handle_chat returns a clean 400 "message is required".
        body = request.get_json(silent=True) or {}

        # Pass the user's message and identity to ChatService for processing.
        return reply(service.handle_chat(body, who(body)))

    # FEEDBACK ROUTE: Receives feedback from the user about a report.
    @app.post("/feedback")
    def feedback():
        # Read the feedback information sent as JSON.
        body = request.get_json(silent=True) or {}

        # Pass the feedback to ChatService and return its response.
        return reply(service.handle_feedback(body, who(body)))

    # END SESSION ROUTE: Ends a particular conversation.
    @app.post("/session/<session_id>/end")
    def session_end(session_id):
        # Read the reason for ending the session, if provided.
        body = request.get_json(silent=True) or {}

        # Ask ChatService to end the specified session.
        return reply(service.end_session(session_id, body.get("reason"), who(body)))

    # LATEST SESSION ROUTE: Finds the most recent session for the user.
    @app.get("/session/latest")
    def session_latest():
        # Pass the user's demo role, if provided, to the identity resolver.
        return reply(service.latest(who({"demo_role": request.args.get("demo_role")})))

    # TRANSCRIPT ROUTE: Retrieves messages from a particular conversation.
    @app.get("/session/<session_id>/transcript")
    def session_transcript(session_id):
        # ?after=<id> from the query string; a non-number simply means "everything".
        # Read the ID of the last message already received by the visual.
        try:
            after = int(request.args.get("after", 0))
        except ValueError:
            # If the value is not a number, start from the beginning.
            after = 0

        # Return messages newer than the supplied message ID.
        return reply(service.transcript(session_id, after))

    # PROGRESS ROUTE: Checks how far an ongoing request has progressed.
    @app.get("/session/<session_id>/progress")
    def session_progress(session_id):
        return reply(service.progress(session_id))

    # AUDIT SESSIONS ROUTE: Returns a list of sessions for reviewing activity.
    @app.get("/audit/sessions")
    def audit_sessions():
        # Clamp ?limit= to 1..500 so nobody can ask for an enormous list.
        # Restrict the number of sessions returned to a maximum of 500.
        try:
            limit = max(1, min(500, int(request.args.get("limit", 50))))
        except ValueError:
            # Use 50 sessions if the supplied limit is invalid.
            limit = 50

        return reply(service.audit_sessions(limit))

    # AUDIT SESSION ROUTE: Retrieves the audit information for one session.
    @app.get("/audit/sessions/<session_id>")
    def audit_session(session_id):
        # Get the session's saved audit information.
        payload, status = service.audit_session(session_id)

        # ?format=text returns a plain-text, human-readable transcript instead of JSON.
        # If text format is requested, convert the messages into a readable transcript.
        if request.args.get("format") == "text":
            text = format_conversation_text(session_id, payload.get("messages", []))
            return Response(text, status=status, mimetype="text/plain; charset=utf-8")

        # Otherwise, return the audit information as JSON.
        return jsonify(payload), status

    # AUDIT REQUEST ROUTE: Retrieves information about one report request.
    @app.get("/audit/requests/<request_id>")
    def audit_request(request_id):
        return reply(service.audit_request(request_id))

    # RESET SESSION ROUTE: Handles a request to reset a conversation.
    @app.post("/session/<session_id>/reset")
    def session_reset(session_id):
        return reply(service.reset(session_id, who()))

    # HISTORY ROUTE: Retrieves the conversation history for a session.
    @app.get("/session/<session_id>")
    def session_history(session_id):
        return reply(service.history(session_id))

    # REVISE REPORT ROUTE: Handles requests to revise an existing report.
    @app.post("/revise-report")
    def revise_report():
        # Read the requested report changes.
        body = request.get_json(silent=True) or {}

        # Pass the revision request to ChatService.
        return reply(service.revise_report(body, who(body)))

    # COMPLETE REPORT ROUTE: Completes a report using manually supplied SQL.
    @app.post("/complete-report")
    def complete_report():
        # This route is only available when mock SQL mode is enabled.
        if not service.allow_mock_sql:       # same switch as mock_sql in /chat: a test-only hook
            return jsonify(error="not found"), 404

        # Read the report details sent by the caller.
        body = request.get_json(silent=True) or {}

        # Extract the request ID and check that it is valid.
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return jsonify(error="request_id is required"), 400

        # Identify the caller so their permissions can be checked.
        identity = who(body)

        # Pass the supplied SQL and report details to ChatService.
        return reply(service.complete_manual_report(
            request_id.strip(),
            body.get("generated_sql"),
            resolved_request=body.get("resolved_request"),
            assumptions=body.get("assumptions") or [],
            role=identity.user_role,
            access=service.access,
            identity=identity,
        ))

    # The metadata row of one request (status, timings, view name, feedback). DB errors -> 500.
    # REQUEST STATUS ROUTE: Retrieves the saved status and metadata for a report request.
    @app.get("/requests/<request_id>")
    def request_status(request_id):
        try:
            # Look up the request's metadata in the report store.
            row = service.store.fetch_status_row(request_id)
        except Exception:
            # If the database lookup fails, record the error and return an HTTP 500 response.
            log.exception("Could not read request metadata for %s", request_id)
            return jsonify(error="Could not read the request record."), 500

        # If no matching request exists, return an HTTP 404 response.
        if row is None:
            return jsonify(error="request not found"), 404

        # Convert the database row into JSON-safe data before returning it.
        return jsonify(json_safe(row))

    # SAVED REPORT ROUTES: "Save report" on a generated page, and the chat's "Saved reports" list.
    # GET requests carry the demo role as ?demo_role= (like /session/latest).
    @app.post("/reports/save")
    def report_save():
        body = request.get_json(silent=True) or {}
        return reply(service.save_report(body, who(body)))

    @app.get("/reports/saved")
    def reports_saved():
        return reply(service.list_saved_reports(who({"demo_role": request.args.get("demo_role")})))

    @app.get("/reports/saved/<request_id>")
    def report_saved_status(request_id):
        return reply(service.saved_report_status(request_id, who({"demo_role": request.args.get("demo_role")})))

    @app.post("/reports/saved/<request_id>/open")
    def report_saved_open(request_id):
        body = request.get_json(silent=True) or {}
        return reply(service.open_saved_report(request_id, body, who(body)))

    @app.post("/reports/saved/<request_id>/delete")
    def report_saved_delete(request_id):
        body = request.get_json(silent=True) or {}
        return reply(service.delete_saved_report(request_id, who(body)))

    # HEALTH ROUTE: Checks whether the backend is running and reports its configuration.
    # Liveness check used by the visual: is the backend up, and which LLM/model/dialect is in use.
    @app.get("/health")
    def health():
        # Get the language model currently used by the pipeline.
        llm_ = service.pipeline.llm

        # Return basic backend and AI model information.
        return jsonify(ok=True, mode="conversational", provider=llm_.name, model=llm_.model,
                       dialect=service.dialect,
                       demo_role_selection=service.identity.demo_role_selection)

    # Return the fully configured Flask application to the caller (main()).
    return app


# Program start (`python chatbot_api.py`): load config, set up logging, build the app, serve.
def main():
    path = Path(os.getenv("CHATBOT_CONFIG") or DEFAULT_CONFIG)
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    # Logging is set up here, once, and not in create_app(): the tests build many apps.
    log_file = configure_logging(cfg, BASE)
    host, port = os.getenv("CHATBOT_HOST", "127.0.0.1"), int(os.getenv("CHATBOT_PORT", "5056"))
    log.info("Starting chatbot backend: config=%s log_file=%s", path, log_file or "(console only)")
    # If startup fails (bad config, DB unreachable, ...) write the traceback to the log file
    # first, then re-raise so the process stops with the error visible on the console.
    try:
        app = create_app(cfg=cfg)
    except Exception:
        log.exception("Backend failed to start")
        raise
    service = app.config["SERVICE"]
    log.info("Backend ready on http://%s:%s (provider=%s model=%s dialect=%s powerbi=%s automation=%s)",
             host, port, service.pipeline.llm.name, service.pipeline.llm.model, service.dialect,
             service.powerbi.enabled, service.powerbi.automation_enabled)
    # threaded=True: every HTTP request gets its own thread, so /progress polls are answered
    # while a slow /chat is still running. use_reloader=False: no second process (that would
    # create duplicate sessions and background threads).
    app.run(host=host, port=port,
            debug=os.getenv("FLASK_DEBUG") == "1", use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
