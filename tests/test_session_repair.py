"""Resilience of the turn/session boundary: a turn that dies mid-dispatch must
never leave an assistant tool_use stranded without its tool_result (the shape
that 400s the next API call), and remote tools must refuse to run while offline.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from harness.core.agent import Agent
from harness.core.connectivity import is_online
from harness.core.policy import ActionClass, PolicyEngine
from harness.core.registry import ToolRegistry
from harness.core.session import Session, sanitize_history
from harness.core.types import LLMResponse, Message, ToolCall
from harness.store.sessions import SessionStore
from harness.tools.base import Tool

from test_agent_loop import ScriptedProvider


# --- test tools ---------------------------------------------------------------


class RemoteTool(Tool):
    name = "remote_echo"
    description = "A network-backed tool."
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(self) -> None:
        self.ran = False

    async def run(self, args: dict[str, Any]) -> str:
        self.ran = True
        return "remote ok"


class CancellingTool(Tool):
    name = "cancelling"
    description = "Unwinds like an offline MCP call."
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    classification = ActionClass.READ_ONLY

    async def run(self, args: dict[str, Any]) -> str:
        raise asyncio.CancelledError()


def build_agent(provider: ScriptedProvider, tools: list[Tool]) -> Agent:
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return Agent(
        provider=provider,
        registry=registry,
        system_prompt="test",
        policy=PolicyEngine(),
    )


def _one_call(name: str) -> LLMResponse:
    return LLMResponse(
        text=None, tool_calls=[ToolCall(id="t1", name=name, arguments={})]
    )


# --- sanitize_history ---------------------------------------------------------


def test_sanitize_drops_trailing_orphan_tool_use():
    msgs = [
        Message(role="user", text="hi"),
        Message(role="assistant", tool_calls=[ToolCall("t1", "x", {})]),
    ]
    out = sanitize_history(msgs)
    assert [m.role for m in out] == ["user"]


def test_sanitize_leaves_valid_history_untouched():
    msgs = [
        Message(role="user", text="hi"),
        Message(role="assistant", tool_calls=[ToolCall("t1", "x", {})]),
        Message(role="tool", tool_results=[]),
        Message(role="assistant", text="done"),
    ]
    out = sanitize_history(msgs)
    assert [m.role for m in out] == ["user", "assistant", "tool", "assistant"]


def test_sanitize_empty_is_fine():
    assert sanitize_history([]) == []


def test_sanitize_drops_interior_orphan_with_user_pileup():
    # The real-world shape: a stranded tool_use mid-history with retry user
    # turns piled on after it (each retry 400'd before an answer was stored).
    msgs = [
        Message(role="user", text="start"),
        Message(role="assistant", tool_calls=[ToolCall("t1", "read", {})]),
        Message(role="tool", tool_results=[]),  # completes t1
        Message(role="assistant", tool_calls=[ToolCall("t2", "bash", {})]),  # orphan
        Message(role="user", text="did it fail?"),
        Message(role="user", text="hello?"),
    ]
    out = sanitize_history(msgs)
    assert [m.role for m in out] == ["user", "assistant", "tool", "user", "user"]
    # No assistant tool_use is left without a following tool message.
    for i, m in enumerate(out):
        if m.role == "assistant" and m.tool_calls:
            assert out[i + 1].role == "tool"


def test_sanitize_keeps_orphan_assistant_prose():
    msgs = [
        Message(role="user", text="go"),
        Message(
            role="assistant",
            text="let me check that",
            tool_calls=[ToolCall("t1", "bash", {})],
        ),
        Message(role="user", text="?"),
    ]
    out = sanitize_history(msgs)
    assert [m.role for m in out] == ["user", "assistant", "user"]
    assert out[1].text == "let me check that"
    assert out[1].tool_calls == []


def test_sanitize_drops_orphan_tool_result():
    msgs = [
        Message(role="user", text="go"),
        Message(role="tool", tool_results=[]),  # no owning tool_use
        Message(role="assistant", text="ok"),
    ]
    out = sanitize_history(msgs)
    assert [m.role for m in out] == ["user", "assistant"]


# --- resume repairs a corrupted store -----------------------------------------


async def test_resume_strips_orphaned_tool_use(db):
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("do a thing")
    # A turn that died after persisting the assistant tool_use but before its
    # tool_result — exactly what an offline/cancelled dispatch used to leave.
    await session.add_assistant(None, [ToolCall("t1", "remote_echo", {})])

    resumed = await Session.resume(store, session.id)

    assert resumed is not None
    assert [m.role for m in resumed.messages] == ["user"]
    # The store itself is untouched (append-only) and the next index continues
    # past the full stored length, not the trimmed view.
    assert resumed._next_idx == 2
    assert len(await store.load_messages(session.id)) == 2


# --- atomic step write --------------------------------------------------------


async def test_append_step_writes_both_rows_in_order(db):
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("go")
    await session.add_step(
        "thinking",
        [ToolCall("t1", "x", {})],
        [],
    )
    roles = [m.role for m in await store.load_messages(session.id)]
    assert roles == ["user", "assistant", "tool"]


async def test_uncaught_dispatch_failure_persists_no_orphan(db):
    """If dispatch dies with an uncaught error, add_step is never reached, so
    the store keeps only the user turn — no stranded tool_use to 400 on resume."""
    store = SessionStore(db)
    session = await Session.create(store)
    provider = ScriptedProvider([_one_call("remote_echo")])
    agent = build_agent(provider, [RemoteTool()])

    async def boom(*_a, **_k):
        raise RuntimeError("policy exploded")

    agent.policy.check = boom  # force an uncaught raise inside dispatch

    with pytest.raises(RuntimeError):
        await agent.run_turn(session, "go")

    roles = [m.role for m in await store.load_messages(session.id)]
    assert roles == ["user"]
    # And a resume of the wreckage is clean.
    resumed = await Session.resume(store, session.id)
    assert [m.role for m in resumed.messages] == ["user"]


async def test_tool_cancellederror_becomes_error_result_not_corruption():
    provider = ScriptedProvider([_one_call("cancelling"), LLMResponse(text="ok")])
    agent = build_agent(provider, [CancellingTool()])
    session = Session()

    final = await agent.run_turn(session, "go")

    assert final == "ok"
    assert [m.role for m in session.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    result = session.messages[2].tool_results[0]
    assert result.is_error is True, (
        "CancelledError bypasses `except Exception` — dispatch must still turn it "
        "into an error result (via Session.add_step, one commit) or the tool_use orphans (400)"
    )
    assert "interrupted" in result.content


# --- offline pre-flight for remote tools --------------------------------------


async def test_remote_tool_refused_when_offline(monkeypatch):
    async def offline(*_a, **_k):
        return False

    monkeypatch.setattr("harness.core.tool_dispatch.is_online", offline)

    tool = RemoteTool()
    provider = ScriptedProvider([_one_call("remote_echo"), LLMResponse(text="noted")])
    agent = build_agent(provider, [tool])
    session = Session()

    await agent.run_turn(session, "go")

    assert tool.ran is False
    result = session.messages[2].tool_results[0]
    assert result.is_error is True
    assert "no network" in result.content


async def test_remote_tool_runs_when_online(monkeypatch):
    async def online(*_a, **_k):
        return True

    monkeypatch.setattr("harness.core.tool_dispatch.is_online", online)

    tool = RemoteTool()
    provider = ScriptedProvider([_one_call("remote_echo"), LLMResponse(text="done")])
    agent = build_agent(provider, [tool])
    session = Session()

    await agent.run_turn(session, "go")

    assert tool.ran is True
    assert session.messages[2].tool_results[0].is_error is False


async def test_local_tool_skips_offline_probe(monkeypatch):
    """A local tool must never be gated by connectivity, even if a probe would
    report offline."""
    probed = False

    async def offline(*_a, **_k):
        nonlocal probed
        probed = True
        return False

    monkeypatch.setattr("harness.core.tool_dispatch.is_online", offline)

    from test_agent_loop import EchoTool

    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall("t1", "echo", {"text": "hi"})],
            ),
            LLMResponse(text="done"),
        ]
    )
    agent = build_agent(provider, [EchoTool()])
    session = Session()

    await agent.run_turn(session, "go")

    assert probed is False
    assert session.messages[2].tool_results[0].content == "echo: hi"


# --- the probe itself ---------------------------------------------------------


async def test_is_online_false_when_connect_refused(monkeypatch):
    async def refuse(*_a, **_k):
        raise ConnectionRefusedError()

    monkeypatch.setattr("asyncio.open_connection", refuse)
    assert await is_online(timeout_s=0.1) is False


async def test_is_online_false_on_timeout(monkeypatch):
    async def hang(*_a, **_k):
        await asyncio.sleep(10)

    monkeypatch.setattr("asyncio.open_connection", hang)
    assert await is_online(timeout_s=0.05) is False


async def test_is_online_true_when_connect_succeeds(monkeypatch):
    class FakeWriter:
        def close(self):
            pass

        async def wait_closed(self):
            pass

    async def connect(*_a, **_k):
        return (object(), FakeWriter())

    monkeypatch.setattr("asyncio.open_connection", connect)
    assert await is_online(timeout_s=0.1) is True
