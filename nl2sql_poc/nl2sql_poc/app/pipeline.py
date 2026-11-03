"""Pipeline: conversational planner, SQL generation, validation and read-only execution.

Two LLM stages, both stateless helpers (conversation memory lives in session.py):

  plan()          chat history + state  -> Plan (intent, reply, self-contained request, ...)
  generate_sql()  request (+ old SQL)   -> Result (validated SQL executed read-only)
"""
# ---------------------------------------------------------------------------------------------
# WHAT THIS FILE DOES (beginner overview)
#   The "AI + SQL" engine room. The Pipeline class:
#     1. plan():         asks the planner LLM what the user wants (intent, clean request, score).
#     2. generate_sql(): asks the SQL LLM to write ONE SELECT, checks it with app/guardrails.py
#                        (read-only, allowed tables/columns for the role), runs it read-only on
#                        MySQL through app/db.py, and retries with the error message if needed.
#   It also builds the system prompts (via app/prompt.py), hides tables/columns a role may not
#   see (via app/access.py), and appends one JSON line per request to a log file.
# WHO CALLS IT
#   app/conversation.py (ConversationEngine) calls plan() and generate_sql() for every chat turn.
#   chatbot_api.py creates the Pipeline and calls log_turn(). run.py (command line) uses ask().
# WHAT IT CALLS
#   self.llm (app/llm.py provider: .complete(system, messages)), self.db (app/db.py:
#   describe, table_columns, execute_readonly), guardrails.validate_sql / tables_for_role.
#   It never writes to MySQL and never touches Power BI.
# ---------------------------------------------------------------------------------------------
import getpass
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .db import QueryError
from .guardrails import GuardrailError, validate_sql, tables_for_role
from .prompt import build_planner_prompt, build_system_prompt

# Logger "app.pipeline": lines go to the console and logs/backend_billing.log (set up by
# app/logging_setup.py). Each line is tagged with sid=/rid= (session id / request id).
log = logging.getLogger(__name__)

# The one standard message shown when the role may not read the data. conversation.py looks
# for the words "not authorized to access this information" to recognise a denial.
DENIED_ERROR =("Your role is not authorized to access this information. "
                "Please contact your administrator if you believe you need access.")
# Intents that lead to SQL + a new Power BI page.
REPORT_INTENTS = ("new_report", "modify_report")
# Every intent the planner may return (must match the list in prompt.PLANNER_TEMPLATE).
INTENTS =("chat", "explain", "new_report", "modify_report", "change_chart", "clarify", "decline")
# Power BI visual type names this project can generate ("auto" = let the generator choose).
VISUAL_TYPES = ("auto", "clusteredColumnChart", "barChart", "lineChart", "pieChart", "donutChart", "tableEx")

# Everyday chart words (from the user or the LLM) -> official Power BI visual type.
# Keys are lower-case letters only, because normalize_chart_type() strips everything else
# ("Vertical bar" -> "verticalbar").
_CHART_ALIASES = {
    "auto": "auto", "automatic": "auto", "": "auto",
    "column": "clusteredColumnChart", "columns": "clusteredColumnChart",
    "columnchart": "clusteredColumnChart", "clusteredcolumnchart": "clusteredColumnChart",
    "verticalbar": "clusteredColumnChart", "verticalbarchart": "clusteredColumnChart",
    "bar": "barChart", "barchart": "barChart", "horizontalbar": "barChart",
    "horizontalbarchart": "barChart",
    "line": "lineChart", "linechart": "lineChart", "trend": "lineChart",
    "pie": "pieChart", "piechart": "pieChart",
    "donut": "donutChart", "doughnut": "donutChart", "donutchart": "donutChart",
    "doughnutchart": "donutChart",
    "table": "tableEx", "tableex": "tableEx", "grid": "tableEx", "tableonly": "tableEx",
}


# Used by parse_plan(), conversation.py and chatbot_api.py whenever a chart name comes in.
# Returns "auto" for a missing value, a visual type for a known word, or None for nonsense
# (callers treat None as "no usable chart choice").
def normalize_chart_type(value):
    """Map a model/user chart name to a Power BI visual type; None when unrecognised."""
    if value is None:
        return "auto"
    key = re.sub(r"[^a-z]", "", str(value).lower())
    return _CHART_ALIASES.get(key)


