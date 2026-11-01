"""Three-part compaction context (Step 2): frozen opening, banded summary,
legal mid-turn cut.

Pins: (a) the sent view is [opening verbatim][<context_checkpoint>][tail]
with the opening byte-stable across resumes; (b) the summary target clamps
into the [2k, 10k] band for a small and a huge middle; (c) a single 60-message
tool-loop turn (the a88873342e1f trap) IS cuttable at a step boundary and no
tool_use is ever separated from its tool_result.
"""

from __future__ import annotations

from harness.core.context import (
    SUMMARY_BAND_MAX,
    SUMMARY_BAND_MIN,
    Compactor,
    summary_target_tokens,
)
from harness.core.session import OPENING_TOOL_RESULT_CHARS, SYNTH_IDX, Session
from harness.core.types import LLMResponse, Message, ToolCall, ToolResult
from harness.store.sessions import SessionStore

from test_agent_loop import ScriptedProvider
from test_context import assert_pairs_intact

# Canned summarizer replies must clear the echo guard's floor (max(200 chars,
# 2% of the band target), core/summarize.py) — a short canned reply now reads as an
# instruction echo and takes the FAILED path; these tests are about the cut,
# the band and byte stability, not about failing the summary. The ceiling test
# asks for SUMMARY_BAND_MAX (floor 600 chars), so MID_SUM must clear THAT.
MID_SUM = (
    "MIDDLE SUMMARY: the middle stretch's goals and the decisions made with "
    "their reasons, the exact paths, ids, dates and account numbers the work "
    "depends on, the open items left in flight and who owns each of them, "
    "every preference the user stated while the work was being done, and the "
    "failures worth not repeating — kept as structured substance rather than "
    "prose, so the next turn can continue without the original transcript, "
    "re-checking the live state of any fact before trusting it rather than "
    "acting on a remembered number, path or identifier that may since have "
    "changed, and naming the next concrete step so work resumes immediately."
)

MEMORY = "<memory>\nfact: the widget repo lives at ~/dev/widget\n</memory>"
MARKER = "truncated in the frozen opening"


def deep_snapshot(session: Session) -> list[tuple]:
    """Stronger than (role, text): tool ids and result bytes ride the prefix
    cache too, so byte equality must cover them."""
    return [
        (
            m.role,
            m.text or "",
            [(tc.id, tc.name, str(tc.arguments)) for tc in m.tool_calls],
            [(tr.tool_call_id, tr.content) for tr in m.tool_results],
        )
        for m in session.messages
    ]


async def seed_first_exchange_with_tools(db):
    """A real opening: first user message incl. its <memory> block, a tool
    exchange with one GIANT result, then the answer — and enough later turns
    to blow the budget."""
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("Build a widget report.\n" + MEMORY)
    await session.add_step(
        None,
        [ToolCall(id="t0", name="read_file", arguments={"path": "~/dev/widget/README"})],
        [ToolResult(tool_call_id="t0", content="README " + "r" * 9_000)],
    )
    await session.add_assistant("Read it — here is the plan: " + "p" * 300)
    for i in range(8):
        await session.add_user(f"step {i} detail " + "x" * 700)
        await session.add_assistant(f"step {i} done " + "y" * 700)
    return store, session


# --- (a) opening → checkpoint block → tail, byte-stable opening -------------


async def test_view_is_opening_then_checkpoint_then_tail(db):
    store, session = await seed_first_exchange_with_tools(db)
    summarizer = ScriptedProvider([LLMResponse(text=MID_SUM)])
    compactor = Compactor(summarizer=summarizer, budget_tokens=1_500, keep_tokens=600)

    info = await compactor.maybe_compact(session)
    assert info is not None
    assert info["summarized"] is True  # success path (learnings 2026-10-22)

    view = session.messages
    # [1] OPENING: the first user message verbatim — <memory> block included.
    assert view[0].text == "Build a widget report.\n" + MEMORY
    # ...its tool exchange kept (calls survive)...
    assert view[1].tool_calls[0].id == "t0"
    # ...with the giant tool RESULT deterministically truncated.
    frozen_result = view[2].tool_results[0].content
    raw_result = "README " + "r" * 9_000
    assert len(frozen_result) < len(raw_result)
    assert frozen_result.startswith(raw_result[:OPENING_TOOL_RESULT_CHARS])
    assert MARKER in frozen_result
    assert "9007 characters" in frozen_result
    assert view[3].text.startswith("Read it — here is the plan")
    # [2] the checkpoint block: framing + summary, a user message of its own.
    checkpoint = view[4]
    assert checkpoint.role == "user"
    assert checkpoint.text.startswith("<context_checkpoint>")
    assert "compacted by a local model" in checkpoint.text
    assert "authoritative" in checkpoint.text
    assert "MIDDLE SUMMARY" in checkpoint.text
    # [3] TAIL: verbatim from the cut onward.
    assert view[5].text.startswith("step 6 detail")
    assert view[-1].text.startswith("step 7 done")
    assert_pairs_intact(view)

    row = await store.load_checkpoint(session.id)
    assert row["covers_upto_idx"] == 4  # opening = the first exchange (4 rows)
    stored = await store.load_messages(session.id)
    # The anchor is the tail's first stored row — kept verbatim, addressed by
    # its store idx, with its text OUTSIDE the checkpoint block.
    assert stored[row["anchor_idx"]].text.startswith("step 6 detail")
    assert row["anchor_idx"] > row["covers_upto_idx"]


