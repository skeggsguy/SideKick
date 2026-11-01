"""Extended thinking (Sonnet 5): round-trip rules, persistence, truncation,
and thinking-token accounting."""

from __future__ import annotations

from harness.core.session import Session, sanitize_history
from harness.core.types import LLMResponse, Message, ToolCall, ToolResult, Usage
from harness.providers.anthropic_provider import (
    AnthropicProvider,
    _to_api_messages,
)
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore, _message_from_dict
from test_agent_loop import EchoTool, ScriptedProvider, build_agent

THINK = {"type": "thinking", "thinking": "let me reason", "signature": "sig-abc"}
REDACTED = {"type": "redacted_thinking", "data": "opaque"}
TEXT = {"type": "text", "text": "checking"}
TOOL_USE = {"type": "tool_use", "id": "t1", "name": "echo", "input": {"text": "hi"}}


def _history() -> list[Message]:
    """user → assistant(think+tool) → tool → user → assistant(think+tool) → tool.
    The second user message starts the current turn."""
    return [
        Message(role="user", text="first"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="t0", name="echo", arguments={})],
            content_blocks=[THINK, {**TOOL_USE, "id": "t0"}],
        ),
        Message(role="tool", tool_results=[ToolResult(tool_call_id="t0", content="ok")]),
        Message(role="user", text="second"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "hi"})],
            content_blocks=[THINK, REDACTED, TEXT, TOOL_USE],
        ),
        Message(role="tool", tool_results=[ToolResult(tool_call_id="t1", content="ok")]),
    ]


# -- provider round-trip rules -------------------------------------------


def test_intra_turn_blocks_sent_verbatim_cross_turn_thinking_stripped():
    api = _to_api_messages(_history(), thinking_on=True)
    # Cross-turn assistant (before the last user message): thinking stripped,
    # text/tool_use order preserved.
    assert api[1]["content"] == [{**TOOL_USE, "id": "t0"}], (
        "Sonnet 5 thinking blocks must be STRIPPED cross-turn (else 400)"
    )
    # Current-turn assistant: everything verbatim, including redacted_thinking.
    assert api[4]["content"] == [THINK, REDACTED, TEXT, TOOL_USE], (
        "within a tool loop thinking blocks round-trip VERBATIM (else 400)"
    )


def test_thinking_off_strips_thinking_blocks_everywhere():
    api = _to_api_messages(_history(), thinking_on=False)
    assert api[1]["content"] == [{**TOOL_USE, "id": "t0"}]
    assert api[4]["content"] == [TEXT, TOOL_USE]


def test_local_lane_keeps_thinking_cross_turn_and_when_off():
    """Local lane: the Qwen template re-renders prior reasoning, so stripping
    it cross-turn diverges the prompt and the llama.cpp server re-prefills the
    whole conversation every turn (learnings 2026-09-30)."""
    for thinking_on in (True, False):
        api = _to_api_messages(
            _history(), thinking_on=thinking_on, keep_all_thinking=True
        )
        assert api[1]["content"] == [THINK, {**TOOL_USE, "id": "t0"}]
        assert api[4]["content"] == [THINK, REDACTED, TEXT, TOOL_USE]


def test_legacy_message_without_blocks_uses_reconstruction():
    api = _to_api_messages(
        [
            Message(role="user", text="hi"),
            Message(
                role="assistant",
                text="calling",
                tool_calls=[ToolCall(id="t1", name="echo", arguments={"a": 1})],
            ),
        ],
        thinking_on=True,
    )
    assert api[1]["content"] == [
        {"type": "text", "text": "calling"},
        {"type": "tool_use", "id": "t1", "name": "echo", "input": {"a": 1}},
    ]


def test_thinking_params_are_model_gated():
    on = AnthropicProvider(api_key="x", model="claude-sonnet-5", thinking="adaptive")
    assert on._thinking_param() == {"type": "adaptive", "display": "summarized"}
    off = AnthropicProvider(api_key="x", model="claude-sonnet-5", thinking="off")
    assert off._thinking_param() == {"type": "disabled"}
    # A non-supporting model never receives the param, whatever the env says.
    haiku = AnthropicProvider(api_key="x", model="claude-haiku-4-5", thinking="adaptive")
    assert haiku._thinking_param() is None, (
        "thinking/output_config are gated on THINKING_MODELS — any other model 400s on them"
    )
    sonnet46 = AnthropicProvider(
        api_key="x", model="claude-sonnet-4-6", thinking="adaptive"
    )
    assert sonnet46._thinking_param() is None


