"""Per-conversation state: what the UI shows, what the LLM remembers, and the last report.

Everything the chatbot needs to survive a Power BI reload lives here (server side), keyed by a
session id that the visual stores and sends back. Nothing in this module touches the network.
"""
# ---------------------------------------------------------------------------------------------
# WHAT THIS FILE DOES (beginner overview)
#   Holds the in-memory state of every chat conversation ("session").
#   - ChatSession: one conversation. It stores the messages shown in the Power BI visual
#     (transcript), the short memory sent to the LLM (history), the last report built, and
#     flags that track where the conversation is (waiting for a clarification answer, waiting
#     for a chart choice, a Power BI reload running, End clicked, ...).
#   - SessionStore: a thread-safe dictionary {session_id -> ChatSession} with expiry rules.
# WHO USES IT
#   chatbot_api.py creates one SessionStore at startup and calls get_or_create()/get()/latest()
#   for every HTTP request. app/conversation.py (ConversationEngine) reads and updates the
#   session fields while it plans a turn.
# WHAT IT CALLS
#   Only the standard library plus app/clock.py (utc_now). Sessions live in RAM only: if the
#   Flask backend restarts, all sessions are gone (the MySQL tables keep the permanent records).
# ---------------------------------------------------------------------------------------------
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .clock import utc_now

# Logger named "app.session". Lines go to the console and to logs/backend_billing.log
# (configured once at startup by app/logging_setup.py from the `logging:` config section).
log = logging.getLogger(__name__)

# Size limits so one long conversation cannot use unlimited memory (or LLM tokens).
MAX_HISTORY_MESSAGES = 40       # LLM memory kept per session (user + assistant messages)
MAX_TRANSCRIPT_MESSAGES = 400   # what the UI can restore
MAX_REPORTS_REMEMBERED = 10    # (not referenced in this file; chatbot_api.py trims reports itself)


