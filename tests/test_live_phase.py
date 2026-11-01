"""BK-041 G4: a page that opens a chat mid-run learns when the run started
and what it is doing — for chat runs from the ChatRun (live.LivePhase, via
/messages `live` and /live), for cron/watch runs from gw.bg_live (fed by the
orchestrator's run events), surfaced as /messages `bg_live`."""

from __future__ import annotations

import time

import httpx

from harness.core.session import Session
from harness.gateway.app import Gateway, create_app
from harness.gateway.live import BgLive, LivePhase
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore


def test_phase_is_the_last_row_setting_event_slimmed():
    p = LivePhase()
    assert p.view()["phase"] is None
    p.observe({"type": "tool_call", "tool": "web_search", "args": {"q": "secret"}, "seq": 3})
    assert p.view()["phase"] == {"type": "tool_call", "tool": "web_search"}
    # call_tool keeps ONLY the wrapped name (the label needs it), no args.
    p.observe({"type": "tool_call", "tool": "call_tool",
               "args": {"name": "calendar_list", "arguments": {"q": "x"}}})
    assert p.view()["phase"] == {"type": "tool_call", "tool": "call_tool",
                                 "args": {"name": "calendar_list"}}
    p.observe({"type": "status", "phase": "reading", "pct": 40})
    assert p.view()["phase"] == {"type": "status", "phase": "reading", "pct": 40}
    # Bookkeeping never moves the phase; text never rides it.
    p.observe({"type": "turn_complete"})
    p.observe({"type": "delta", "text": "an untrusted answer"})
    assert p.view()["phase"] == {"type": "delta"}
    assert abs(p.view()["started_ms"] - time.time() * 1000) < 5000


def test_bg_live_tracks_one_run_per_session_until_run_done():
    bg = BgLive()
    bg.observe({"type": "tool_call", "session_id": "s1", "tool": "x"})  # no run yet
    assert bg.view("s1") is None
    bg.observe({"type": "run_start", "session_id": "s1", "title": "t", "trigger": "cron:1"})
    assert bg.view("s1")["phase"] is None
    bg.observe({"type": "tool_start", "session_id": "s1", "tool": "web_search"})
    assert bg.view("s1")["phase"] == {"type": "tool_start", "tool": "web_search"}
    assert bg.view("s2") is None
    bg.observe({"type": "run_done", "session_id": "s1", "text": ""})
    assert bg.view("s1") is None


async def test_messages_carries_bg_live_for_a_running_cron_session(db):
    sessions = SessionStore(db)
    gw = Gateway(sessions=sessions, runs=RunStore(db), approval_store=None, waiter=None,
                 triggers=None, bus=None, push=None, agent_factory=None)
    sid = (await Session.create(sessions, channel="web")).id
    gw.bg_live.observe({"type": "run_start", "session_id": sid})
    gw.bg_live.observe({"type": "tool_call", "session_id": sid, "tool": "web_search"})
    transport = httpx.ASGITransport(app=create_app(gw))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        body = (await c.get(f"/api/sessions/{sid}/messages")).json()
    assert body["live"] is None  # no chat run: a bg run has no ring
    assert body["bg_live"]["phase"] == {"type": "tool_call", "tool": "web_search"}
    assert body["bg_live"]["started_ms"] <= time.time() * 1000
