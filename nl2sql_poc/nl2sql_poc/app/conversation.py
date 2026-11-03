"""Conversation engine: turns one user message into an Outcome using memory + the pipeline.

It decides WHAT should happen (chat reply, explanation, clarification, new/modified report, chart
change). It does not touch MySQL or Power BI - the API layer performs those side effects.
"""
# ---------------------------------------------------------------------------------------------
# WHAT THIS FILE DOES (beginner overview)
#   The "brain" of one chat turn. ConversationEngine.handle(session, message) decides what the
#   bot should do and returns an Outcome describing it. Steps in handle():
#     1. if a chart-type answer was pending, use it and go straight to SQL,
#     2. otherwise ask the planner LLM (Pipeline.plan) for an intent,
#     3. access check on the restricted data the planner says the user wants to see
#        (plan.restricted; mentioning data only to leave it out is fine),
#     4. apply guard rails: clarify if not ready, answer chat/explain, change chart,
#        ask for the chart type, or generate SQL (Pipeline.generate_sql via _generate()).
#   It also builds the text summary of the last report that the planner reads (state_block).
# WHO CALLS IT
#   chatbot_api.py: ChatService creates one ConversationEngine and calls handle() for every
#   /chat message (while holding session.lock). It then acts on Outcome.kind: saves the view
#   in MySQL, generates the Power BI page, writes metadata rows, etc.
# WHAT IT CALLS
#   app/pipeline.py (plan, generate_sql, restricted_requested), app/session.py fields/methods,
#   app/util.py formatting helpers, app/coverage.py (date range message for empty results).
# ---------------------------------------------------------------------------------------------
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from .pipeline import (REPORT_INTENTS, Pipeline, PlannerError, Plan, Result, Usage,
                       normalize_chart_type)
from .util import fmt_number, humanize, is_number, json_safe
from .coverage import DataCoverage, no_data_message

# Logger "app.conversation"; lines end up in logs/backend_billing.log (see app/logging_setup.py).
log = logging.getLogger(__name__)

# Column names containing these words hold rates/averages: adding them up gives nonsense
# (summing 3 monthly "churn_rate" values is meaningless), so column_stats() gives them no sum.
_SUM_UNFRIENDLY =("rate", "pct", "percent", "avg", "average", "ratio", "share")

# The fixed question asked once a request is fully clarified (the visual shows chips for it).
CHART_QUESTION = "What type of visual would you like: Auto, column, bar, line, pie, donut, or table?"
# Standard reply for restricted data. Also imported by chatbot_api.py.
DENIED_REPLY =("You are not permitted to access this information. "
                "Please contact your administrator if you need access.")
# Phrases in a typed reply to the chart question that mean "you pick" -> "auto".
_AUTO_WORDS =("auto", "automatic", "you choose", "your choice", "you pick", "any", "anything",
               "whatever", "no preference", "dont care", "don't care", "doesnt matter",
               "doesn't matter", "best", "default", "surprise")


# detect_chart_choice: used by handle() when the user TYPES an answer to the chart question
# instead of clicking a chip. Plain keyword matching (no LLM call) keeps it fast and free.
# Returns a Power BI visual type string, "auto", or None.
def detect_chart_choice(text: str):
    """Chart type named in a SHORT reply to the chart question, else None.

    Long messages are treated as new requests, so "show refunds as a pie chart for 2025 instead"
    is planned normally rather than being read as an answer."""
    # Lower-case and keep only letters, apostrophes and spaces.
    cleaned = re.sub(r"[^a-z' ]", " ", (text or "").lower())
    words = cleaned.split()
    # More than 8 words is probably a new request, not a chart answer.
    if not words or len(words) > 8:
        return None
    # Spaces on both ends so " any " style whole-word checks work at the start/end too.
    # \b in the regexes = word boundary, so "barchart" or "online" do not match by accident.
    # Order matters: the first match wins (e.g. "donut" is checked before "pie").
    padded = f" {' '.join(words)} "
    if re.search(r"\b(donut|doughnut)\b", padded):
        return "donutChart"
    if re.search(r"\bpie\b", padded):
        return "pieChart"
    if re.search(r"\b(column|columns|vertical)\b", padded):
        return "clusteredColumnChart"
    if re.search(r"\b(bar|bars|horizontal)\b", padded):
        return "barChart"
    if re.search(r"\b(line|trend)\b", padded):
        return "lineChart"
    if re.search(r"\b(table|grid)\b", padded):
        return "tableEx"
    if any(f" {word} " in padded for word in _AUTO_WORDS):
        return "auto"
    return None