# ChatSession = everything the backend knows about ONE conversation.
# @dataclass auto-generates __init__ from the annotated fields below, so a session is created
# with just ChatSession(session_id="abc123") and every other field takes its default.
# field(default_factory=list) is needed for mutable defaults: each session gets its OWN new list
# (a plain `= []` would be shared by every instance, a classic Python pitfall).
@dataclass
class ChatSession:
    # Short id that the Power BI visual stores and sends back with every request.
    session_id: str
    created: float = field(default_factory=time.time)
    # Last activity time (epoch seconds). SessionStore uses it to expire idle sessions.
    updated: float = field(default_factory=time.time)
    transcript: list = field(default_factory=list)     # UI messages (with ids)
    history: list = field(default_factory=list)        # LLM messages [{"role","content"}]
    last_report: dict | None = None                    # full data of the latest report
    reports: list = field(default_factory=list)        # short summaries of every report built
    # Who is asking. Filled ONLY from a trusted identity source (see app/identity.py), else None.
    user_id: str | None = None
    # Role such as "CEO" / "Analyst" / "Developer"; decides which tables and columns may be read
    # (app/access.py). Set by chatbot_api._claim_session(), never from the message body.
    user_role: str | None = None
    # True when the role came from the demo role picker in the visual (demo mode).
    demo_role_selected: bool = False
    # Where the user chats from (e.g. Power BI); stored in the request metadata row.
    platform: str | None = None
    # Session lifecycle. An ended session keeps its history but accepts no more messages.
    ended: bool = False
    ended_at: float | None = None
    end_reason: str | None = None
    # end_requested: chatbot_api.end_session() sets it first, then tries the session lock. If a
    # turn is still running (lock busy), _finish_turn() sees the flag afterwards and ends it,
    # so a half-built report and its database rows are never cut off midway.
    end_requested: bool = False        # End clicked while a request was running: ends right after it
    # Function returning "now" as a datetime. chatbot_api.py replaces it with its own clock;
    # tests can inject a fake clock to get predictable timestamps. repr=False hides it in prints.
    clock: Callable[[], datetime] = field(default=utc_now, repr=False)
    thread_started: datetime | None = None   # when the current report request started (UTC)
    turn_started: float | None = None        # perf_counter() at the arrival of the current turn
    # "request thread": one report request including its clarification questions
    # True after the bot asked a clarifying question: the next user message is treated as the
    # answer and merged into the same request instead of starting a new one.
    awaiting_clarification: bool = False
    # How many clarifying questions were asked for the current request (capped by
    # pipeline.max_clarifications in config, see ConversationEngine).
    clarify_count: int = 0
    # The user's messages that belong to the current request (first message + answers).
    thread_messages: list = field(default_factory=list)
    # Id of the request's row in the MySQL metadata table. None until chatbot_api._ensure_row()
    # creates the row; reset to None when the request thread ends. Also written into every
    # log line as rid=... (see logging_setup.bind_request).
    request_id: str | None = None
    # Processing time (ms) banked from earlier turns of the same request (excludes the time the
    # user spent typing answers). chatbot_api adds the current turn on top for response_time_ms.
    runtime_ms: int = 0
    # A ready request waiting for the user's chart choice (asked after all clarifications).
    # Holds intent, request, title, assumptions, previous report, confidence and reply until the
    # chart answer arrives; then ConversationEngine generates the SQL exactly once.
    pending_chart: dict | None = None
    # busy: set True by chatbot_api._schedule_reload() and back to False by the background thread
    # when the PowerShell reload of Power BI Desktop ends. The visual sees it in every response.
    busy: bool = False                                  # Power BI reload running in background
    # progress: a human-readable status line. The visual polls GET /session/<id>/progress while
    # waiting for a reply and shows this text; chatbot_api resets it to None when the turn ends.
    progress: str | None = None                         # what the current turn is doing right now
    # Per-session lock. chatbot_api holds it for a whole chat turn so two messages of the same
    # session are never processed at once. RLock (re-entrant) lets the same thread acquire it
    # again without deadlocking itself (e.g. a helper that also takes the lock).
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    # Counter used to give each transcript message a unique, increasing id.
    _next_id: int = 1

    # Read-only alias used by app/audit.py when it writes the conversation audit rows.
    @property
    def requested_by(self) -> str | None:
        """Kept for the audit log: the VERIFIED user id (None when identity is unknown)."""
        return self.user_id

    # ---------------------------------------------------------------- transcript
    # add_message: append one chat bubble to the transcript (what the visual displays).
    #   role: "user" or "assistant"; kind: "text", "status", "error", ...; meta: extra UI data.
    #   Returns the stored entry (dict) so the caller can send it to the visual / audit log.
    #   Called from chatbot_api.py for every user message, reply and status note.
    def add_message(self, role: str, text: str, kind: str = "text", meta: dict | None = None) -> dict:
        entry = {"id": self._next_id, "role": role, "text": text, "kind": kind,
                 "ts": time.time(), "meta": meta or {}}
        self._next_id += 1
        self.transcript.append(entry)
        # Keep only the newest MAX_TRANSCRIPT_MESSAGES entries (drop the oldest from the front).
        if len(self.transcript) > MAX_TRANSCRIPT_MESSAGES:
            del self.transcript[: len(self.transcript) - MAX_TRANSCRIPT_MESSAGES]
        self.updated = time.time()
        return entry

    # messages_after: the transcript entries newer than after_id. The visual remembers the last
    # id it has shown and asks only for newer ones (used for chat replies, /transcript and
    # history restore after a Power BI reload).
    def messages_after(self, after_id: int = 0) -> list:
        return [m for m in self.transcript if m["id"] > after_id]

    # Id of the newest message so far (0 when the transcript is empty).
    @property
    def last_id(self) -> int:
        return self._next_id - 1

    # ---------------------------------------------------------------- LLM memory
    # remember: store one user/assistant exchange in the LLM memory (history). Called by
    # chatbot_api.py after every finished turn. LLM chat APIs expect user/assistant to
    # alternate, which is why both are always appended together.
    def remember(self, user_text: str, assistant_text: str) -> None:
        """Record one exchange. Always appends BOTH roles so roles keep alternating."""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": assistant_text or "(no reply)"})
        if len(self.history) > MAX_HISTORY_MESSAGES:
            del self.history[: len(self.history) - MAX_HISTORY_MESSAGES]

    # llm_history: the slice of memory sent to the planner LLM (ConversationEngine.handle).
    #   max_chars: total character budget (keeps the prompt small = cheaper, faster).
    #   per_message: any single message longer than this is cut and ends with " ...".
    #   Returns a list of {"role", "content"} dicts, oldest first.
    def llm_history(self, max_chars: int = 3500, per_message: int = 700) -> list:
        """Most recent messages that fit the budget, starting on a user message."""
        picked, used = [], 0
        # Walk backwards from the newest message, stop when the budget is used up
        # (but always keep at least one message: `and picked`).
        for message in reversed(self.history):
            content = message["content"]
            if len(content) > per_message:
                content = content[:per_message].rstrip() + " ..."
            if used + len(content) > max_chars and picked:
                break
            picked.append({"role": message["role"], "content": content})
            used += len(content)
        picked.reverse()   # back to chronological order
        # The LLM conversation must start with a user message, so drop leading assistant ones.
        while picked and picked[0]["role"] != "user":
            picked.pop(0)
        return picked

    # ---------------------------------------------------------------- request thread
    # A "request thread" = one report request from the first message, through any clarifying
    # questions and the chart question, until the report is built (or fails).
    # start_thread: begin a new request (called by ConversationEngine.handle). It clears the
    # counters and records when the request started (used later for request_ts / day_id).
    def start_thread(self, first_message: str) -> None:
        self.thread_messages = [first_message]
        self.clarify_count = 0
        self.request_id = None
        self.runtime_ms = 0
        self.thread_started = self.clock()

    # end_thread: forget the current request (after a report is finished, fails, or the session
    # ends). Called from chatbot_api.py. Note: it does NOT clear last_report or history.
    def end_thread(self) -> None:
        self.thread_messages = []
        self.clarify_count = 0
        self.awaiting_clarification = False
        self.pending_chart = None
        self.request_id = None
        self.runtime_ms = 0
        self.thread_started = None