async def test_opening_bytes_identical_across_two_resumes(db):
    store, session = await seed_first_exchange_with_tools(db)
    summarizer = ScriptedProvider([LLMResponse(text=MID_SUM)])
    compactor = Compactor(summarizer=summarizer, budget_tokens=1_500, keep_tokens=600)
    assert await compactor.maybe_compact(session) is not None

    live = deep_snapshot(session)
    first = await Session.resume(store, session.id)
    second = await Session.resume(store, session.id)
    # Byte equality of EVERY message — opening (incl. the truncated tool
    # result), the checkpoint block, the tail — across both resumes.
    assert deep_snapshot(first) == live
    assert deep_snapshot(second) == live
    # The opening is addressed by the frozen covers_upto; the checkpoint
    # message carries the SYNTH marker between opening and tail.
    assert first.store_idxs[4] == SYNTH_IDX
    # The framing's timestamp is stored, not re-read from a clock — the one
    # byte source that could otherwise diverge live vs resume.
    row = await store.load_checkpoint(session.id)
    assert row["created_at"] in second.messages[4].text
    assert second.messages[4].text == session.messages[4].text


# --- (b) the summary size band ------------------------------------------------


async def _compact_three_turn_chat(budget: int, keep: int) -> dict:
    session = Session()
    session.messages = [
        Message(role="user", text="open " + "a" * 200),
        Message(role="assistant", text="ack " + "b" * 200),
        Message(role="user", text="one more " + "c" * 300),
        Message(role="assistant", text="done " + "d" * 300),
        Message(role="user", text="and then " + "e" * 300),
        Message(role="assistant", text="final " + "f" * 300),
    ]
    compactor = Compactor(
        summarizer=ScriptedProvider([LLMResponse(text=MID_SUM)]),
        budget_tokens=budget,
        keep_tokens=keep,
    )
    info = await compactor.maybe_compact(session)
    assert info is not None
    return info


async def test_summary_target_band_floor_small_middle():
    # A tiny middle still gets a real summary — the band FLOOR, not a
    # proportional sliver.
    info = await _compact_three_turn_chat(budget=100, keep=150)
    assert info["summary_target"] == SUMMARY_BAND_MIN


async def test_band_target_reaches_the_summarizer_prompt():
    """The band is not bookkeeping — the number must ride the prompt that is
    actually sent (wiring pin: check what the callee RECEIVES)."""
    session = Session()
    summarizer = ScriptedProvider([LLMResponse(text=MID_SUM)])
    session.messages = [
        Message(role="user", text="open " + "a" * 200),
        Message(role="assistant", text="ack " + "b" * 200),
        Message(role="user", text="one more " + "c" * 300),
        Message(role="assistant", text="done " + "d" * 300),
        Message(role="user", text="and then " + "e" * 300),
        Message(role="assistant", text="final " + "f" * 300),
    ]
    compactor = Compactor(summarizer=summarizer, budget_tokens=100, keep_tokens=150)
    info = await compactor.maybe_compact(session)
    assert info is not None
    system = summarizer.systems[0]
    assert str(info["summary_target"]) in system  # the floor, 2000
    assert "GOALS" in system and "FACTS" in system  # structured retention


async def test_summary_target_band_ceiling_huge_middle():
    session = Session()
    session.messages = [
        Message(role="user", text="open " + "a" * 300),
        Message(role="assistant", text="ack " + "b" * 300),
    ]
    for i in range(40):  # ~200k chars of middle → 15% is far past 10k
        session.messages.append(Message(role="user", text=f"q{i} " + "x" * 5_000))
        session.messages.append(Message(role="assistant", text=f"a{i} " + "y" * 5_000))
    compactor = Compactor(
        summarizer=ScriptedProvider([LLMResponse(text=MID_SUM)]),
        budget_tokens=10_000,
        keep_tokens=1_000,
    )
    info = await compactor.maybe_compact(session)
    assert info is not None
    assert info["summary_target"] == SUMMARY_BAND_MAX
    # Honest fixture: the canned reply must clear the ceiling's echo floor
    # (600 chars) or this asserts a band on the FAILED path (summarized=0).
    assert info["summarized"] is True


