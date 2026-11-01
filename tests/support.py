"""Shared helpers for the session / feedback / metadata / naming / identity tests."""
import json
import threading
from datetime import datetime, timezone

from app.audit import ConversationRecorder
from app.identity import IdentityResolver
from app.pipeline import Pipeline
from chatbot_api import ChatService
from tests.fakes import FakeDB, FakePowerBI, FakeStore, ScriptedLLM, plan_json

SQL = "```sql\nSELECT month_start, revenue_usd FROM charges\n```"
REPORT = plan_json("new_report", reply="Monthly gross revenue.", request="Monthly gross revenue",
                   title="Monthly Revenue", confidence=95, sufficient=True)
REFUNDS = plan_json("new_report", reply="Refunds by reason.", request="Refunds by reason",
                    title="Refunds by Reason", confidence=95, sufficient=True)
FIXED_NOW = datetime(2026, 10, 26, 20, 0, 0, tzinfo=timezone.utc)      # 01:30 on 27 Oct in India


class GatedLLM(ScriptedLLM):
    """Blocks inside the Nth call until released, to simulate a request that is still running."""
    def __init__(self, replies, block_on_call):
        super().__init__(replies)
        self.block_on_call, self.entered, self.release = block_on_call, threading.Event(), threading.Event()

    def complete(self, system, messages):
        if len(self.calls) + 1 == self.block_on_call:
            self.entered.set()
            self.release.wait(5)
        return super().complete(system, messages)


class SlowLLM(ScriptedLLM):
    def __init__(self, replies, delay):
        super().__init__(replies)
        self.delay = delay

    def complete(self, system, messages):
        import time
        time.sleep(self.delay)
        return super().complete(system, messages)


def make(replies, *, ask_chart=False, db=None, powerbi=None, store=None, llm=None, clock=None,
         tz="UTC", identity_cfg=None, environ=None, background=None, end_wait=0.05, chat_cfg=None):
    cfg = {"db": {"allowed_tables": ["charges"], "max_rows": 1000}, "context": {"files": []},
           "pipeline": {"max_retries": 1, "max_clarifications": 2},
           "chat": {"ask_chart_type": ask_chart, "end_wait_seconds": end_wait, **(chat_cfg or {})},
           "app": {"timezone": tz, "platform": "Power BI"}}
    if identity_cfg:
        cfg["identity"] = identity_cfg
    store = store or FakeStore()
    powerbi = powerbi or FakePowerBI()
    llm = llm or ScriptedLLM(replies)
    pipeline = Pipeline(llm, db or FakeDB(), json.loads(json.dumps(cfg)), base_dir=".")
    service = ChatService(cfg, pipeline, store, powerbi, background=background,
                          recorder=ConversationRecorder(store, None),
                          identity=IdentityResolver(cfg, environ=environ or {}), clock=clock)
    return service, llm, store, powerbi


def chat(service, text, sid=None, identity=None, **extra):
    return service.handle_chat({"message": text, "session_id": sid, **extra}, identity)


def report(service, text="monthly revenue", sid=None):
    """Run one message that is expected to build a report; returns (payload, session_id)."""
    payload, status = chat(service, text, sid)
    assert status == 200 and payload["status"] == "ok", payload
    return payload, payload["session_id"]