# -- sanitize + store round-trip -------------------------------------------


def test_sanitize_keeps_blocks_on_pairs_drops_them_on_orphans():
    complete = _history()
    assert sanitize_history(complete)[4].content_blocks == [
        THINK,
        REDACTED,
        TEXT,
        TOOL_USE,
    ]
    # An orphaned tool_use (turn died mid-dispatch): prose survives, the raw
    # blocks (which embed the stranded tool_use + dangling signature) do not.
    orphan = [
        Message(role="user", text="hi"),
        Message(
            role="assistant",
            text="prose",
            tool_calls=[ToolCall(id="t9", name="echo", arguments={})],
            content_blocks=[THINK, {**TOOL_USE, "id": "t9"}],
        ),
    ]
    repaired = sanitize_history(orphan)
    assert [m.role for m in repaired] == ["user", "assistant"]
    assert repaired[1].text == "prose"
    assert repaired[1].content_blocks == []
    assert repaired[1].tool_calls == []


async def test_content_blocks_round_trip_through_the_store(db):
    store = SessionStore(db)
    session = await Session.create(store, channel="cli")
    await session.add_step(
        "checking",
        [ToolCall(id="t1", name="echo", arguments={"text": "hi"})],
        [ToolResult(tool_call_id="t1", content="ok")],
        content_blocks=[THINK, TEXT, TOOL_USE],
    )
    resumed = await Session.resume(store, session.id)
    assert resumed.messages[0].content_blocks == [THINK, TEXT, TOOL_USE]


def test_message_from_dict_tolerates_missing_content_blocks():
    legacy = _message_from_dict({"role": "assistant", "text": "old row"})
    assert legacy.content_blocks == []
    new = _message_from_dict(
        {"role": "assistant", "text": "new", "content_blocks": [THINK]}
    )
    assert new.content_blocks == [THINK]


# -- agent loop: persistence, truncation, accounting ------------------------


async def test_agent_persists_content_blocks():
    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "hi"})],
                content_blocks=[THINK, TOOL_USE],
            ),
            LLMResponse(text="done", content_blocks=[{"type": "text", "text": "done"}]),
        ]
    )
    agent = build_agent(provider, [EchoTool()])
    session = Session()
    await agent.run_turn(session, "go")
    assert session.messages[1].content_blocks == [THINK, TOOL_USE]
    assert session.messages[3].content_blocks == [{"type": "text", "text": "done"}]


async def test_truncated_prose_is_kept_but_blocks_dropped():
    provider = ScriptedProvider(
        [LLMResponse(text="half an ans", stop_reason="max_tokens",
                     content_blocks=[THINK, {"type": "text", "text": "half an ans"}])]
    )
    agent = build_agent(provider, [EchoTool()])
    truncations = []
    agent.on_truncated = lambda: truncations.append(True)
    session = Session()
    final = await agent.run_turn(session, "go")
    assert final == "half an ans"
    assert truncations == [True]
    # Persisted as plain prose, without the possibly-mid-thought raw blocks.
    assert session.messages[-1].text == "half an ans"
    assert session.messages[-1].content_blocks == []


async def test_truncated_mid_tool_use_is_never_dispatched():
    calls = []

    class RecordingEcho(EchoTool):
        async def run(self, args):
            calls.append(args)
            return "ran"

    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "par"})],
                stop_reason="max_tokens",
            )
        ]
    )
    agent = build_agent(provider, [RecordingEcho()])
    truncations = []
    agent.on_truncated = lambda: truncations.append(True)
    session = Session()
    final = await agent.run_turn(session, "go")
    assert calls == []          # partial tool input must not run a real action
    assert truncations == [True]
    assert final == ""
    # The partial assistant message is not persisted (would strand a tool_use).
    assert [m.role for m in session.messages] == ["user"]