async def test_echo_floor_scales_with_the_band_target():
    """The echo guard is not a flat 200 chars: under max(200, 2% of the band
    target in chars) the reply is an echo, so at the 10k ceiling the floor is
    600 — a reply that would pass at the 2k floor is still failed here."""
    middle = [Message(role="user", text="archive block " + "x" * 900)]
    compactor = Compactor(
        summarizer=ScriptedProvider([LLMResponse(text="S" * 300)])
    )
    text, summarized, error, _ = await compactor._summarize(
        middle, None, SUMMARY_BAND_MAX
    )
    assert summarized is False
    assert error == "summary implausibly short"
    assert "no summary is available" in text

    # The boundary is inclusive: exactly 2% of the target in chars passes.
    compactor = Compactor(
        summarizer=ScriptedProvider([LLMResponse(text="S" * 600)])
    )
    text, summarized, error, _ = await compactor._summarize(
        middle, None, SUMMARY_BAND_MAX
    )
    assert summarized is True and error is None and text == "S" * 600


def test_summary_target_clamp_pure():
    assert summary_target_tokens(100) == SUMMARY_BAND_MIN  # floor
    assert summary_target_tokens(20_000) == 3_000  # proportional middle
    assert summary_target_tokens(1_000_000) == SUMMARY_BAND_MAX  # ceiling


def test_composition_max_tokens_covers_band_ceiling():
    """Step 4 strengthened: max_tokens must cover the band ceiling PLUS the
    guaranteed think space — with the reserve parameterized, 10k think and a
    10k ceiling summary are both reachable in one call."""
    from harness.composition import COMPACTION_MAX_TOKENS, COMPACTION_THINKING_TOKENS

    assert COMPACTION_MAX_TOKENS >= SUMMARY_BAND_MAX + COMPACTION_THINKING_TOKENS


def test_context_reexports_are_the_summarize_module_themselves():
    """The summarization concern lives in core/summarize.py; context.py only
    re-exports it. Assert IDENTITY, not equality: an equality check passes
    over a shadowing copy left in context.py (the import wins because the
    re-export line runs after the old def), and that copy then silently
    diverges the moment summarize.py changes — while every test stays green."""
    import harness.core.context as ctx
    import harness.core.summarize as sm

    for name in (
        "SUMMARY_TARGET_SHARE",
        "SUMMARY_BAND_MIN",
        "SUMMARY_BAND_MAX",
        "PIECE_CHARS",
        "TRANSCRIPT_CHARS",
        "_SUMMARY_BLOCK",
        "CHARS_PER_TOKEN",
        "summary_system",
        "summary_target_tokens",
        "summarize_middle",
        "render_transcript",
    ):
        assert getattr(ctx, name) is getattr(sm, name), name


# --- (c) the 60-message tool-loop trap ---------------------------------------


async def test_sixty_message_tool_loop_turn_is_cuttable(db):
    """One long tool-loop turn (61 messages, no user text inside) was
    uncuttable: the old cut rule demanded a user text message, so compaction
    stalled at 148k. A complete assistant tool_use + tool_result step is now
    a legal boundary."""
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("open the original task " + "o" * 100)
    await session.add_assistant("acknowledged " + "a" * 100)
    await session.add_user("now the big one-shot job")  # store idx 2
    for i in range(29):
        await session.add_step(
            None,
            [ToolCall(id=f"L{i}", name="browser_interact", arguments={"i": i})],
            [ToolResult(tool_call_id=f"L{i}", content="page " + "z" * 900)],
        )
    await session.add_assistant("all done " + "d" * 200)

    compactor = Compactor(
        summarizer=ScriptedProvider([LLMResponse(text=MID_SUM)]),
        budget_tokens=2_000,
        keep_tokens=1_200,
    )
    info = await compactor.maybe_compact(session)
    assert info is not None

    # The cut landed INSIDE the loop: the message right below the checkpoint
    # block is an assistant tool_use — impossible under the old user-text rule
    # (the only user text after the opening was the loop's own first row).
    first_tail = session.messages[3]
    assert first_tail.role == "assistant" and first_tail.tool_calls
    # Every assistant tool_call id in the sent view still has its matching
    # tool_result immediately after it.
    assert_pairs_intact(session.messages)

    row = await store.load_checkpoint(session.id)
    assert row["anchor_idx"] > 2  # inside the loop, not at its user message
    stored = await store.load_messages(session.id)
    assert stored[row["anchor_idx"]].tool_calls  # the cut row is a step start
    assert stored[row["anchor_idx"] + 1].role == "tool"  # and its pair follows
    assert row["covers_upto_idx"] == 2  # the opening is the first exchange

    # And the resumed view rebuilds the same thing, pairings intact.
    resumed = await Session.resume(store, session.id)
    assert deep_snapshot(resumed) == deep_snapshot(session)
    assert_pairs_intact(resumed.messages)