# --------------------------------------------------------------------------- SQL reply parsing
# Parsed = the SQL LLM's reply after parse_response() has read it.
#   kind "sql": `sql` holds the query (+ optional assumptions);
#   kind "clarify": the model asked a question instead (`clarification`);
#   kind "invalid": the reply did not follow the protocol (generate_sql will retry).
@dataclass
class Parsed:
    kind: str  # "sql" | "clarify" | "invalid"
    sql: str = ""
    assumptions: list = field(default_factory=list)
    clarification: str = ""


# Pre-compiled regular expressions (compiled once at import, reused for every reply):
#   _FENCE   captures the text inside a ``` or ```sql code block (re.S lets "." span lines).
#   _ASSUME  finds every line starting with "ASSUMPTION:" (re.M makes ^/$ match per line).
#   _CLARIFY finds a line starting with "CLARIFY:". re.I = case-insensitive.
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)
_ASSUME = re.compile(r"^\s*ASSUMPTION:\s*(.+?)\s*$", re.M | re.I)
_CLARIFY = re.compile(r"^\s*CLARIFY:\s*(.+?)\s*$", re.M | re.I)


# parse_response: turn the raw text of the SQL LLM into a Parsed object.
# Order of checks: a fenced SQL block wins; otherwise a CLARIFY line; otherwise a bare reply
# that starts with WITH/SELECT is accepted as SQL; anything else is "invalid".
# Called by Pipeline.generate_sql(). The SQL is NOT trusted here - validate_sql() checks it.
def parse_response(text: str) -> Parsed:
    fence = _FENCE.search(text)
    assumptions = [match.group(1) for match in _ASSUME.finditer(text)]
    if fence and fence.group(1).strip():
        return Parsed("sql", sql=fence.group(1).strip(), assumptions=assumptions)
    clarification = _CLARIFY.search(text)
    if clarification:
        return Parsed("clarify", clarification=clarification.group(1))
    if re.match(r"\s*(with|select)\b", text, re.I):
        return Parsed("sql", sql=text.strip(), assumptions=assumptions)
    return Parsed("invalid")


# --------------------------------------------------------------------------- planner parsing
# Plan = the planner LLM's decision for one user message (built by parse_plan()).
#   intent: one of INTENTS; reply: text for the user; request: self-contained report request
#   (for new/modify_report); title: page title; chart_type: Power BI visual type or "auto";
#   assumptions: defaults the model chose; sufficient/clarification: is the request ready, and
#   if not, the question to ask. ConversationEngine.handle() acts on it.
@dataclass
class Plan:
    intent: str  # What the AI decided to do, such as chat, explain, or create a report.
    reply: str = ""      # The message the AI wants to show the user.
    request: str = ""      # The complete report request, if the user wants a report.
    title: str = ""    #title to use for the Power BI report page.
    chart_type: str = "auto"
    assumptions: list = field(default_factory=list)
    confidence: int = 0          # the readiness score (0-100) the model gave the request
    scored: bool = False         # False when the model gave no score at all
    sufficient: bool = True
    clarification: str = ""