# Outcome: the engine's decision for one turn, returned by handle() to chatbot_api.py, which
# dispatches on `kind` in ChatService._finish_turn(). Besides the kinds listed on the line
# below, the code also returns "choose_chart" (ask the chart question) and "no_data" (the
# query returned 0 rows).
#   result: the pipeline Result (SQL, columns, rows) for report-like outcomes.
#   previous_report: the report being modified (modify_report), else None.
#   confidence: the planner's readiness score; elapsed_ms: time spent in the engine.
@dataclass
class Outcome:
    kind: str                      # reply | clarify | report | chart_change | denied | failed | error
    reply: str = ""
    intent: str = ""
    request: str = ""
    title: str = ""
    chart_type: str = "auto"
    assumptions: list = field(default_factory=list)
    result: Result | None = None
    previous_report: dict | None = None
    confidence: int = 0
    error: str = ""
    elapsed_ms: int = 0


# --------------------------------------------------------------------------- data summaries
# These helpers summarise report data WITHOUT an LLM, so numbers are exact.

# _label_for: the first non-numeric value in a row (e.g. the month or plan name), used to say
# WHICH row holds the min/max ("max 5,200 (2025-03)"). None if the row has no label column.
def _label_for(row, columns, numeric_idx):
    for i, value in enumerate(row):
        if i not in numeric_idx and value is not None:
            return str(json_safe(value))
    return None


def column_stats(columns, rows):
    """{column: {sum?, min, max, min_label, max_label}} for numeric columns, over ALL rows."""
    # Used by report_prompt_block() and quick_take(). rows are tuples, columns are names.
    stats = {}
    # A column counts as numeric if at least one of its values is a number.
    numeric_idx = {i for i in range(len(columns))
                   if any(is_number(r[i]) for r in rows)}
    for i in sorted(numeric_idx):
        # (value, whole row) pairs, so we can later find the label of the min/max row.
        values = [(float(r[i]), r) for r in rows if is_number(r[i])]
        if not values:
            continue
        low, high = min(values, key=lambda v: v[0]), max(values, key=lambda v: v[0])
        entry = {"min": low[0], "max": high[0],
                 "min_label": _label_for(low[1], columns, numeric_idx),
                 "max_label": _label_for(high[1], columns, numeric_idx)}
        if not any(h in columns[i].lower() for h in _SUM_UNFRIENDLY):
            entry["sum"] = sum(v[0] for v in values)
        stats[columns[i]] = entry
    return stats


# _csv_cell: format one value for the small CSV sample in the planner prompt
# (None -> empty, floats rounded to 2 decimals, quotes around text that contains a comma).
def _csv_cell(value):
    value = json_safe(value)
    if value is None:
        return ""
    if isinstance(value, float):
        value = round(value, 2)
    text = str(value)
    return f'"{text}"' if "," in text else text


