"""Context compaction (P6): long histories shrink, recent turns survive."""

from __future__ import annotations

from harness.core.agent import Agent
from harness.core.context import Compactor, estimate_tokens
from harness.core.policy import PolicyEngine
from harness.core.registry import ToolRegistry
from harness.core.session import Session
from harness.core.types import LLMResponse, Message, ToolCall, ToolResult
from harness.store.sessions import SessionStore

from test_agent_loop import ScriptedProvider


def long_chat(turns: int, chars: int = 400) -> list[Message]:
    msgs: list[Message] = []
    for i in range(turns):
        msgs.append(Message(role="user", text=f"question {i} " + "x" * chars))
        msgs.append(Message(role="assistant", text=f"answer {i} " + "y" * chars))
    return msgs


async def test_under_budget_is_untouched():
    session = Session()
    session.messages = long_chat(3)
    compactor = Compactor(summarizer=None, budget_tokens=10_000, keep_tokens=100)
    assert await compactor.maybe_compact(session) is None
    assert len(session.messages) == 6


async def test_compaction_summarizes_middle_and_keeps_opening_and_tail():
    summarizer = ScriptedProvider([LLMResponse(text=(
        "SUMMARY OF EARLIER: goals, decisions and their reasons, the exact "
        "paths, ids and dates the work depends on, the open items still in "
        "flight, and every preference the user stated along the way, kept as "
        "structured substance rather than prose."
    ))])
    session = Session()
    session.messages = long_chat(20)
    before = estimate_tokens(session.messages)
    compactor = Compactor(summarizer=summarizer, budget_tokens=1_000, keep_tokens=300)

    info = await compactor.maybe_compact(session)

    assert info is not None and info["summarized"] is True
    assert info["est_tokens_after"] < before
    # Three parts, in order: verbatim opening, the checkpoint block, verbatim
    # tail — the opening is no longer merged into anything.
    assert session.messages[0].role == "user"
    assert session.messages[0].text.startswith("question 0")
    assert session.messages[1].text.startswith("answer 0")
    checkpoint = session.messages[2]
    assert checkpoint.role == "user"
    assert "<context_checkpoint>" in checkpoint.text
    assert "SUMMARY OF EARLIER" in checkpoint.text
    # The most recent exchange survives verbatim.
    assert session.messages[-1].text.startswith("answer 19")
    # The summarizer saw the ELIDED MIDDLE only — the frozen opening is kept
    # verbatim forever, so re-summarizing it would waste the band.
    transcript = summarizer.sent[0][0].text
    assert "USER: question 1" in transcript
    assert "question 0" not in transcript


def assert_pairs_intact(view: list[Message]) -> None:
    """The API's hard requirement: every assistant tool_use is immediately
    followed by a tool message carrying a result for EVERY one of its ids."""
    for i, m in enumerate(view):
        if m.role == "assistant" and m.tool_calls:
            assert i + 1 < len(view), "tool_use at the end of the view"
            nxt = view[i + 1]
            assert nxt.role == "tool", f"tool_use at {i} not followed by tool msg"
            ids = {tr.tool_call_id for tr in nxt.tool_results}
            missing = {tc.id for tc in m.tool_calls} - ids
            assert not missing, f"tool_use ids without results at {i}: {missing}"


async def test_cut_never_splits_a_tool_exchange():
    session = Session()
    msgs: list[Message] = []
    for i in range(12):
        msgs.append(Message(role="user", text=f"task {i} " + "x" * 400))
        msgs.append(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id=f"t{i}", name="grep", arguments={"q": "z"})],
            )
        )
        msgs.append(
            Message(
                role="tool",
                tool_results=[
                    ToolResult(tool_call_id=f"t{i}", content="hits " + "r" * 400)
                ],
            )
        )
        msgs.append(Message(role="assistant", text=f"found {i} " + "y" * 400))
    session.messages = msgs
    compactor = Compactor(summarizer=None, budget_tokens=1_000, keep_tokens=300)

    info = await compactor.maybe_compact(session)

    assert info is not None
    # Whatever the cut lands on, the sent view keeps every exchange whole.
    assert session.messages[0].role == "user"
    assert session.messages[0].text
    assert_pairs_intact(session.messages)


async def test_summarizer_failure_degrades_to_elision_note():
    class BrokenProvider(ScriptedProvider):
        async def chat(self, system, messages, tools, on_text_delta=None):
            raise RuntimeError("summarizer down")

    session = Session()
    session.messages = long_chat(20)
    compactor = Compactor(
        summarizer=BrokenProvider([]), budget_tokens=1_000, keep_tokens=300
    )

    info = await compactor.maybe_compact(session)

    assert info is not None and info["summarized"] is False
    assert "summarizer down" in info["summary_error"]
    assert "elided" in session.messages[2].text  # the checkpoint message


async def test_compaction_does_not_corrupt_persisted_history(db):
    store = SessionStore(db)
    session = await Session.create(store)
    for i in range(6):
        await session.add_user(f"q{i} " + "x" * 200)
        await session.add_assistant(f"a{i} " + "y" * 200)
    compactor = Compactor(summarizer=None, budget_tokens=100, keep_tokens=50)
    assert await compactor.maybe_compact(session) is not None

    # Appends after compaction keep unique storage indexes...
    await session.add_user("after compaction")
    await session.add_assistant("final")
    # ...and the store still holds the FULL history, in order.
    loaded = await store.load_messages(session.id)
    assert len(loaded) == 14
    assert loaded[0].text.startswith("q0")
    assert loaded[-2].text == "after compaction"
    assert loaded[-1].text == "final"


async def test_agent_loop_compacts_before_calling_the_model():
    provider = ScriptedProvider([LLMResponse(text="hi")])
    agent = Agent(
        provider=provider,
        registry=ToolRegistry(),
        system_prompt="test",
        policy=PolicyEngine(),
        compactor=Compactor(summarizer=None, budget_tokens=500, keep_tokens=200),
    )
    session = Session()
    session.messages = long_chat(20)

    await agent.run_turn(session, "one more question")

    sent = provider.sent[0]
    # Three-part shape reaches the wire: verbatim opening first, the
    # checkpoint block third (after the opening's answer), the new question last.
    assert sent[0].text.startswith("question 0")
    assert "<context_checkpoint>" in sent[2].text
    assert sent[-1].text == "one more question"
    assert estimate_tokens(sent) < estimate_tokens(long_chat(20))