async def test_first_turn_over_budget_is_a_no_op_not_a_crash():
    """A session whose whole history is ONE turn (user + a long tool loop, no
    second user message) has no opening/tail split: the opening IS the whole
    first exchange, so nothing is elidable. maybe_compact must return None
    (today's behavior) — not IndexError, which is awaited UNGUARDED in
    Agent._step_chat and would kill the turn."""
    session = Session()
    session.messages = [Message(role="user", text="open the big job " + "o" * 500)]
    for i in range(30):
        session.messages.append(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id=f"c{i}", name="grep", arguments={"q": str(i)})],
            )
        )
        session.messages.append(
            Message(
                role="tool",
                tool_results=[ToolResult(tool_call_id=f"c{i}", content="z" * 1_500)],
            )
        )
    session.messages.append(Message(role="assistant", text="done " + "d" * 200))
    before = [m.text for m in session.messages]

    compactor = Compactor(summarizer=None, budget_tokens=2_000, keep_tokens=1_000)
    assert await compactor.maybe_compact(session) is None
    assert [m.text for m in session.messages] == before  # view untouched
    assert_pairs_intact(session.messages)

    # The same shape with a single message: one user turn over budget.
    lone = Session()
    lone.messages = [Message(role="user", text="huge " + "h" * 20_000)]
    assert await compactor.maybe_compact(lone) is None
    assert len(lone.messages) == 1


async def test_compaction_at_a_step_boundary_mid_tool_loop():
    """maybe_compact runs before EVERY model call — including mid tool-loop.
    A step-boundary cut there must keep the in-flight exchange whole and must
    never put the block directly before a lone tool_result."""
    session = Session()
    session.messages = [
        Message(role="user", text="task " + "t" * 400),
        Message(role="assistant", text="ok " + "o" * 400),
        Message(role="user", text="big job"),
    ]
    for i in range(8):
        session.messages.append(
            Message(
                role="assistant",
                tool_calls=[ToolCall(id=f"s{i}", name="grep", arguments={"q": str(i)})],
            )
        )
        session.messages.append(
            Message(
                role="tool",
                tool_results=[ToolResult(tool_call_id=f"s{i}", content="r" * 500)],
            )
        )
    compactor = Compactor(summarizer=None, budget_tokens=1_000, keep_tokens=700)
    info = await compactor.maybe_compact(session)
    assert info is not None
    assert_pairs_intact(session.messages)
    for i, m in enumerate(session.messages):
        if m.text and m.text.startswith("<context_checkpoint>"):
            assert session.messages[i + 1].role == "assistant"
            assert session.messages[i + 1].tool_calls


# --- opening freeze mechanics -------------------------------------------------


async def test_recompaction_never_refreezes_the_opening(db):
    """Re-truncating an already-truncated result would nest markers and change
    the opening bytes — the exact cache-busting this quest kills. The live
    re-compaction must pass the opening through untouched."""
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("task " + "t" * 100)
    await session.add_step(
        None,
        [ToolCall(id="x", name="read_file", arguments={"p": "1"})],
        [ToolResult(tool_call_id="x", content="R" * 9_000)],
    )
    await session.add_assistant("noted " + "n" * 100)
    for i in range(10):
        await session.add_user(f"u{i} " + "x" * 600)
        await session.add_assistant(f"a{i} " + "y" * 600)

    compactor = Compactor(summarizer=None, budget_tokens=1_000, keep_tokens=300)
    assert await compactor.maybe_compact(session) is not None
    frozen = [m for m in session.messages if m.role == "tool"][0]
    assert frozen.tool_results[0].content.count(MARKER) == 1

    for i in range(10, 20):
        await session.add_user(f"u{i} " + "x" * 600)
        await session.add_assistant(f"a{i} " + "y" * 600)
    compactor2 = Compactor(summarizer=None, budget_tokens=1_000, keep_tokens=300)
    assert await compactor2.maybe_compact(session) is not None

    refrozen = [m for m in session.messages if m.role == "tool"][0]
    assert refrozen is frozen  # same object, passed through untouched
    assert refrozen.tool_results[0].content.count(MARKER) == 1
    row = await store.load_checkpoint(session.id)
    # The opening stays frozen across compactions.
    assert row["covers_upto_idx"] == 4