# _extract_json_object: LLMs sometimes wrap JSON in ```json fences or add chatter around it.
# Try plain json.loads first; if that fails, parse the part between the first "{" and the last
# "}". Raises ValueError (caught in Pipeline.plan, which then retries once).
def _extract_json_object(text: str):
    cleaned = (text or "").strip()
    # Strip a leading ``` / ```json and a trailing ``` if present.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        first, last = cleaned.find("{"), cleaned.rfind("}")
        if first < 0 or last <= first:
            raise ValueError("The planner did not return a JSON object.")
        try:
            return json.loads(cleaned[first:last + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"The planner returned invalid JSON: {exc}") from exc


# parse_plan: raw planner text -> Plan. Called by Pipeline.plan().
# It is deliberately forgiving (missing keys get defaults) because models are not perfectly
# consistent, but it raises ValueError when nothing usable is there.
def parse_plan(text: str) -> Plan:
    """Parse the planner reply, tolerating fences, stray text, float scores and odd casing."""
    data = _extract_json_object(text)
    if not isinstance(data, dict):
        raise ValueError("The planner reply must be a JSON object.")

    # Helper: the value as a stripped string, or "" when missing / not a string.
    def as_text(key):
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    reply, request, title = as_text("reply"), as_text("request"), as_text("title")
    # "New Report" / "new-report" -> "new_report".
    intent = re.sub(r"[\s\-]+", "_", as_text("intent").lower())
    # Unknown intent: guess from what was filled in (a request means a report, a reply means chat).
    if intent not in INTENTS:
        if request:
            intent = "new_report"
        elif reply:
            intent = "chat"
        else:
            raise ValueError("The planner reply had no usable intent.")

    chart = normalize_chart_type(data.get("chart_type"))
    raw_assumptions = data.get("assumptions")
    # Keep at most 5 non-empty assumptions; ignore the field if it is not a list.
    assumptions =[str(a).strip() for a in raw_assumptions if str(a).strip()][:5] \
        if isinstance(raw_assumptions, list) else []

    # Readiness score: accept "score" (or the older name "confidence"), allow "85.5" or 85.5,
    # and clamp to 0..100. A non-numeric score is ignored (scored stays False).
    raw_score = data.get("score", data.get("confidence"))
    scored, confidence = False, 0
    if raw_score is not None:
        try:
            confidence, scored = max(0, min(100, int(float(raw_score)))), True
        except (TypeError, ValueError):
            pass

    # "sufficient" may arrive as true/false, "true"/"yes", or be missing. When missing, a
    # clarify intent counts as not sufficient and everything else as sufficient.
    raw_sufficient = data.get("sufficient")
    if isinstance(raw_sufficient, str):
        sufficient = raw_sufficient.strip().lower() in ("true", "yes", "1")
    elif raw_sufficient is None:
        sufficient = intent != "clarify"
    else:
        sufficient = bool(raw_sufficient)
    return Plan(intent=intent, reply=reply, request=request, title=title,
                chart_type=chart or "auto", assumptions=assumptions, confidence=confidence,
                scored=scored, sufficient=sufficient, clarification=as_text("clarification"))


# --------------------------------------------------------------------------- results
# Usage: running total of LLM tokens (cost tracking) over several calls in one turn.
# add() takes an LLMResponse from app/llm.py; cache_read_tokens is only reported by some
# providers, hence .get(..., 0).
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, response):
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.cache_read_tokens += response.extra.get("cache_read_tokens", 0)


# Result: everything about one SQL-generation attempt, returned by generate_sql() / ask().
# Note: request_id here is a short 8-character id for the JSONL request log only; it is NOT the
# same as session.request_id (the MySQL metadata row id created by chatbot_api.py).
#   status "ok": sql/columns/rows are filled; "clarify": the model asked a question;
#   "failed": guardrail/DB errors after all retries or access denied; "error": the LLM call
#   itself failed. truncated=True means the row cap (max_rows) cut the result.
@dataclass
class Result:
    request_id: str
    question: str
    status: str = "failed"  # ok | clarify | chat | failed | error
    intent: str = ""
    sql: str = ""
    assumptions: list = field(default_factory=list)
    clarification: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    attempts: int = 0
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_s: float = 0.0
    sql_runtime_ms: int = 0
    prompt_score: int = 0
    prompt_reason: str = ""


# Raised by Pipeline.plan(); ConversationEngine.handle() catches it and shows a friendly
# "couldn't reach my AI service" reply instead of crashing the request.
class PlannerError(RuntimeError):
    """The planner LLM call failed or kept returning unusable output."""


