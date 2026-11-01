"""Stop while a tool never returns (2026-11-01: a list_files on ~/Pictures sat
on a macOS privacy prompt for hours, and the chat's Stop did nothing — the
deferred Stop waited for the tool to finish). Stop must end the turn anyway,
with the step still persisted paired and audited."""

from __future__ import annotations

import asyncio

import pytest

from harness.core import tool_dispatch
from harness.core.agent import Agent
from harness.core.policy import ActionClass, PolicyEngine
from harness.core.registry import ToolRegistry
from harness.core.session import Session
from harness.core.types import LLMResponse, ToolCall
from harness.store.audit import AuditStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.tools.base import Tool
from test_agent_loop import ScriptedProvider


class StuckTool(Tool):
    """Blocks until `gate` opens — forever, unless a test opens it."""

    name = "stuck"
    description = "Never returns."
    input_schema = {"type": "object", "properties": {}, "required": []}
    classification = ActionClass.READ_ONLY

    def __init__(self, gate: asyncio.Event, classification=ActionClass.READ_ONLY):
        self.gate = gate
        self.classification = classification
        self.ran = 0

    async def run(self, args):
        self.ran += 1
        await self.gate.wait()
        return "done"


class AllowAll(PolicyEngine):
    """Auto-runs every class — the gate is not what these tests are about."""

    async def check(self, *args, **kwargs):
        return "auto", "test"


async def _start(db, tool, calls=1):
    sessions, runs = SessionStore(db), RunStore(db)
    registry = ToolRegistry()
    registry.register(tool)
    step = LLMResponse(
        text="", tool_calls=[ToolCall(id=f"t{i}", name="stuck", arguments={})
                             for i in range(calls)],
    )
    provider = ScriptedProvider([step, LLMResponse(text="after")])
    agent = Agent(
        provider=provider, registry=registry, system_prompt="test",
        policy=AllowAll(), run_store=runs, audit=AuditStore(db),
    )
    session = await Session.create(sessions, channel="web")
    turn = asyncio.create_task(agent.run_turn(session, "go", channel="web"))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if agent._tool_executing:
            break
    assert agent._tool_executing
    return agent, turn, sessions, runs, session, provider


async def _audit_rows(db):
    cur = await db.conn.execute("SELECT tool, is_error, result_digest FROM audit_log ORDER BY id")
    return [dict(r) for r in await cur.fetchall()]


async def test_stop_abandons_a_read_only_tool_that_never_returns(db):
    tool = StuckTool(asyncio.Event())
    agent, turn, sessions, runs, session, provider = await _start(db, tool)

    assert agent.request_stop() is False  # still deferred: no task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=2)  # was: hung until the tool returned

    assert provider.calls == 1  # no model call after the Stop
    persisted = await sessions.load_messages(session.id)
    assert [m.role for m in persisted] == ["user", "assistant", "tool"]
    result = persisted[2].tool_results[0]
    assert result.is_error and "Stopped by the user" in result.content
    assert [r["tool"] for r in await _audit_rows(db)] == ["stuck"]  # audited too
    assert (await runs.get(agent.last_run["run_id"]))["status"] == "cancelled"


async def test_stop_gives_a_mutating_tool_the_grace_then_stops(db, monkeypatch):
    monkeypatch.setattr(tool_dispatch, "STOP_GRACE_S", 0.2)
    tool = StuckTool(asyncio.Event(), ActionClass.MUTATING_LOCAL)
    agent, turn, sessions, _, session, _ = await _start(db, tool)

    agent.request_stop()
    await asyncio.sleep(0.05)
    assert not turn.done()  # inside the grace: its side effect may be under way
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=2)
    result = (await sessions.load_messages(session.id))[2].tool_results[0]
    assert result.is_error and "may or may not have taken effect" in result.content


async def test_a_mutating_tool_that_finishes_inside_the_grace_keeps_its_result(db):
    gate = asyncio.Event()
    tool = StuckTool(gate, ActionClass.MUTATING_LOCAL)
    agent, turn, sessions, _, session, provider = await _start(db, tool)

    agent.request_stop()
    gate.set()  # finishes well inside STOP_GRACE_S
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=2)
    assert provider.calls == 1
    result = (await sessions.load_messages(session.id))[2].tool_results[0]
    assert (result.content, result.is_error) == ("done", False)


async def test_calls_after_the_stop_in_the_same_step_are_not_run(db):
    tool = StuckTool(asyncio.Event())
    agent, turn, sessions, _, session, _ = await _start(db, tool, calls=3)

    agent.request_stop()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(turn, timeout=2)
    assert tool.ran == 1  # the 2nd and 3rd never executed
    results = (await sessions.load_messages(session.id))[2].tool_results
    assert [r.tool_call_id for r in results] == ["t0", "t1", "t2"]  # every tool_use paired
    assert [r.content for r in results[1:]] == ["Not run: the user pressed Stop."] * 2


async def test_a_tool_that_returns_with_no_stop_is_unchanged(db):
    gate = asyncio.Event()
    tool = StuckTool(gate)
    agent, turn, sessions, _, session, provider = await _start(db, tool)

    gate.set()
    assert await asyncio.wait_for(turn, timeout=2) == "after"
    assert provider.calls == 2
    result = (await sessions.load_messages(session.id))[2].tool_results[0]
    assert (result.content, result.is_error) == ("done", False)
