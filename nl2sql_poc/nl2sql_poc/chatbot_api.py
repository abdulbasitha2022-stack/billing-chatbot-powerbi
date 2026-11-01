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
  GET  /health
Run:  python chatbot_api.py   (127.0.0.1:5056; set CHATBOT_HOST / CHATBOT_PORT / FLASK_DEBUG=1)
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, request

from app.audit import ConversationRecorder, format_conversation_text
from app.clock import app_timezone, to_day_id, to_utc_naive, utc_now
from app.conversation import DENIED_REPLY, ConversationEngine, Outcome, quick_take
from app.guardrails import GuardrailError, validate_sql
from app.identity import Identity, IdentityResolver
from app.naming import unique_view_name
from app.pipeline import VISUAL_TYPES, normalize_chart_type
from app.powerbi_project_generator import PowerBIProjectError
from app.report_store import ViewExistsError
from app.session import ChatSession, SessionStore
from app.util import json_safe
from app.access import AccessController

BASE = Path(__file__).parent
DEFAULT_CONFIG = BASE / "config_billing.yaml"
ALLOWED_VISUAL_TYPES = set(VISUAL_TYPES)
log = logging.getLogger("chatbot")

VISUAL_LABELS = {
    "auto": "Auto-select", "clusteredColumnChart": "Column chart", "barChart": "Bar chart",
    "lineChart": "Line chart", "pieChart": "Pie chart", "donutChart": "Donut chart",
    "tableEx": "Table",
}
CHART_CHIPS = [("clusteredColumnChart", "Column"), ("barChart", "Bar"), ("lineChart", "Line"),
               ("pieChart", "Pie"), ("donutChart", "Donut"), ("tableEx", "Table only")]
# Buttons for the chart question that closes the clarification phase.
CHOICE_CHIPS = [{"label": label, "action": "choose_chart", "visual_type": visual_type, "message": label}
                for visual_type, label in [("auto", "Auto"), ("clusteredColumnChart", "Column chart"),
                                           ("barChart", "Bar chart"), ("lineChart", "Line chart"),
                                           ("pieChart", "Pie chart"), ("donutChart", "Donut chart"),
                                           ("tableEx", "Table only")]]


END_REASONS = ("user", "new_chat", "finished_after_unsatisfied", "reset")
SESSION_ENDED_TEXT = ("Session ended. Your conversation and report records are saved. "
                      "Start a new conversation whenever you're ready.")


def visual_label(visual_type) -> str:
    return VISUAL_LABELS.get(visual_type or "auto", "Chart")