def test_usage_add_sums_thinking_tokens():
    total = Usage()
    total.add(Usage(output_tokens=10, thinking_tokens=7))
    total.add(Usage(output_tokens=5, thinking_tokens=3))
    assert total.thinking_tokens == 10
    assert total.output_tokens == 15


def test_to_response_text_excludes_thinking_blocks():
    """The compaction summarizer THINKS (Step 4) and only resp.text is
    persisted — the checkpoint must hold the summary, never the reasoning.
    _to_response collects TEXT blocks only; the thinking tokens still ride
    usage (captured off message_delta) and fold into the run totals."""
    import anthropic

    from harness.providers.anthropic_provider import _to_response

    msg = anthropic.types.Message.model_validate(
        {
            "id": "m1",
            "type": "message",
            "role": "assistant",
            "model": "qwen3.8-flash-next",
            "content": [THINK, TEXT],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 5, "output_tokens": 9},
        }
    )
    r = _to_response(msg, 4)
    assert r.text == "checking"  # the thinking text never enters resp.text
    assert r.usage.thinking_tokens == 4
    assert r.usage.output_tokens == 9  # thinking tokens included, as always
    # The raw blocks survive for the chat-history round-trip (local lane).
    assert [b["type"] for b in r.content_blocks] == ["thinking", "text"]
    # Discriminator: with ONLY a thinking block there is no text at all —
    # an extractor that collected thinking text would return it here.
    thinking_only = _to_response(
        msg.model_copy(update={"content": [msg.content[0]]}), 4
    )
    assert thinking_only.text is None


async def test_run_row_accumulates_thinking_tokens_across_steps(db):
    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "hi"})],
                usage=Usage(input_tokens=10, output_tokens=20, thinking_tokens=8),
            ),
            LLMResponse(
                text="done",
                usage=Usage(input_tokens=15, output_tokens=5, thinking_tokens=4),
            ),
        ]
    )
    runs = RunStore(db)
    agent = build_agent(provider, [EchoTool()])
    agent.run_store = runs
    session = Session()
    await agent.run_turn(session, "go")
    row = (await runs.list(limit=1))[0]
    assert row["thinking_tokens"] == 12
    assert row["output_tokens"] == 25
    summary = await runs.summary(days=1)
    assert summary["thinking_tokens"] == 12


# ---- history cache breakpoint (B1) ------------------------------------------

def _cache_marks(api: list[dict]) -> list[tuple[int, int]]:
    """(message_index, block_index) of every cache_control in the payload."""
    marks = []
    for i, m in enumerate(api):
        content = m["content"]
        if isinstance(content, list):
            for j, b in enumerate(content):
                if isinstance(b, dict) and "cache_control" in b:
                    marks.append((i, j))
    return marks


def test_history_breakpoint_on_newest_user_message():
    api = _to_api_messages(
        [
            Message(role="user", text="first"),
            Message(role="assistant", text="answer one"),
            Message(role="user", text="second"),
        ],
        thinking_on=False,
    )
    # Exactly ONE moving mark, on the final user message's last block.
    assert _cache_marks(api) == [(2, 0)]
    # User content is always block-form so the serialized shape is identical
    # with and without the mark (prefix bytes stay cache-stable).
    assert api[0]["content"] == [{"type": "text", "text": "first"}]


def test_history_breakpoint_advances_to_tool_results():
    api = _to_api_messages(
        [
            Message(role="user", text="do it"),
            Message(
                role="assistant",
                tool_calls=[ToolCall(id="t1", name="echo", arguments={})],
            ),
            Message(
                role="tool",
                tool_results=[
                    ToolResult(tool_call_id="t1", content="ok", is_error=False)
                ],
            ),
        ],
        thinking_on=False,
    )
    # Mid tool-loop the mark sits on the newest tool_result batch (a user-role
    # API message), not the older human turn.
    assert _cache_marks(api) == [(2, 0)]
    assert api[2]["content"][0]["type"] == "tool_result"


def test_history_breakpoint_never_touches_thinking_blocks():
    api = _to_api_messages(_history(), thinking_on=True)
    # Verbatim assistant blocks stay unannotated; the one mark is user-role.
    for i, j in _cache_marks(api):
        assert api[i]["role"] == "user"
        assert api[i]["content"][j].get("type") != "thinking"
    assert len(_cache_marks(api)) == 1
