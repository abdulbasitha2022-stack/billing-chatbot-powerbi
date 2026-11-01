"""Permanent record of the conversation: every question asked and every answer given.

Two copies are kept so nothing is lost:
  * a JSON-lines file (logs/conversations_billing.jsonl) - always works, no setup;
  * the MySQL table chat_conversation_log - easy to query (create it with chatbot_schema.sql).
A failure of either copy never breaks the chat.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .util import json_safe

log = logging.getLogger("chatbot.audit")


class ConversationRecorder:
    def __init__(self, store, jsonl_path=None):
        self.store = store
        self.path = Path(jsonl_path) if jsonl_path else None
        self._lock = threading.Lock()
        self._db_ok = True

    # ------------------------------------------------------------------ writing
    def record(self, session, entries: list, request_id=None, intent=None) -> None:
        """Store chat entries (dicts from ChatSession.add_message) for this session."""
        rows = []
        for entry in entries:
            meta = entry.get("meta") or {}
            rows.append({
                "session_id": session.session_id, "seq": entry["id"],
                "request_id": request_id, "role": entry["role"], "kind": entry.get("kind", "text"),
                "intent": intent, "text": entry["text"],
                "view_name": meta.get("view_name"), "page_name": meta.get("page_name"),
                "chart_type": meta.get("chart_type"), "requested_by": session.requested_by,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        if not rows:
            return
        if self.path:
            try:
                with self._lock:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.path, "a", encoding="utf-8") as handle:
                        for row in rows:
                            handle.write(json.dumps(json_safe(row), ensure_ascii=False) + "\n")
            except Exception:
                log.exception("Could not write the conversation log file")
        if self._db_ok:
            try:
                self.store.log_messages(rows)
            except Exception as exc:
                self._db_ok = False      # table missing / no permission: stop retrying until restart
                log.warning("Conversation log table not available (%s). Run the chat_conversation_log "
                            "section of chatbot_schema.sql. The JSONL file is still being written.", exc)

    # ------------------------------------------------------------------ reading
    def _file_rows(self) -> list:
        if not self.path or not self.path.is_file():
            return []
        rows = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    def read_session(self, session_id: str) -> list:
        try:
            rows = self.store.fetch_conversation(session_id)
            if rows:
                return json_safe(rows)
        except Exception:
            pass
        return sorted((r for r in self._file_rows() if r["session_id"] == session_id),
                      key=lambda r: r["seq"])

    def read_request(self, request_id: str) -> list:
        try:
            rows = self.store.fetch_request_conversation(request_id)
            if rows:
                return json_safe(rows)
        except Exception:
            pass
        return sorted((r for r in self._file_rows() if r.get("request_id") == request_id),
                      key=lambda r: (r["session_id"], r["seq"]))

    def list_sessions(self, limit: int = 50) -> list:
        try:
            rows = self.store.list_conversations(limit)
            if rows:
                return json_safe(rows)
        except Exception:
            pass
        grouped: dict = {}
        for row in self._file_rows():
            g = grouped.setdefault(row["session_id"], {"session_id": row["session_id"],
                                                       "started": row["ts"], "messages": 0,
                                                       "questions": 0, "reports": set()})
            g["messages"] += 1
            g["questions"] += row["role"] == "user"
            g["last_activity"] = row["ts"]
            if row.get("view_name"):
                g["reports"].add(row["view_name"])
        out = [{**g, "reports": len(g["reports"])} for g in grouped.values()]
        return sorted(out, key=lambda g: g["last_activity"], reverse=True)[:limit]


def format_conversation_text(session_id: str, rows: list) -> str:
    """Readable transcript for a browser tab or a support ticket."""
    if not rows:
        return f"No conversation recorded for {session_id}.\n"
    lines = [f"Conversation {session_id}"
             + (f"  (asked by: {rows[0]['requested_by']})" if rows[0].get("requested_by") else ""), ""]
    for row in rows:
        when = str(row.get("created_at") or row.get("ts") or "")[:19].replace("T", " ")
        who = "USER" if row["role"] == "user" else f"ASSISTANT ({row.get('kind') or 'text'})"
        lines.append(f"[{when}] {who}: {row['text']}")
        extras = [f"{label}: {row[key]}" for key, label in
                  (("view_name", "view"), ("page_name", "page"), ("chart_type", "chart"),
                   ("request_id", "request")) if row.get(key) and row["role"] != "user"]
        if extras:
            lines.append("    -> " + " | ".join(extras))
        lines.append("")
    return "\n".join(lines)