def report_prompt_block(report: dict, max_rows: int = 20) -> str:
    """Compact description of the last report for the planner (it explains from this)."""
    # Called by state_block(). `report` is session.last_report (built by chatbot_api.py).
    # Output: page name + chart, request, assumptions, SQL, per-column sum/min/max computed over
    # ALL rows, then the first `max_rows` rows as CSV. Pre-computed totals let the planner answer
    # "what is the total?" correctly even when only a sample of rows fits in the prompt.
    columns, rows = report["columns"], report["rows"]
    lines = [
        f'LAST REPORT: page "{report.get("page_name") or report.get("title")}", '
        f'chart: {report.get("visual_type", "auto")}',
        f'- Request: {report.get("request", "")}',
    ]
    if report.get("assumptions"):
        lines.append("- Assumptions: " + "; ".join(report["assumptions"]))
    if report.get("sql"):
        # Collapse whitespace to one line and cap at 1200 characters to save tokens.
        lines.append("- SQL used: " + " ".join(report["sql"].split())[:1200])
    lines.append("- Columns: " + ", ".join(columns))
    if not rows:
        lines.append("- Rows: 0 (the query returned no data for these filters)")
        return "\n".join(lines)
    note = " (TRUNCATED at the row cap, so totals are partial)" if report.get("truncated") else ""
    lines.append(f"- Rows: {len(rows)}{note}")
    for column, stat in column_stats(columns, rows).items():
        parts = []
        if "sum" in stat:
            parts.append(f"sum {fmt_number(stat['sum'], column)}")
        parts.append(f"min {fmt_number(stat['min'], column)}"
                     + (f" ({stat['min_label']})" if stat["min_label"] else ""))
        parts.append(f"max {fmt_number(stat['max'], column)}"
                     + (f" ({stat['max_label']})" if stat["max_label"] else ""))
        lines.append(f"- {column}: " + " | ".join(parts) + "  [over all rows]")
    shown = rows[:max_rows]
    lines.append(f"DATA (first {len(shown)} of {len(rows)} rows, CSV):")
    lines.append(",".join(columns))
    lines.extend(",".join(_csv_cell(v) for v in row) for row in shown)
    return "\n".join(lines)


def quick_take(report: dict) -> str:
    """One deterministic sentence about the data (no LLM, so the numbers are always exact)."""
    # Imported by chatbot_api.py and added to the "report ready" reply.
    # Cases: no rows / no numeric column / exactly one row (list its values) / many rows
    # (highest and lowest of the main measure).
    columns, rows = report["columns"], report["rows"]
    if not rows:
        return ("The query ran but returned no rows, so the charts will be empty. "
                "Try a wider period or fewer filters.")
    numeric = [i for i in range(len(columns)) if any(is_number(r[i]) for r in rows)]
    if not numeric:
        return f"It returned {len(rows):,} row{'s' if len(rows) != 1 else ''}."
    if len(rows) == 1:
        parts = [f"{humanize(columns[i])}: {fmt_number(rows[0][i], columns[i])}"
                 for i in numeric if is_number(rows[0][i])]
        return "Result — " + ", ".join(parts[:4]) + "."
    # Prefer the measure the Power BI page charts (measure_field); else the first numeric column.
    measure = report.get("measure_field")
    mi =columns.index(measure) if measure in columns else numeric[0]
    stats = column_stats(columns, rows).get(columns[mi])
    if not stats:
        return f"It returned {len(rows):,} rows."
    text = f"{len(rows):,} rows. Highest {humanize(columns[mi])}: " \
           f"{fmt_number(stats['max'], columns[mi])}"
    if stats["max_label"]:
        text += f" ({stats['max_label']})"
    text += f"; lowest: {fmt_number(stats['min'], columns[mi])}"
    if stats["min_label"]:
        text += f" ({stats['min_label']})"
    return text + "."