class ChatService:
    def __init__(self, cfg, pipeline, store, powerbi, sessions=None, background=None, recorder=None,
                 identity=None, clock=None):
        self.cfg = cfg
        self.recorder = recorder or ConversationRecorder(store, None)
        self.pipeline = pipeline
        self.store = store
        self.powerbi = powerbi
        self.sessions = sessions or SessionStore()
        self.engine = ConversationEngine(pipeline, cfg)
        self.engine.on_generation_started = self._on_generation_started
        self.dialect = pipeline.db.dialect
        self.allowed_tables = cfg["db"]["allowed_tables"]
        db_engine = getattr(pipeline.db, "engine", None)
        self.access = AccessController.from_engine(db_engine) if db_engine is not None else AccessController([])
        self.max_rows = int(cfg["db"].get("max_rows", 1000))
        self.source = f"{pipeline.llm.name}_api"
        chat_cfg = cfg.get("chat", {}) or {}
        self.allow_mock_sql = bool(chat_cfg.get("allow_mock_sql", True))
        self.resume_latest = bool(chat_cfg.get("resume_latest_session", True))
        self.resume_window_s = float(chat_cfg.get("resume_window_minutes", 480)) * 60
        self.end_wait_s = float(chat_cfg.get("end_wait_seconds", 2))
        self.tz = app_timezone(cfg)
        self.clock = clock or utc_now
        self.identity = identity or IdentityResolver(cfg)
        self._background = background or self._spawn

    @staticmethod
    def _spawn(fn):
        threading.Thread(target=fn, daemon=True, name="powerbi-reload").start()

    # ================================================================== /chat
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

        identity = identity or Identity(platform=self.identity.platform_for(body))
        if self.identity.demo_role_selection:
            if not identity.demo_role:
                return {"error": "Choose a valid demo role before starting or continuing a chat."}, 403
            if not identity.user_role and body.get("demo_role") is None and session_id:
                existing = self.sessions.get(session_id.strip())
                if existing and existing.demo_role_selected:
                    identity = Identity(user_role=existing.user_role,
                                        platform=identity.platform, demo_role=True)
            if identity.user_role not in ("CEO", "Developer", "Analyst"):
                return {"error": "Choose a valid demo role before starting or continuing a chat."}, 403
        session = self.sessions.get_or_create(session_id.strip() if session_id else None)
        if (identity.demo_role and session.user_role
                and identity.user_role != session.user_role):
            return {"error": "A demo role cannot change during a chat. Start a new chat to switch roles."}, 409
        session.clock = self.clock
        denied = self._claim_session(session, identity)
        if denied:
            return denied
        if session.ended:
            return self._ended_response(session)
        with session.lock:
            if session.ended:                 # ended while this request was waiting for the lock
                return self._ended_response(session)
            try:
                return self._handle_locked(session, message, visual_type, action, body,identity)
            finally:
                session.progress = None
                session.turn_started = None

    def _claim_session(self, session: ChatSession, identity: Identity):
        """A session belongs to the first VERIFIED user who uses it; nobody else may use it."""
        if session.user_id and identity.user_id != session.user_id:
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

    def _ended_response(self, session):
        return {"status": "session_ended", "session_ended": True, "session_id": session.session_id,
                "message": "This session has ended. Please start a new conversation."}, 409

    def _handle_locked(self, session, message, visual_type, action, body,identity):
        first_new_id = session.last_id
        session.turn_started = time.perf_counter()
        session.add_message("user", message)
        session.progress = "Understanding your request…"

        mock_sql = body.get("mock_sql")
        if isinstance(mock_sql, str) and mock_sql.strip() and self.allow_mock_sql:
            return self._mock_sql_turn(session, message, mock_sql, first_new_id,identity)

        if action == "feedback_followup":
            if not self._can_reuse_report(session, identity):
                return {"error": "Your role is not authorized to access this information."}, 403
            return self._followup_turn(session, message, body.get("choice"), first_new_id)

        if action == "change_chart":
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
        if not identity or not identity.user_role or not session.last_report:
            return False
        try:
            validate_sql(session.last_report.get("sql", ""), self.dialect,
                         self.allowed_tables, self.max_rows,
                         role=identity.user_role, access=self.access)
            return True
        except GuardrailError:
            return False

    # ------------------------------------------------------------------ turn dispatch
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
                session.add_message("assistant", outcome.reply)
                session.remember(user_text, outcome.reply)
                self.pipeline.log_turn(question=user_text, status="chat", intent=outcome.intent,
                                       prompt_score=outcome.confidence, latency_s=outcome.elapsed_ms / 1000)
                payload, status = {"status": "chat", "message": outcome.reply}, 200
        except Exception as exc:   # never lose the conversation because a side effect failed
            log.exception("Turn failed after planning")
            text = ("Something went wrong while finishing that request. Please try again. "
                    "The details are in the backend log.")
            failed_request_id = session.request_id
            if failed_request_id:
                self._finish_row(session, failed_request_id, "failed",
                                 error=f"Unexpected error: {type(exc).__name__}: {str(exc)[:500]}")
            session.add_message("assistant", text, kind="error")
            session.remember(user_text, text)
            session.end_thread()
            payload, status = {"status": "error", "message": text, "request_id": failed_request_id}, 500
        if session.end_requested and not session.ended:      # End was clicked while this ran
            self._end_session(session, session.end_reason or "user")
        return self._payload(session, first_new_id, outcome, payload), status

    def _payload(self, session, first_new_id, outcome, payload):
        payload = dict(payload)
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
    def _ensure_row(self, session: ChatSession) -> str:
        if session.request_id is None:
            started = session.thread_started or self.clock()
            request_id = uuid.uuid4().hex
            self.store.insert_metadata(
                session_id=session.session_id, request_id=request_id, user_id=session.user_id,
                user_role=session.user_role, platform=session.platform or self.identity.default_platform,
                sql_source=self.source, request_ts=to_utc_naive(started),
                day_id=to_day_id(started, self.tz), request_status="initiated")
            session.request_id = request_id
        return session.request_id

    def _safe_update(self, request_id, **fields):
        try:
            self.store.update_metadata(request_id, **fields)
        except Exception:
            log.exception("Could not update metadata for %s", request_id)

    def _set_final(self, request_id, status, response_ms, view_name=None, error=None):
        fields = {"request_status": status, "response_time_ms": int(response_ms),
                  "error_message": error[:4000] if error else None}
        if view_name:
            fields["view_name"] = view_name
        self._safe_update(request_id, **fields)

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
    def _clarify(self, session, user_text, outcome):
        self._close_turn(session)
        try:
            self._ensure_row(session)
        except Exception:
            log.exception("Could not record the clarification request")
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
        chips = getattr(outcome, "suggestions", None) or []
        session.add_message("assistant", outcome.reply, kind="text",
                            meta={"suggestions": chips} if chips else None)
        session.remember(user_text, f'{outcome.reply} [The request "{outcome.request}" returned no '
                                    "rows; no page was created.]")
        session.end_thread()
        return {"status": "no_data", "message": outcome.reply, "request_id": request_id,
                "result_row_count": 0}, 200

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
        self.pipeline.log_turn(question=user_text, status="denied", intent="denied", error=outcome.error,
                               latency_s=outcome.elapsed_ms / 1000)
        session.add_message("assistant", outcome.reply, kind="error")
        session.remember(user_text, f"{outcome.reply} [Access denied. This request is closed; "
                                    "do not continue it or ask about it again.]")
        session.end_thread()
        return {"status": "failed", "access_denied": True, "message": outcome.reply,
                "request_id": request_id}, 400

    # ------------------------------------------------------------------ new / modified report
    def _view_taken(self, name: str) -> bool:
        try:
            return bool(self.store.view_exists(name))
        except Exception:
            return False

    def _create_view(self, session, request_id, title, day, sql, question, assumptions,
                     session_id=None, used=None):
        """Pick the final view name, create the view, register it. Returns (name, ms).

        The SAME name is used for the CREATE VIEW, the registry, the metadata row and the reply."""
        session_id = session_id or (session.session_id if session is not None else None)
        taken_names = set(used or ())
        if session is not None:
            taken_names |= {r.get("view_name") for r in session.reports}
            session.progress = "Report generation in progress: saving the report view…"
        for _ in range(3):
            name = unique_view_name(title, day, session_id, request_id,
                                    lambda n: n in taken_names or self._view_taken(n))
            started = time.perf_counter()
            try:
                self.store.create_view(sql, name)
            except ViewExistsError:           # lost a race for the name: try the next suffix
                taken_names.add(name)
                continue
            elapsed = round((time.perf_counter() - started) * 1000)
            self.store.log_view(session_id, question, sql, name, assumptions)
            self._safe_update(request_id, view_name=name, request_status="in_progress")
            return name, elapsed
        raise RuntimeError("Could not find an unused view name")

    def _build_report(self, session, user_text, outcome):
        result = outcome.result
        request_id = self._ensure_row(session)
        title = outcome.title or report_title_from_request(outcome.request)
        assumptions = list(dict.fromkeys(list(result.assumptions or []) + list(outcome.assumptions or [])))
        day = to_day_id(session.thread_started or self.clock(), self.tz)
        try:
            validated = validate_sql(result.sql, self.dialect, self.allowed_tables, self.max_rows,
                                     role=session.user_role, access=self.access)
            view_name, view_ms = self._create_view(session, request_id, title, day, validated.sql,
                                                   outcome.request, assumptions)
        except GuardrailError as exc:
            self._finish_row(session, request_id, "failed", error=f"Safety check failed: {exc}")
            text = (DENIED_REPLY if "not authorized" in str(exc).lower() else
                    f"That query didn't pass the safety checks: {exc}. Could you rephrase the request?")
            session.add_message("assistant", text, kind="error")
            session.remember(user_text, text)
            session.end_thread()
            return {"status": "failed", "message": text, "request_id": request_id}, 400

        session.progress = "Report generation in progress: building the Power BI page…"
        project = self._build_page(view_name, title, outcome.chart_type, request_id=request_id, session_id=session.session_id)
        detail = project.pop("_detail", None)
        project["request_id"] = request_id
        page_ok = project.get("status") != "error"
        self._finish_row(
            session, request_id, "completed" if page_ok else "failed", view_name=view_name,
            error=None if page_ok else f"Power BI page was not created: {detail or project.get('message')}")
        response_ms = self._response_ms(session)

        columns = list(result.columns)
        rows = [[json_safe(v) for v in row] for row in result.rows]
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
        del session.reports[:-10]

        text = self._report_message(outcome, project, report)
        history_note = (f'{outcome.reply or "Built the report."} [Built Power BI page '
                        f'"{report["page_name"] or title}" for: {outcome.request}]')
        feedback_ok = bool(project.get("page_name")) and page_ok
        session.add_message("assistant", text, kind="report", meta={
            "page_name": report["page_name"], "view_name": view_name,
            "chart_type": report["visual_type"], "request_id": request_id,
            "feedback_enabled": feedback_ok, "feedback": None,
            "suggestions": self._suggestions(report)})
        session.remember(user_text, history_note)
        session.end_thread()
        self._schedule_reload(session, project)

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

    def _build_page(self, view_name, title, chart_type, reuse=False, request_id=None, session_id=None) -> dict:
        try:
            return self.powerbi.build_page(view_name, title, chart_type, reuse_existing_table=reuse,
                                           request_id=request_id, session_id=session_id)
        except Exception as exc:
            log.exception("Power BI page generation failed for view=%s", view_name)
            return {"status": "error", "view_name": view_name,
                    "message": "Power BI page generation failed. The details are in the backend log.",
                    "_detail": f"{type(exc).__name__}: {str(exc)[:1500]}"}

    # ------------------------------------------------------------------ chart-only change
    def _change_chart(self, session, user_text, outcome):
        """A different chart of the SAME view is a separate report page, so it is its own request."""
        last = session.last_report
        label = visual_label(outcome.chart_type)
        base_title = last.get("base_title") or last["title"]
        title = f"{base_title} - {label}"
        started = self.clock()
        request_id = uuid.uuid4().hex
        try:
            self.store.insert_metadata(
                session_id=session.session_id, request_id=request_id, user_id=session.user_id,
                user_role=session.user_role, platform=session.platform or self.identity.default_platform,
                sql_source="chart_change", request_ts=to_utc_naive(started),
                day_id=to_day_id(started, self.tz), request_status="initiated")
            self._safe_update(request_id, request_status="in_progress", view_name=last["view_name"])
        except Exception:
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
        return "\n\n".join(p for p in parts if p)

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
    def _schedule_reload(self, session, project):
        if project.get("status") != "generated_pending_reload_and_refresh":
            return
        if not self.powerbi.automation_enabled:
            return
        session.busy = True
        page = project.get("page_name")

        def job():
            time.sleep(self.powerbi.reload_delay)     # let the chat reply reach the visual first
            try:
                self.powerbi.reload_refresh(project)
                text = (f"Power BI has reloaded and refreshed “{page}”. "
                        "The charts should be visible now.")
                kind = "status"
            except Exception as exc:
                log.exception("Power BI reload/refresh failed")
                text = None

            with session.lock:
                if text:
                    session.add_message("assistant", text, kind=kind)
                session.busy = False

        self._background(job)


    # ================================================================== session lifecycle
    def _end_session(self, session: ChatSession, reason: str):
        """Mark the session ended (history and records are kept). Caller must hold session.lock."""
        if session.ended:
            return None
        if session.request_id:       # a report request was still open: close its row cleanly
            self._finish_row(session, session.request_id, "failed",
                             error="Session ended before the report was completed",
                             response_ms=session.runtime_ms)
        session.end_thread()         # also clears a pending chart choice
        session.ended, session.ended_at = True, time.time()
        session.end_reason, session.end_requested = reason, False
        entry = session.add_message("assistant", SESSION_ENDED_TEXT, kind="status",
                                    meta={"session_ended": True, "reason": reason})
        self.recorder.record(session, [entry], request_id=None, intent="session_end")
        return entry

    def end_session(self, session_id, reason=None, identity: Identity | None = None):
        """End a session. If a request is still running, the session ends right after it finishes
        (so the report and its database records are never cut in half)."""
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 64:
            return {"error": "session_id must be a non-empty string of 64 characters or fewer"}, 400
        session = self.sessions.get(session_id.strip())
        if session is None:
            return {"error": "session not found"}, 404
        if session.user_id and (identity or Identity()).user_id != session.user_id:
            return {"error": "This session belongs to another user."}, 403
        if session.ended:
            return {"ended": True, "already_ended": True, "session_id": session.session_id}, 200
        reason = reason if reason in END_REASONS else "user"
        session.end_requested, session.end_reason = True, reason
        # Setting the flag BEFORE trying the lock means a running turn will see it and end the
        # session itself when it finishes, so the request is never lost in between.
        if session.lock.acquire(timeout=self.end_wait_s):
            try:
                if not session.ended:
                    self._end_session(session, reason)
            finally:
                session.lock.release()
        if session.ended:
            ended_entries = [m for m in session.transcript if (m.get("meta") or {}).get("session_ended")]
            return json_safe({"ended": True, "session_id": session.session_id,
                              "messages": ended_entries[-1:]}), 200
        return {"ended": False, "pending": True, "session_id": session.session_id,
                "message": "Your current request is still running. "
                           "The session will end as soon as it finishes."}, 202

    def _followup_turn(self, session, message, choice, first_new_id):
        """The two buttons shown after 'Unsatisfied': Return to conversation / Finish session."""
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
        session_id, request_id = body.get("session_id"), body.get("request_id")
        satisfaction = body.get("satisfaction")
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
        session = self.sessions.get(session_id.strip())
        if session is None:
            return {"error": "session not found"}, 404
        if session.user_id and (identity or Identity()).user_id != session.user_id:
            return {"error": "This session belongs to another user."}, 403
        if session.ended:
            return self._ended_response(session)
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
            value = satisfaction == "satisfied"
            try:
                saved = self.store.set_satisfaction(request_id.strip(), session.session_id, value)
            except Exception:
                log.exception("Could not save feedback for %s", request_id)
                return {"status": "error",
                        "message": "I couldn't save your feedback. Please try again."}, 500
            if not saved["found"]:
                return {"error": "That report request was not found."}, 404

            first_new_id = session.last_id
            entry["meta"]["feedback"] = satisfaction
            user_entry = session.add_message("user", label)
            lead = (f"Thanks! Your feedback is recorded as **{label}**." if current is None
                    else f"I've updated your feedback to **{label}**.")
            meta = None
            if value:
                text = lead
            else:
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

    # ================================================================== other endpoints
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
        session = self.sessions.get(session_id)
        if session is None:
            return {"error": "session not found"}, 404
        return {"progress": session.progress, "busy": session.busy, "ended": session.ended}, 200

    # ------------------------------------------------------------------ audit
    def audit_sessions(self, limit=50):
        return {"sessions": self.recorder.list_sessions(limit)}, 200

    def audit_session(self, session_id):
        rows = self.recorder.read_session(session_id)
        return ({"session_id": session_id, "messages": rows}, 200) if rows \
            else ({"error": "no recorded conversation for this session"}, 404)

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
        if status == 404:
            return {"ok": True, "deleted": False, "ended": False}, 200
        payload.update(ok=True, deleted=False)
        return payload, status

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
        return self.handle_chat({"session_id": session.session_id, "action": "change_chart",
                                 "visual_type": visual_type,
                                 "message": f"Show it as a {visual_label(visual_type).lower()}"}, identity)

    # ------------------------------------------------------------------ manual-SQL test hooks
    def _mock_sql_turn(self, session, message, sql, first_new_id,identity):
        session.start_thread(message)
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
        started = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - started) * 1000)
        if not role or access is None or not access.normalize_role(role):
            return {
                "status": "failed",
                "message": "Your role is not authorized to access this information. Please contact your administrator if you believe you need access.",
            }, 403
        record = self.store.fetch_metadata(request_id)
        if record is None:
            return {"status": "failed", "error": "Unknown request_id"}, 404
        if identity is not None:
            if identity.demo_role:
                wrong_owner = record.get("user_id") is not None
            else:
                wrong_owner = not identity.user_id or identity.user_id != record.get("user_id")
            if wrong_owner or identity.user_role != record.get("user_role"):
                return {"status": "failed", "message": "This request belongs to another user."}, 403
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
        except GuardrailError as exc:
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
    import re
    title = re.sub(r"\s+", " ", str(request_text or "")).strip()
    title = re.sub(r"^(?:please\s+)?(?:(?:can|could|would) you\s+)?", "", title, flags=re.I)
    title = re.sub(r"^(?:show me|show|display|create|generate|make|build|give me|provide|list|find)\s+",
                   "", title, flags=re.I)
    title = re.sub(r"\s*,?\s*(?:sorted|sort|ordered|order)\s+(?:by|from|in)\b.*$", "", title, flags=re.I)
    title = title.strip(" .?!,:;-_")
    if not title:
        return fallback
    small = {"a", "an", "the", "and", "or", "but", "for", "nor", "on", "at", "to", "from", "by",
             "of", "in", "with", "per", "as"}
    words = title.title().split()
    words = [w.lower() if i and w.lower() in small else w for i, w in enumerate(words)]
    title = " ".join(words)
    for acronym in ("Usd", "Id", "Api", "Sql", "Ai"):
        title = re.sub(rf"\b{acronym}\b", acronym.upper(), title)
    if len(title) > 54:
        title = title[:54].rsplit(" ", 1)[0].strip() or title[:54]
    return title or fallback


