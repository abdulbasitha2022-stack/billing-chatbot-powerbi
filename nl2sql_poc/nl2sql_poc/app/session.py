"""Per-conversation state: what the UI shows, what the LLM remembers, and the last report.

Everything the chatbot needs to survive a Power BI reload lives here (server side), keyed by a
session id that the visual stores and sends back. Nothing in this module touches the network.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .clock import utc_now

MAX_HISTORY_MESSAGES = 40       # LLM memory kept per session (user + assistant messages)
MAX_TRANSCRIPT_MESSAGES = 400   # what the UI can restore
MAX_REPORTS_REMEMBERED = 10


@dataclass
class ChatSession:
    session_id: str
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    transcript: list = field(default_factory=list)     # UI messages (with ids)
    history: list = field(default_factory=list)        # LLM messages [{"role","content"}]
    last_report: dict | None = None                    # full data of the latest report
    reports: list = field(default_factory=list)        # short summaries of every report built
    # Who is asking. Filled ONLY from a trusted identity source (see app/identity.py), else None.
    user_id: str | None = None
    user_role: str | None = None
    demo_role_selected: bool = False
    platform: str | None = None
    # Session lifecycle. An ended session keeps its history but accepts no more messages.
    ended: bool = False
    ended_at: float | None = None
    end_reason: str | None = None
    end_requested: bool = False        # End clicked while a request was running: ends right after it
    clock: Callable[[], datetime] = field(default=utc_now, repr=False)
    thread_started: datetime | None = None   # when the current report request started (UTC)
    turn_started: float | None = None        # perf_counter() at the arrival of the current turn
    # "request thread": one report request including its clarification questions
    awaiting_clarification: bool = False
    clarify_count: int = 0
    thread_messages: list = field(default_factory=list)
    request_id: str | None = None
    runtime_ms: int = 0
    # A ready request waiting for the user's chart choice (asked after all clarifications).
    pending_chart: dict | None = None
    busy: bool = False                                  # Power BI reload running in background
    progress: str | None = None                         # what the current turn is doing right now
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _next_id: int = 1

    @property
    def requested_by(self) -> str | None:
        """Kept for the audit log: the VERIFIED user id (None when identity is unknown)."""
        return self.user_id

    # ---------------------------------------------------------------- transcript
    def add_message(self, role: str, text: str, kind: str = "text", meta: dict | None = None) -> dict:
        entry = {"id": self._next_id, "role": role, "text": text, "kind": kind,
                 "ts": time.time(), "meta": meta or {}}
        self._next_id += 1
        self.transcript.append(entry)
        if len(self.transcript) > MAX_TRANSCRIPT_MESSAGES:
            del self.transcript[: len(self.transcript) - MAX_TRANSCRIPT_MESSAGES]
        self.updated = time.time()
        return entry

    def messages_after(self, after_id: int = 0) -> list:
        return [m for m in self.transcript if m["id"] > after_id]

    @property
    def last_id(self) -> int:
        return self._next_id - 1

    # ---------------------------------------------------------------- LLM memory
    def remember(self, user_text: str, assistant_text: str) -> None:
        """Record one exchange. Always appends BOTH roles so roles keep alternating."""
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": assistant_text or "(no reply)"})
        if len(self.history) > MAX_HISTORY_MESSAGES:
            del self.history[: len(self.history) - MAX_HISTORY_MESSAGES]

    def llm_history(self, max_chars: int = 3500, per_message: int = 700) -> list:
        """Most recent messages that fit the budget, starting on a user message."""
        picked, used = [], 0
        for message in reversed(self.history):
            content = message["content"]
            if len(content) > per_message:
                content = content[:per_message].rstrip() + " ..."
            if used + len(content) > max_chars and picked:
                break
            picked.append({"role": message["role"], "content": content})
            used += len(content)
        picked.reverse()
        while picked and picked[0]["role"] != "user":
            picked.pop(0)
        return picked

    # ---------------------------------------------------------------- request thread
    def start_thread(self, first_message: str) -> None:
        self.thread_messages = [first_message]
        self.clarify_count = 0
        self.request_id = None
        self.runtime_ms = 0
        self.thread_started = self.clock()

    def end_thread(self) -> None:
        self.thread_messages = []
        self.clarify_count = 0
        self.awaiting_clarification = False
        self.pending_chart = None
        self.request_id = None
        self.runtime_ms = 0
        self.thread_started = None


class SessionStore:
    def __init__(self, ttl_seconds: int = 12 * 3600, max_sessions: int = 200):
        self._sessions: dict[str, ChatSession] = {}
        self._lock = threading.Lock()
        self.ttl_seconds, self.max_sessions = ttl_seconds, max_sessions

    def _purge(self) -> None:
        now = time.time()
        for sid in [s for s, v in self._sessions.items() if now - v.updated > self.ttl_seconds]:
            del self._sessions[sid]
        if len(self._sessions) > self.max_sessions:
            oldest = sorted(self._sessions.values(), key=lambda v: v.updated)
            for session in oldest[: len(self._sessions) - self.max_sessions]:
                del self._sessions[session.session_id]

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            self._purge()
            return self._sessions.get(session_id)

    def get_or_create(self, session_id: str | None) -> ChatSession:
        with self._lock:
            self._purge()
            sid = session_id or uuid.uuid4().hex[:12]
            session = self._sessions.get(sid)
            if session is None:
                session = self._sessions[sid] = ChatSession(session_id=sid)
            return session

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

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None