# --------------------------------------------------------------------------- engine
# ConversationEngine: one instance, created by chatbot_api.py at startup and shared by all
# sessions. It keeps no per-user state itself; everything per conversation is read from and
# written to the ChatSession passed into handle().
class ConversationEngine:
    # pipeline: the shared Pipeline (LLM + DB); cfg: the parsed config YAML.
    def __init__(self, pipeline: Pipeline, cfg: dict):
        self.pipeline = pipeline
        pcfg = cfg.get("pipeline", {})
        # 0 = no limit. The cap only stops endless question loops; a request that scores below
        # the threshold (e.g. "generate a report") is always asked about, whatever the cap.
        self.max_clarifications = int(pcfg.get("max_clarifications", 4))
        # Minimum planner score (0-100) for a report request to be built without asking more.
        self.threshold =int(pcfg.get("prompt_eval_threshold", 40))
        # Ask for the chart type as the LAST question, once the request is fully clarified.
        self.ask_chart = bool((cfg.get("chat", {}) or {}).get("ask_chart_type", True))
        # A query that matches no rows is reported (with the data's date range) instead of
        # becoming a blank Power BI page. Set chat.skip_empty_reports: false to turn off.
        self.skip_empty_reports = bool((cfg.get("chat", {}) or {}).get("skip_empty_reports", True))
        # Knows the date range of the data, used to explain an empty result (app/coverage.py).
        self.coverage = DataCoverage.from_config(self.pipeline.db, cfg)
        # Called with the session the moment SQL generation starts. The API layer uses it to move
        # the request's metadata row from "initiated" to "in_progress".
        # A "hook" (callback) keeps this module free of database code: chatbot_api.py plugs in
        # its own function (ChatService._on_generation_started) after creating the engine.
        self.on_generation_started = None
        # Character budget of chat history sent to the planner (see ChatSession.llm_history).
        self.history_chars = int(pcfg.get("history_chars", 3500))
        # How many rows of the last report are shown to the planner as CSV.
        self.data_rows = int(pcfg.get("data_rows_in_prompt", 20))

    # ------------------------------------------------------------------ state for the planner
    # state_block: plain-English facts about where the conversation stands, placed in the
    # "## Conversation state" section of the planner prompt. The LLM itself is stateless, so
    # everything it should know (pending question, remaining budget, last report data) is
    # re-sent on every turn.
    def state_block(self, session) -> str:
        lines = []
        if session.awaiting_clarification:
            lines.append("You just asked the user a clarifying question. Their latest message most "
                         "likely answers it: merge it into the earlier request.")
        if self.max_clarifications > 0:
            left = max(0, self.max_clarifications - session.clarify_count)
            lines.append(f"Clarification budget left for this request: {left}.")
        else:
            lines.append("Clarification budget: unlimited (still ask only what is needed).")
        if session.pending_chart:
            lines.append("The system is waiting for the user's chart choice for the report: "
                         f'"{session.pending_chart["request"]}".')
        if session.last_report:
            lines.append(report_prompt_block(session.last_report, self.data_rows))
        else:
            lines.append("LAST REPORT: none yet. There is nothing to explain, modify or re-chart.")
        if len(session.reports) > 1:
            # Up to 5 earlier report names (all except the last one, which is described above).
            earlier ="; ".join(f'"{r["page_name"] or r["title"]}"' for r in session.reports[:-1][-5:])
            lines.append(f"Earlier reports in this conversation: {earlier}.")
        return "\n".join(lines)

    # _fallback_request: the request text to build. Normally the planner's merged `request`;
    # if it left that empty, glue together the user's own messages of this request thread.
    def _fallback_request(self, session, plan: Plan, user_text: str) -> str:
        if plan.request:
            return plan.request
        messages = session.thread_messages or [user_text]
        if len(messages) == 1:
            return messages[0]
        return "Original request: " + messages[0] + "\nClarifications: " + " | ".join(messages[1:])

    # ------------------------------------------------------------------ main entry
    def handle(
        self,
        session,
        user_text: str,
        preferred_chart: str | None = None,
        chart_choice: str | None = None,
        role: str | None = None,
        access=None,
    ) -> Outcome:
        """chart_choice: a chip click on the chart question (may be "auto")."""
        # Inputs (from chatbot_api._handle_locked):
        #   session: the ChatSession (caller already holds session.lock).
        #   user_text: the message typed by the user.
        #   preferred_chart: visual type sent with a normal message (a chart picked in the UI).
        #   chart_choice: set only for action "choose_chart" (a chip click on the chart question).
        #   role / access: user's role and the AccessController (app/access.py) for permissions.
        # Output: an Outcome. This method changes session fields (clarify_count, pending_chart,
        # thread_messages, ...) but performs no database or Power BI work itself.
        started = time.perf_counter()
        # Small helper: milliseconds since this turn started (called at each return point).
        elapsed = lambda:round((time.perf_counter() - started) * 1000)

        # A request waiting for its chart choice already passed the access check when it was planned.
        pending = session.pending_chart

        # 0. Are we waiting for the chart choice? ----------------------------------------------
        # A chip was clicked but no request is waiting for it (e.g. an old chip): treat a real
        # chart type as "change the chart of the last report", otherwise just say so.
        if chart_choice is not None and not pending:
            if session.last_report and normalize_chart_type(chart_choice) not in (None, "auto"):
                return Outcome(kind="chart_change", intent="change_chart",
                               chart_type=normalize_chart_type(chart_choice), elapsed_ms=elapsed())
            return Outcome(kind="reply", intent="chat", elapsed_ms=elapsed(),
                           reply="I'm not waiting on a chart choice right now. What would you like to see?")
        # A request is waiting for its chart type: take the chip value, or try to read a chart
        # word from the typed text. If found, build the stored request now - no planner call.
        # If not found, fall through: the message is planned normally (it may be a new request).
        if pending:
            choice = normalize_chart_type(chart_choice) if chart_choice is not None \
                else detect_chart_choice(user_text)
            if choice:
                log.info("Chart choice %s received for the pending request; generating", choice)
                return self._generate(
                    session,
                    pending["intent"],
                    pending["request"],
                    pending["title"],
                    choice,
                    pending["assumptions"],
                    pending["previous"],
                    pending["confidence"],
                    pending["reply"],
                    Usage(),
                    started,
                    role=role,
                    access=access,
                )

        # 1. Ask the planner LLM: recent memory + the new message, with the state in the prompt.
        history = session.llm_history(self.history_chars) + [{"role": "user", "content": user_text}]
        try:
            plan, usage = self.pipeline.plan(history, self.state_block(session), role=role, access=access)
        # The LLM was unreachable or kept returning junk: return a friendly "error" Outcome
        # (chatbot_api turns it into an error reply) instead of letting the exception escape.
        except PlannerError as exc:
            log.error("Planner failed: %s", exc)
            return Outcome(kind="error", intent="error", error=str(exc), elapsed_ms=elapsed(),
                           reply="Sorry, I couldn't reach my AI service just now, so I didn't get "
                                 "your request. Please try again in a moment.")
        # Access check on what the planner says the user wants to SEE (it understands paraphrases,
        # follow-ups and "without <column>"), before any of its text reaches the user and before any
        # SQL is written. Every intent is checked: a reply about restricted data must not reach
        # the user as "the data does not exist".
        denied = self._denied_reference(role, access, plan)
        if denied:
            return self._denied(denied, elapsed())
        intent = plan.intent
        has_report = session.last_report is not None
        # A chart type sent by the UI with the message beats the planner's guess.
        explicit_chart =normalize_chart_type(preferred_chart) if preferred_chart else None

        # Guard rails around the model's choice ------------------------------------------------
        # The LLM can be wrong; these rules correct choices that are impossible in this state.
        if intent == "modify_report" and not has_report:
            log.info("Planner said modify_report but there is no report yet; treating it as new_report")
            intent = "new_report"
        if intent in ("explain", "change_chart") and not has_report:
            return self._reply(plan, "I don't have a report to work with yet. What would you like "
                                     "to see? For example: \"monthly revenue for the last 12 months\".",
                               elapsed())

        # Readiness check (same rule as the original evaluator): a report request is only built
        # when the model says it is sufficient AND its score reaches the threshold.
        below_floor = intent in REPORT_INTENTS and plan.scored and plan.confidence < self.threshold
        not_ready = intent in REPORT_INTENTS and (not plan.sufficient or below_floor)
        if intent == "clarify" or not_ready:
            # budget_left: True when there is no cap (0) or fewer questions than the cap were asked.
            budget_left = (self.max_clarifications <= 0
                           or session.clarify_count < self.max_clarifications)
            # Ask a question if budget remains, OR if the request is too vague to build anyway
            # (score under the threshold), even when the budget is used up.
            if budget_left or below_floor or (intent == "clarify" and plan.scored
                                              and plan.confidence < self.threshold):
                session.pending_chart = None   # a new question cancels any waiting chart choice
                # First question of this request: start a new request thread; otherwise add
                # the user's answer to the existing thread.
                if not session.awaiting_clarification:
                    session.start_thread(user_text)
                else:
                    session.thread_messages.append(user_text)
                session.clarify_count += 1
                session.awaiting_clarification = True
                question = (plan.clarification or plan.reply
                            or "Could you tell me more about what the report should show?")
                return Outcome(kind="clarify", intent="clarify", confidence=plan.confidence,
                               reply=question, elapsed_ms=elapsed())
            if intent == "clarify":          # budget used up and enough to go on: proceed
                log.info("Clarification budget (%d) used up; building the report with what we have",
                         self.max_clarifications)
                intent = "new_report"

        # Plain answers: no SQL, no new page. "explain" answers come from the LAST REPORT data
        # that state_block() put in the prompt.
        if intent in ("chat", "explain", "decline"):
            text = plan.reply or "I'm here to help with your billing reports."
            if session.pending_chart:      # still waiting: answer, then remind
                text += f"\n\nTo build your report: {CHART_QUESTION}"
            return self._reply(plan, text, elapsed(), intent)

        # Only the visual type changes: chatbot_api reuses the saved view/data, so no new SQL.
        if intent == "change_chart":
            chart = explicit_chart if explicit_chart not in (None, "auto") else plan.chart_type
            if chart in (None, "auto"):
                return self._reply(plan, plan.reply or "Which chart would you like: column, bar, line, "
                                                       "pie, donut or table?", elapsed(), "chat")
            return Outcome(kind="chart_change", intent="change_chart", chart_type=chart,
                           confidence=plan.confidence, reply=plan.reply, elapsed_ms=elapsed())

        # Report intents -----------------------------------------------------------------------
        if not session.awaiting_clarification:
            session.start_thread(user_text)
        else:
            session.thread_messages.append(user_text)
        request = self._fallback_request(session, plan, user_text)
        # For a modification, pass the last report so the SQL model edits its query.
        previous = session.last_report if intent == "modify_report" else None
        chart = explicit_chart if explicit_chart not in (None, "auto") else plan.chart_type
        chart_given = chart not in (None, "auto")
        if intent == "modify_report" and not chart_given:
            # A revision keeps the chart type of the report being revised; no need to ask again.
            chart, chart_given = (session.last_report or {}).get("visual_type") or "auto", True

        if self.ask_chart and not chart_given:
            # Everything is clarified: the chart type is the LAST question. Hold the request and
            # run the SQL only after the answer, so the query is executed exactly once.
            session.pending_chart = {
                "intent": intent, "request": request, "title": plan.title,
                "assumptions": plan.assumptions, "previous": previous,
                "confidence": plan.confidence, "reply": plan.reply}
            session.awaiting_clarification = False
            question = f"{plan.reply}\n\n{CHART_QUESTION}" if plan.reply else CHART_QUESTION
            # chatbot_api._ask_chart() shows this question with chart chips.
            return Outcome(kind="choose_chart", intent="choose_chart", request=request,
                           title=plan.title, assumptions=plan.assumptions,
                           confidence=plan.confidence, reply=question, elapsed_ms=elapsed())
        return self._generate(session, intent, request, plan.title, chart or "auto",
                              plan.assumptions, previous, plan.confidence, plan.reply, usage, started,
                              role=role, access=access)

    # ------------------------------------------------------------------ SQL + outcome
    # _generate: run SQL generation for a ready request and turn the pipeline Result into an
    # Outcome. Called from handle() (chart already known) or when the chart answer arrives.
    # `usage` carries the planner's token counts so they are added to the Result.
    # `started` is the turn's start time, so elapsed_ms covers the whole turn.
    def _generate(self, session, intent, request, title, chart, assumptions, previous,
                  confidence, reply, usage, started,role=None, access=None) -> Outcome:
        elapsed = lambda: round((time.perf_counter() - started) * 1000)
        session.pending_chart = None
        # The visual polls /session/<id>/progress and shows this text while it waits.
        session.progress = "Report generation in progress: writing and running the query…"
        # Tell chatbot_api to create/update the request's metadata row as "in_progress".
        if self.on_generation_started:
            try:
                self.on_generation_started(session)
            except Exception:             # bookkeeping must never stop a report; it is retried later
                log.exception("on_generation_started hook failed; continuing with the report")
        log.info("Generating %s: chart=%s title=%r", intent, chart or "auto", title)
        log.debug("Resolved request: %s", request)
        result = self.pipeline.generate_sql(request, previous=previous, assumptions=assumptions,role=role,
        access=access)
        result.intent, result.prompt_score = intent, confidence
        result.input_tokens += usage.input_tokens
        result.output_tokens += usage.output_tokens
        result.cache_read_tokens += usage.cache_read_tokens
        # Query worked but returned nothing: explain (with the data's date range) and offer
        # suggestion chips instead of building an empty Power BI page.
        if result.status == "ok" and not result.rows and self.skip_empty_reports:
            reply, chips = no_data_message(self.coverage, result.sql)
            outcome = Outcome(kind="no_data", intent=intent, request=request, title=title,
                              chart_type=chart or "auto", assumptions=assumptions, result=result,
                              confidence=confidence, reply=reply, elapsed_ms=elapsed())
            # `suggestions` is not a declared Outcome field; it is attached as an extra attribute.
            outcome.suggestions = chips
            return outcome

        # Fields shared by the outcomes below, passed with **common.
        common = dict(intent=intent, request=request, title=title, chart_type=chart or "auto",
                      assumptions=assumptions, result=result, previous_report=previous,
                      confidence=confidence, elapsed_ms=elapsed())
        # Map Result.status -> Outcome.kind:
        #   ok -> report (chatbot_api saves the view and builds the Power BI page)
        #   clarify -> clarify (the SQL model needs one more detail)
        #   error -> error (LLM unreachable); "not authorized" -> denied; anything else -> failed
        if result.status == "ok":
            return Outcome(kind="report", reply=reply, **common)
        if result.status == "clarify":
            session.clarify_count += 1
            session.awaiting_clarification = True
            return Outcome(kind="clarify", intent="clarify", confidence=confidence,
                           reply=result.clarification, elapsed_ms=elapsed())
        # Technical detail for logs/metadata (shortened); the user gets a friendly reply.
        detail = (result.error or "unknown error")[:300]
        if result.status == "error":
            return Outcome(kind="error", error=detail, reply=(
                "I ran into a technical problem while building that report. "
                "Please try again in a moment."), **common)
        if "not authorized to access this information" in detail.lower():
            # Last layer: the SQL guardrail refused the generated query for this role.
            return Outcome(kind="denied", error=f"Access denied: {detail}", reply=DENIED_REPLY, **common)
        reply = ("I couldn't turn that into a working query after a few tries. "
                 "Could you rephrase it or give me a bit more detail?")
        return Outcome(kind="failed", error=detail, reply=reply, **common)

    # ------------------------------------------------------------------ access
    def _denied_reference(self, role, access, plan):
        """Restricted table/column the plan says the user wants to see (per the backend rules), else None."""
        if access is None:
            return None                   # no rules loaded here; the SQL guardrail still denies
        return self.pipeline.restricted_requested(plan.restricted, role, access)

    # _denied: the "access denied" Outcome. Only the generic DENIED_REPLY reaches the user; the
    # restricted resource name goes into `error` (for logs and the metadata table).
    # @staticmethod: it does not need `self`.
    @staticmethod
    def _denied(resource, elapsed_ms) -> Outcome:
        return Outcome(kind="denied", intent="denied", reply=DENIED_REPLY,
                       error=f"Access denied: request needs restricted data ({resource})",
                       elapsed_ms=elapsed_ms)

    # _reply: a plain text answer Outcome (kind "reply"); chatbot_api just shows the text.
    def _reply(self, plan, text, elapsed_ms, intent=None) -> Outcome:
        return Outcome(kind="reply", intent=intent or plan.intent, reply=text,
                       confidence=plan.confidence, elapsed_ms=elapsed_ms)