# ====================================================================== Flask wiring
def create_app(cfg_path=None, *, cfg=None, llm=None, db=None, store=None, powerbi=None,
               background=None) -> Flask:
    """Build the app. Every dependency can be injected, which is how the tests run offline."""
    from app.db import build_db
    from app.llm import build_llm
    from app.pipeline import Pipeline
    from app.powerbi_service import PowerBIService
    from app.report_store import ReportStore

    if cfg is None:
        path = Path(cfg_path or os.getenv("CHATBOT_CONFIG") or DEFAULT_CONFIG)
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    store = store or ReportStore()
    pipeline = Pipeline(llm or build_llm(cfg["llm"]), db or build_db(cfg["db"]), cfg, base_dir=BASE)
    powerbi = powerbi or PowerBIService(cfg, BASE, store)
    log_file = (cfg.get("pipeline", {}) or {}).get("conversation_log_file")
    recorder = ConversationRecorder(store, BASE / log_file if log_file else None)
    service = ChatService(cfg, pipeline, store, powerbi, background=background, recorder=recorder)

    def who(body=None):
        """Trusted identity of the caller (None-values when unknown). Never from the request body."""
        return service.identity.resolve(request.headers, body)

    app = Flask(__name__)
    app.config["SERVICE"] = service

    @app.after_request
    def cors(response):
        # The visual runs in a sandboxed iframe, so any origin must be allowed (local POC only).
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        if request.headers.get("Access-Control-Request-Private-Network"):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

    def reply(result):
        payload, status = result
        return jsonify(payload), status

    @app.post("/chat")
    def chat():
        body = request.get_json(silent=True) or {}
        return reply(service.handle_chat(body, who(body)))

    @app.post("/feedback")
    def feedback():
        body = request.get_json(silent=True) or {}
        return reply(service.handle_feedback(body, who(body)))

    @app.post("/session/<session_id>/end")
    def session_end(session_id):
        body = request.get_json(silent=True) or {}
        return reply(service.end_session(session_id, body.get("reason"), who(body)))

    @app.get("/session/latest")
    def session_latest():
        return reply(service.latest(who({"demo_role": request.args.get("demo_role")})))

    @app.get("/session/<session_id>/transcript")
    def session_transcript(session_id):
        try:
            after = int(request.args.get("after", 0))
        except ValueError:
            after = 0
        return reply(service.transcript(session_id, after))

    @app.get("/session/<session_id>/progress")
    def session_progress(session_id):
        return reply(service.progress(session_id))

    @app.get("/audit/sessions")
    def audit_sessions():
        try:
            limit = max(1, min(500, int(request.args.get("limit", 50))))
        except ValueError:
            limit = 50
        return reply(service.audit_sessions(limit))

    @app.get("/audit/sessions/<session_id>")
    def audit_session(session_id):
        payload, status = service.audit_session(session_id)
        if request.args.get("format") == "text":
            text = format_conversation_text(session_id, payload.get("messages", []))
            return Response(text, status=status, mimetype="text/plain; charset=utf-8")
        return jsonify(payload), status

    @app.get("/audit/requests/<request_id>")
    def audit_request(request_id):
        return reply(service.audit_request(request_id))

    @app.post("/session/<session_id>/reset")
    def session_reset(session_id):
        return reply(service.reset(session_id, who()))

    @app.get("/session/<session_id>")
    def session_history(session_id):
        return reply(service.history(session_id))

    @app.post("/revise-report")
    def revise_report():
        body = request.get_json(silent=True) or {}
        return reply(service.revise_report(body, who(body)))

    @app.post("/complete-report")
    def complete_report():
        if not service.allow_mock_sql:       # same switch as mock_sql in /chat: a test-only hook
            return jsonify(error="not found"), 404
        body = request.get_json(silent=True) or {}
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip():
            return jsonify(error="request_id is required"), 400
        identity = who(body)
        return reply(service.complete_manual_report(
            request_id.strip(),
            body.get("generated_sql"),
            resolved_request=body.get("resolved_request"),
            assumptions=body.get("assumptions") or [],
            role=identity.user_role,
            access=service.access,
            identity=identity,
        ))

    @app.get("/requests/<request_id>")
    def request_status(request_id):
        try:
            row = service.store.fetch_status_row(request_id)
        except Exception:
            app.logger.exception("Could not read request metadata")
            return jsonify(error="Could not read the request record."), 500
        if row is None:
            return jsonify(error="request not found"), 404
        return jsonify(json_safe(row))

    @app.get("/health")
    def health():
        llm_ = service.pipeline.llm
        return jsonify(ok=True, mode="conversational", provider=llm_.name, model=llm_.model,
                       dialect=service.dialect,
                       demo_role_selection=service.identity.demo_role_selection)

    return app


def main():
    app = create_app()
    app.run(host=os.getenv("CHATBOT_HOST", "127.0.0.1"), port=int(os.getenv("CHATBOT_PORT", "5056")),
            debug=os.getenv("FLASK_DEBUG") == "1", use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