# SessionStore: the registry of all live sessions, created once by chatbot_api.py (ChatService).
# Flask serves requests on several threads, so every access goes through self._lock to stop two
# threads from changing the dictionary at the same time.
#   ttl_seconds: a session idle longer than this (default 12 h) is dropped.
#   max_sessions: hard cap; above it the least recently used sessions are evicted.
class SessionStore:
    def __init__(self, ttl_seconds: int = 12 * 3600, max_sessions: int = 200):
        self._sessions: dict[str, ChatSession] = {}
        # Plain Lock (not RLock): it is only taken in this class and never re-entered.
        self._lock = threading.Lock()
        self.ttl_seconds, self.max_sessions = ttl_seconds, max_sessions

    # _purge: housekeeping run at the start of every get/get_or_create/latest call (no
    # background timer needed). Caller must already hold self._lock.
    def _purge(self) -> None:
        now = time.time()
        # 1) Expire sessions idle longer than the TTL.
        expired = [s for s, v in self._sessions.items() if now - v.updated > self.ttl_seconds]
        for sid in expired:
            del self._sessions[sid]
        if expired:
            log.info("Dropped %d idle session(s) from memory: %s", len(expired), ", ".join(expired))
        # 2) Still too many? Evict the least recently updated ones (logged as a WARNING).
        if len(self._sessions) > self.max_sessions:
            oldest = sorted(self._sessions.values(), key=lambda v: v.updated)
            evicted = oldest[: len(self._sessions) - self.max_sessions]
            for session in evicted:
                del self._sessions[session.session_id]
            log.warning("Session limit %d reached: evicted %d oldest session(s)",
                        self.max_sessions, len(evicted))

    # get: look up an existing session; None if unknown or expired (never creates one).
    # Used by endpoints that must not invent sessions (end, progress, transcript, history).
    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            self._purge()
            return self._sessions.get(session_id)

    # get_or_create: used by the /chat endpoint. Returns the session the visual named, or a new
    # one. If the visual sent an id the server does not know (e.g. after a backend restart), a
    # fresh session is created under that SAME id so the visual can keep using it.
    def get_or_create(self, session_id: str | None) -> ChatSession:
        with self._lock:
            self._purge()
            # No id from the visual: invent a random 12-hex-character id.
            sid = session_id or uuid.uuid4().hex[:12]
            session = self._sessions.get(sid)
            if session is None:
                session = self._sessions[sid] = ChatSession(session_id=sid)
                log.info("Session %s created (%s)", sid,
                         "id sent by the visual" if session_id else "new id")
            return session

    # latest: the most recently active, not-ended session that has messages. chatbot_api's
    # /session/latest uses it so a reloaded visual (which may have lost its stored id) can
    # resume its conversation. Optional filters: same user_role, and active within
    # max_age_seconds (config chat.resume_window_minutes). Returns None if nothing matches.
    def latest(self, max_age_seconds: float | None = None,
               user_role: str | None = None) -> ChatSession | None:
        with self._lock:
            self._purge()
            sessions = [s for s in self._sessions.values() if s.transcript and not s.ended]
            if user_role is not None:
                sessions = [s for s in sessions if s.user_role == user_role]
            if max_age_seconds is not None:
                cutoff = time.time() - max_age_seconds
                sessions = [s for s in sessions if s.updated >= cutoff]
            return max(sessions, key=lambda s: s.updated, default=None)

    # delete: remove a session from memory; True if it existed. (At the time of writing no
    # caller in chatbot_api.py or app/ uses it: ended sessions are kept, flagged `ended`.)
    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None