# Pipeline: created ONCE at startup (chatbot_api.create_app / run.py) and shared by all
# sessions, so it must not hold per-conversation state (that lives in session.py).
#   llm: an app/llm.py provider; db: an app/db.py database object; cfg: parsed config YAML;
#   base_dir: folder that relative paths in the config (context files, log file) start from.
class Pipeline:
    def __init__(self, llm, db, cfg, base_dir="."):
        self.llm, self.db = llm, db
        self.cfg = cfg
        db_cfg = cfg["db"]
        # The only tables the chatbot may ever query (before any per-role restriction).
        self.allowed = db_cfg["allowed_tables"]
        # Row cap: the guardrail adds or lowers the SQL LIMIT to this, and the DB layer reads at
        # most this many rows (+1 to detect truncation).
        self.max_rows = int(db_cfg.get("max_rows", 1000))
        pipeline_cfg = cfg.get("pipeline", {})
        # Extra tries after the first one when the SQL fails (2 -> up to 3 attempts in total).
        self.max_retries = int(pipeline_cfg.get("max_retries", 2))
        # JSONL request log (config pipeline.log_file, e.g. logs/requests_billing.jsonl); None = off.
        self.log_path =Path(base_dir, pipeline_cfg["log_file"]) if pipeline_cfg.get("log_file") else None

        context_cfg = cfg.get("context", {})
        # Data dictionary / business-rule files, read once at startup (restart to pick up edits).
        self._context_texts =[Path(base_dir, path).read_text(encoding="utf-8")
                               for path in context_cfg.get("files", [])]
        self._extra_rules = context_cfg.get("extra_rules", "")
        # Full schema text, introspected from the live database at startup (a DB query).
        self._schema_text = db.describe(self.allowed)
        # Discovered columns per table; role_permissions decides which of them a role may see.
        # getattr(..., None) because some database classes (e.g. test fakes) have no
        # table_columns(); then self.columns stays {} and schema_for() falls back to text lines.
        discover = getattr(db, "table_columns", None)
        self.columns = {t.lower(): list(cols) for t, cols in discover(self.allowed).items()} \
            if callable(discover) else {}
        # Everyday words for tables/columns (config), used only when the DB denies that resource.
        self.resource_terms = (cfg.get("access") or {}).get("resource_terms") or {}
        # Cache of built SQL system prompts {schema_text: prompt} and the day it was built for
        # (see sql_system()). Different roles see different schemas, so one entry per schema.
        self._sql_systems, self._sql_system_day = {}, None

    def schema_for(self, role=None, access=None) -> str:
        """Schema text for the prompts: only the tables and columns the role may read.
        Without an access controller (offline tools) the full schema is shown; the SQL
        guardrail still enforces permissions either way."""
        # Why filter at all? If the LLM never sees a restricted table it is unlikely to use it.
        # Security does NOT depend on this: validate_sql() still blocks it.
        if access is None:
            return self._schema_text
        # Lower-cased names of the tables this role may read (empty set for an unknown role).
        readable = tables_for_role(self.allowed, role, access)
        lines = []
        for table in self.allowed:
            t = table.lower()
            if t not in readable:
                continue
            if t in self.columns:
                # Keep only the columns the role may read; format "table(col type, col type)".
                cols =[(n, ty) for n, ty in self.columns[t] if access.can_access_column(role, t, n)]
                if cols:
                    lines.append(f"{table}(" + ", ".join(f"{n} {ty}" for n, ty in cols) + ")")
            else:
                # No column list known: reuse that table's line from the full schema text.
                lines += [l for l in self._schema_text.splitlines() if l.lower().startswith(f"{t}(")]
        return "\n".join(lines) or "(no tables are available to this user)"

    # denied_reference: an early, text-based access check. ConversationEngine calls it (through
    # its _denied_reference) on the user's message and on the planner's restatement BEFORE any
    # SQL is written. The real matching logic lives in AccessController.denied_reference
    # (app/access.py); here we only pass the table/column names and the everyday "terms".
    # *texts collects any number of strings to scan.
    def denied_reference(self, role, access, *texts):
        """Restricted table/column (per role_permissions) the texts ask for, else None."""
        return access.denied_reference(
            role, self.allowed, *texts, terms=self.resource_terms,
            columns={t: [n for n, _ in cols] for t, cols in self.columns.items()})

    # The prompts embed today's date. Rebuild when the day changes so a server that stays up
    # for days still resolves "last month" / "this year" correctly.
    # `system`: shortcut for the full-schema SQL prompt (no role filtering).
    @property
    def system(self):
        return self.sql_system()

    # sql_system: the system prompt for the SQL LLM, showing only the schema the role may read.
    # Built prompts are cached per schema text, and the whole cache is cleared when the date
    # changes (because the prompt contains "Today's date is ...").
    def sql_system(self, role=None, access=None):
        today = date.today()
        if self._sql_system_day != today:
            self._sql_systems, self._sql_system_day = {}, today
        schema_text = self.schema_for(role, access)
        if schema_text not in self._sql_systems:
            self._sql_systems[schema_text] = build_system_prompt(
                dialect=self.db.dialect, schema_text=schema_text,
                context_texts=self._context_texts, extra_rules=self._extra_rules,
                max_rows=self.max_rows, today=today)
        return self._sql_systems[schema_text]

    def restricted_note(self, role=None, access=None) -> str:
        """Planner-only note naming the data role_permissions denies to this role, so a request
        for it is restated by name (and refused by the backend) instead of called missing data."""
        # Why tell the planner about restricted data at all? Otherwise it would answer "that
        # data does not exist" (a decline). Instead it restates the request by name, the
        # denied_reference() check catches it, and the user gets the proper "not permitted" reply.
        if access is None:
            return ""
        columns = {t: [n for n, _ in cols] for t, cols in self.columns.items()}
        tables, denied_columns = access.denied_resources(role, self.allowed, columns)
        lines = [f"{t}(" + ", ".join(columns[t]) + ")" if columns.get(t) else t for t in sorted(tables)]
        lines += sorted(denied_columns)
        if not lines:
            return ""
        return ("\n\nRestricted for this user - this data EXISTS, but the system decides access:\n"
                + "\n".join(lines)
                + "\nIf the user asks for any of this data, in any wording, never decline it and never "
                  "say it does not exist: set intent \"new_report\" and name it in `request` exactly as "
                  "written above. Never mention it in `reply`, and never add it to a request that did "
                  "not ask for it.")

    # planner_system: the planner's system prompt = role-filtered schema + restricted-data note
    # + business rules + the conversation state text from ConversationEngine.state_block().
    # Not cached, because the state changes on every turn.
    def planner_system(self, state: str = "", role=None, access=None) -> str:
        return build_planner_prompt(
            dialect=self.db.dialect,
            schema_text=self.schema_for(role, access) + self.restricted_note(role, access),
            context_texts=self._context_texts, extra_rules=self._extra_rules, state=state)

    # ------------------------------------------------------------------ LLM call
    def _complete(self, stage: str, system: str, messages: list):
        """One timed LLM call. Full prompts and replies are logged at DEBUG only."""
        # stage: a label for the log ("planner", "sql#1", "sql#2", ...).
        # INFO lines (start/finish, timing, token counts) always reach logs/backend_billing.log;
        # the full prompt text is DEBUG because it is large and may contain business data.
        log.info("LLM %s call started (%s/%s, %d messages)", stage, self.llm.name, self.llm.model, len(messages))
        log.debug("LLM %s system prompt:\n%s", stage, system)
        log.debug("LLM %s messages: %s", stage, json.dumps(messages, ensure_ascii=False, default=str))
        # perf_counter() is a high-resolution timer meant for measuring durations.
        started = time.perf_counter()
        try:
            # The actual HTTP call to the LLM provider (app/llm.py). Can fail on network errors,
            # timeouts, bad API keys, rate limits...
            response = self.llm.complete(system, messages)
        # Log the failure with its traceback (log.exception) and re-raise: the caller decides
        # what to do (plan() wraps it in PlannerError, generate_sql() marks status "error").
        except Exception:
            log.exception("LLM %s call failed after %d ms", stage, (time.perf_counter() - started) * 1000)
            raise
        log.info("LLM %s call finished in %d ms (tokens in %s, out %s)", stage,
                 (time.perf_counter() - started) * 1000, response.input_tokens, response.output_tokens)
        log.debug("LLM %s reply:\n%s", stage, response.text)
        return response

    # ------------------------------------------------------------------ stage 1: plan
    def plan(self, messages: list, state: str = "", role=None, access=None):
        """Return (Plan, Usage). Retries once when the reply is not valid JSON."""
        # Called by ConversationEngine.handle() (and ask()).
        #   messages: recent chat history + the new user message ([{"role","content"}, ...]).
        #   state: the conversation-state text appended to the system prompt.
        system = self.planner_system(state, role, access)
        # list(messages) makes a copy so the caller's list is never modified by the retry below.
        usage, msgs, last_error = Usage(), list(messages), None
        for attempt in range(2):           # first try + one retry
            try:
                response = self._complete("planner", system, msgs)
            # A failed LLM call (network/provider) is NOT retried here; turn it into PlannerError.
            except Exception as exc:
                raise PlannerError(f"LLM call failed: {exc}") from exc
            usage.add(response)
            try:
                plan = parse_plan(response.text)
                log.info("Plan: intent=%s score=%s sufficient=%s chart=%s",
                         plan.intent, plan.confidence if plan.scored else "-", plan.sufficient, plan.chart_type)
                return plan, usage
            # Unparseable reply: show the model its own bad answer plus a correction request,
            # then loop around for one more try.
            except ValueError as exc:
                log.warning("Planner reply unusable (%s)%s", exc, "; retrying once" if attempt == 0 else "")
                last_error = exc
                msgs = msgs + [
                    {"role": "assistant", "content": response.text or "(empty reply)"},
                    {"role": "user", "content": "That was not valid. Reply with ONLY the JSON object "
                                                "described in the instructions."},
                ]
        log.error("Planner gave no usable reply after 2 tries: %s", last_error)
        raise PlannerError(str(last_error))

    # ------------------------------------------------------------------ stage 2: SQL
    def generate_sql(self, request_text: str, *, previous: dict | None = None,
                     assumptions: list | None = None, role: str | None = None,
                     access=None) -> Result:
        """Generate SQL for a request, applying role‑based table whitelist.
        The caller supplies the user's role (from the session). The method
        computes a role‑specific allowed‑tables set and validates the LLM‑generated
        SQL against it. Unauthorized tables trigger a GuardrailError which we
        translate into the standard denial message.
        """
        # Called by ConversationEngine._generate() (and ask()).
        #   request_text: the planner's self-contained request ("Monthly revenue for 2025").
        #   previous: the last report dict when the user is MODIFYING it (its SQL is shown to
        #             the model so it edits instead of starting over).
        #   assumptions: defaults the planner agreed with the user.
        # Flow per attempt: LLM -> parse -> validate_sql (guardrails) -> execute read-only.
        # On a fixable failure the error is sent back to the LLM and it tries again.
        started = time.perf_counter()
        result = Result(request_id=uuid.uuid4().hex[:8], question=request_text)
        # Compute role‑specific whitelist once.
        role_allowed = tables_for_role(self.allowed, role, access)
        # Build the single user message for the SQL model.
        content = f"Report request: {request_text}"
        if assumptions:
            content += "\nAgreed assumptions (stay consistent with them): " + "; ".join(assumptions)
        if previous and previous.get("sql"):
            content += (f"\n\nThe user is revising an earlier report.\nEarlier request: {previous.get('request', '')}\nEarlier SQL:\n```sql\n{previous['sql']}\n```\n"
                        "Update this query so it satisfies the new request.")
        messages = [{"role": "user", "content": content}]
        system = self.sql_system(role, access)
        log.info("SQL generation started (%s, role %s, revising=%s)",
                 result.request_id, role or "-", bool(previous and previous.get("sql")))

        # Attempts are numbered 1..max_retries+1. Every path either `break`s out of the loop
        # (success, clarify, access denied, LLM down) or falls through to the retry code at the
        # bottom, which appends the failure to `messages` for the next attempt.
        for attempt in range(1, self.max_retries + 2):
            result.attempts = attempt
            try:
                response = self._complete(f"sql#{attempt}", system, messages)
            # LLM unreachable: retrying immediately would likely fail again, so stop with "error".
            except Exception as exc:
                result.status, result.error = "error", f"LLM call failed: {exc}"
                break
            # Accumulate token usage over all attempts.
            result.input_tokens += response.input_tokens
            result.output_tokens += response.output_tokens
            result.cache_read_tokens += response.extra.get("cache_read_tokens", 0)
            parsed = parse_response(response.text)

            # The SQL model needs more info: stop and let ConversationEngine ask the user.
            if parsed.kind == "clarify":
                result.status, result.clarification, result.error = "clarify", parsed.clarification, ""
                break

            # `failure` = the error text to send back to the model if this attempt fails.
            failure = None
            if parsed.kind != "sql":
                failure = "Reply did not follow the output protocol (expected a SQL block or CLARIFY line)."
                result.error = failure
            else:
                # Safety check with sqlglot (app/guardrails.py): exactly one SELECT, only allowed
                # tables/columns for this role, no writes, row limit applied. Raises GuardrailError.
                try:
                    validated = validate_sql(parsed.sql, self.db.dialect, role_allowed, self.max_rows,
                                             role=role, access=access)
                except GuardrailError as exc:
                    result.sql, failure = parsed.sql, str(exc)
                    result.error = failure
                    # Permission problems are final (no retry: the model must not "work around"
                    # access rules). Other guardrail errors (syntax, forbidden statement) retry.
                    if "not authorized" in failure.lower():
                        # Unauthorized table / column – return canonical denial (status stays "failed").
                        log.warning("Generated SQL denied for role %s: %s", role or "-", failure)
                        log.debug("Denied SQL:\n%s", parsed.sql)
                        result.error = DENIED_ERROR
                        break
                # try/except/else: this `else` runs only when validate_sql raised nothing.
                else:
                    log.debug("Validated SQL (attempt %d):\n%s", attempt, validated.sql)
                    query_started = time.perf_counter()
                    # Run on MySQL via app/db.py (never committed; read-only safety comes from the
                    # guardrails + a read-only DB account). Only QueryError (bad column, SQL
                    # error, lost connection...) is caught -> its message goes back to the model.
                    try:
                        query_result = self.db.execute_readonly(validated.sql, self.max_rows)
                    except QueryError as exc:
                        result.sql_runtime_ms = round((time.perf_counter() - query_started) * 1000)
                        result.sql, failure = validated.sql, str(exc)
                        result.error = failure
                    else:
                        # Success: copy the data into the Result and leave the retry loop.
                        result.sql_runtime_ms = round((time.perf_counter() - query_started) * 1000)
                        result.status, result.error = "ok", ""
                        result.sql, result.assumptions = validated.sql, parsed.assumptions
                        result.columns, result.rows = query_result.columns, query_result.rows
                        result.truncated = query_result.truncated
                        log.info("SQL ran in %d ms: %d rows%s, %d columns", result.sql_runtime_ms,
                                 len(result.rows), " (truncated)" if result.truncated else "",
                                 len(result.columns))
                        break
            # Reaching here means this attempt failed in a retryable way. Feed the model its own
            # reply plus the error ("self-correction"), then loop. After the last attempt the loop
            # simply ends and result.status stays "failed" (the dataclass default).
            log.warning("SQL attempt %d/%d failed: %s", attempt, self.max_retries + 1, failure)
            if result.sql:
                log.debug("Failed SQL (attempt %d):\n%s", attempt, result.sql)
            messages += [
                {"role": "assistant", "content": response.text or "(empty reply)"},
                {"role": "user", "content": f"That failed: {failure}\nReturn a corrected reply following the same output protocol."},
            ]

        result.latency_s = round(time.perf_counter() - started, 2)
        if result.status == "clarify":
            log.info("SQL model asked for clarification instead of writing SQL")
        # Success/clarify are logged at INFO, failures at WARNING (easier to spot in the log).
        log_fn = log.info if result.status in ("ok", "clarify") else log.warning
        log_fn("SQL generation finished: status=%s attempts=%d in %.2f s%s", result.status,
               result.attempts, result.latency_s, f" error={result.error}" if result.error else "")
        self._log(result)   # one JSON line in the request log file (if configured)
        return result

    # ------------------------------------------------------------------ CLI convenience
    def ask(self, question: str, *, role: str | None = None, access=None) -> Result:
        """Single-shot use (run.py): plan, then build SQL when the plan is a report.
        This method now checks permissions *before* any LLM call. If the request
        contains a table or column that the caller's role is not allowed to read,
        it returns an immediate denial without invoking the planner or generating
        any intermediate AI response.
        """
        # Not used by the chatbot API (that goes through ConversationEngine); this is a simpler
        # one-question path with no memory, for the command-line tool run.py.
        # Early RBAC check: restricted tables/columns named in the request (backend rules).
        if access is not None and self.denied_reference(role, access, question):
            return Result(request_id=uuid.uuid4().hex[:8], question=question, status="failed",
                          error=DENIED_ERROR)
        started = time.perf_counter()
        try:
            plan, usage = self.plan([
                {"role": "user", "content": question}],
                "LAST REPORT: none yet. Nothing to explain, modify or re-chart.",
                role=role, access=access)
        except PlannerError as exc:
            return Result(
                request_id=uuid.uuid4().hex[:8],
                question=question,
                status="error",
                error=f"Prompt evaluation failed: {exc}")
        # Report intent -> write and run SQL; anything else -> return the planner's reply as-is.
        if plan.intent in REPORT_INTENTS:
            result = self.generate_sql(
                plan.request or question,
                assumptions=plan.assumptions,
                role=role,
                access=access,
            )
            result.question = question
        else:
            result = Result(
                request_id=uuid.uuid4().hex[:8],
                question=question,
                status="clarify" if plan.intent == "clarify" else "chat",
                clarification=plan.reply,
            )
        result.intent = plan.intent
        result.prompt_score = plan.confidence
        # Add the planner's tokens so the result shows the cost of the whole question.
        result.input_tokens += usage.input_tokens
        result.output_tokens += usage.output_tokens
        result.cache_read_tokens += usage.cache_read_tokens
        result.latency_s = round(time.perf_counter() - started, 2)
        return result

    # ------------------------------------------------------------------ logging
    # This is a SEPARATE log from logs/backend_billing.log: a machine-readable "JSON Lines" file
    # (one JSON object per line, path from config pipeline.log_file, e.g.
    # logs/requests_billing.jsonl) that records every question, its status, SQL, timing and
    # token counts - handy for analysis in a spreadsheet or notebook.
    def _append_log(self, entry: dict):
        if not self.log_path:
            return
        # Create the logs/ folder on first use.
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # "a" = append mode; default=str makes dates/Decimals JSON-serialisable.
        with open(self.log_path, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, default=str) + "\n")

    # Fields shared by every log line: UTC timestamp, the OS account running the backend
    # (getpass.getuser(), not the chat user), and which LLM provider/model was used.
    def _base_entry(self) -> dict:
        return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "user": getpass.getuser(), "provider": self.llm.name, "model": self.llm.model}

    # _log: one log line for a SQL-generation Result (row COUNT only, never the row data).
    # `**dict` unpacks the base fields into the new dict.
    def _log(self, result: Result):
        self._append_log({
            **self._base_entry(), "request_id": result.request_id, "question": result.question,
            "status": result.status, "sql": result.sql, "assumptions": result.assumptions,
            "attempts": result.attempts, "rows": len(result.rows), "latency_s": result.latency_s,
            "sql_runtime_ms": result.sql_runtime_ms, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "cache_read_tokens": result.cache_read_tokens,
            "prompt_score": result.prompt_score, "prompt_reason": result.prompt_reason,
            "error": result.error})

    def log_turn(self, **fields):
        """Log a conversational turn that did not need SQL (chat, explain, clarify, ...)."""
        # Called from chatbot_api.py with keyword arguments (question=..., status=..., ...).
        self._append_log({**self._base_entry(), **fields})
