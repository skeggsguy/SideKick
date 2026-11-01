"""Honest compaction trace + usage accounting + live event hooks (Step 3).

The gaps these pin: the compaction trace row used to carry no identity
(which checkpoint row? where is the seam?) and the summarizer's token usage
was silently dropped (`resp.usage` never read), so a compaction's real cost
was invisible in the run totals. And the live UI needs two events around the
slow summarizer call — `compaction_start` and `compaction` — wired through
the chat route's emit() so they carry a seq and enter the replay ring like
every other event (Step 4 paints them).

Pins:
- the run_events kind='compaction' row carries checkpoint_id, anchor_idx
  (TRUE store idx) and summary_usage, and keeps every pre-Step-3 key — the
  row shape grows additively, never in place;
- the summarizer's usage lands in the run totals (runs.input/output_tokens);
- the agent's on_compaction hook fires exactly ("start", None) then
  ("done", info) per compaction — never on a quiet step, never on a turn
  that is over budget but uncuttable (an orphan start would strand the
  spinner);
- a compacting chat turn emits exactly one compaction_start and one
  compaction SSE frame, monotonic-sequenced, ring-replayable, the compaction
  frame byte-equal to the trace row it mirrors.
"""

from __future__ import annotations

import asyncio

import pytest

from harness.core.agent import Agent
from harness.core.approvals import ApprovalWaiter
from harness.core.context import Compactor
from harness.core.events import EventBus
from harness.core.policy import PolicyEngine
from harness.core.registry import ToolRegistry
from harness.core.session import Session
from harness.core.types import LLMResponse, Message, ToolCall, Usage
from harness.gateway.app import Gateway
from harness.gateway.broadcast import RunBroadcaster
from harness.gateway.routes.chat_run import start_chat_run
from harness.store.approvals import ApprovalStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore

from test_agent_loop import EchoTool, ScriptedProvider

SUMMARY_USAGE = Usage(input_tokens=1234, output_tokens=567)

# Comfortable margins: a filled session is ~20x1030 chars ≈ 6.9k tokens,
# the compacted view ≈ 1.9k — one small follow-up turn never re-crosses the
# 5k budget, so "no events on a quiet turn" tests the hook, not the sizing.
BUDGET = 5_000
KEEP = 300


async def fill(session: Session, turns: int = 10, chars: int = 1_000) -> None:
    for i in range(turns):
        await session.add_user(f"question {i} about site{i}.example " + "x" * chars)
        await session.add_assistant(f"answer {i} " + "y" * chars)


def make_agent(
    compactor: Compactor,
    provider: ScriptedProvider,
    run_store: RunStore | None = None,
    registry: ToolRegistry | None = None,
) -> tuple[Agent, list[tuple]]:
    """A real Agent whose on_compaction hook records (phase, info) calls.
    Strict two-arg signature (no default): the documented OnCompaction
    protocol passes info at BOTH phases, so a "start" fired one-arg is a bug
    the fixture must catch, not tolerate."""
    phases: list[tuple] = []
    agent = Agent(
        provider=provider,
        registry=registry or ToolRegistry(),
        system_prompt="test",
        policy=PolicyEngine(),
        run_store=run_store,
        compactor=compactor,
    )
    agent.on_compaction = lambda phase, info: phases.append((phase, info))
    return agent, phases


def summarizer() -> ScriptedProvider:
    return ScriptedProvider([LLMResponse(text=(
        "THE EARLY PART: goals and decisions with their reasons, the exact "
        "paths, ids and dates the work depends on, the open items left in "
        "flight, and the preferences the user stated while they were being "
        "made, retained as substance rather than prose style."
    ), usage=SUMMARY_USAGE)])


async def compaction_event_row(runs: RunStore, run_id: str) -> dict:
    events = await runs.events(run_id)
    rows = [e for e in events if e["kind"] == "compaction"]
    assert len(rows) == 1
    return rows[0]["data"]


# --- (a) the trace row is honest ----------------------------------------------


async def test_compaction_trace_row_carries_id_anchor_and_usage(db):
    store = SessionStore(db)
    runs = RunStore(db)
    session = await Session.create(store)
    await fill(session)

    provider = ScriptedProvider(
        [LLMResponse(text="an answer"), LLMResponse(text="another answer")]
    )
    compactor = Compactor(summarizer=summarizer(), budget_tokens=BUDGET, keep_tokens=KEEP)
    agent, phases = make_agent(compactor, provider, runs)
    await agent.run_turn(session, "first question")
    run_id = agent.last_run["run_id"]

    row = await compaction_event_row(runs, run_id)
    cp_row = await store.load_checkpoint(session.id)
    # Step 3's additions, truthful against the store.
    assert row["checkpoint_id"] == cp_row["id"]
    assert row["anchor_idx"] == cp_row["anchor_idx"]  # TRUE STORE idx
    assert row["summary_usage"]["input_tokens"] == 1234
    assert row["summary_usage"]["output_tokens"] == 567
    # Backward compatibility: every pre-Step-3 key survives with its meaning.
    assert row["messages_summarized"] > 0
    assert row["est_tokens_before"] > row["est_tokens_after"]
    assert row["summarized"] is True
    assert row["summary_target"] > 0

    # The hook: exactly one start/done pair, done carrying the traced info.
    assert [p for p, _ in phases] == ["start", "done"]
    assert phases[0][1] is None
    assert phases[1][1] == row

    # A quiet turn (view back under budget) fires NOTHING — the pair belongs
    # to a compaction, not to a step.
    await agent.run_turn(session, "second question")
    assert [p for p, _ in phases] == ["start", "done"]
    assert await compaction_event_row(runs, run_id)  # first row untouched
    events2 = await runs.events(agent.last_run["run_id"])
    assert not [e for e in events2 if e["kind"] == "compaction"]


async def test_summarizer_usage_lands_in_the_run_totals(db):
    """The accounting gap: resp.usage from the summarizer used to vanish.
    The runs row must show chat tokens AND summary tokens."""
    store = SessionStore(db)
    runs = RunStore(db)
    session = await Session.create(store)
    await fill(session)

    provider = ScriptedProvider(
        [LLMResponse(text="an answer", usage=Usage(input_tokens=100, output_tokens=20))]
    )
    compactor = Compactor(summarizer=summarizer(), budget_tokens=BUDGET, keep_tokens=KEEP)
    agent, _ = make_agent(compactor, provider, runs)
    await agent.run_turn(session, "first question")

    run = await runs.get(agent.last_run["run_id"])
    assert run["input_tokens"] == 100 + 1234
    assert run["output_tokens"] == 20 + 567


async def test_summary_with_no_text_still_counts_its_tokens(db):
    """A summarizer call that returns no text is a FAILED summary — but the
    call still cost tokens (an empty reply after burning max_tokens is the
    expensive case, not the free one). Both the trace row and the run totals
    must carry that usage; only a compaction with NO call reports None."""
    store = SessionStore(db)
    runs = RunStore(db)
    session = await Session.create(store)
    await fill(session)

    provider = ScriptedProvider([LLMResponse(text="an answer")])
    compactor = Compactor(
        summarizer=ScriptedProvider(
            [LLMResponse(text="", usage=Usage(input_tokens=900, output_tokens=5))]
        ),
        budget_tokens=BUDGET,
        keep_tokens=KEEP,
    )
    agent, phases = make_agent(compactor, provider, runs)
    await agent.run_turn(session, "first question")

    row = await compaction_event_row(runs, agent.last_run["run_id"])
    assert row["summarized"] is False
    assert row["summary_error"] == "summarizer returned no text"
    assert row["summary_usage"]["input_tokens"] == 900
    assert row["summary_usage"]["output_tokens"] == 5
    run = await runs.get(agent.last_run["run_id"])
    assert run["input_tokens"] == 900
    assert run["output_tokens"] == 5
    assert [p for p, _ in phases] == ["start", "done"]  # a failed summary still ends


async def test_compaction_usage_survives_a_turn_that_then_errors(db):
    """The summary's tokens are folded into `total` BEFORE the model call, so
    a turn that dies right after compacting still reports them: the runs row
    is written from that same total in run_turn's finally. (The old code
    dropped the summarizer's usage entirely, so a failed turn lost it too.)"""
    store = SessionStore(db)
    runs = RunStore(db)
    session = await Session.create(store)
    await fill(session)

    class Dying(ScriptedProvider):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("model down")

    compactor = Compactor(summarizer=summarizer(), budget_tokens=BUDGET, keep_tokens=KEEP)
    agent, _ = make_agent(compactor, Dying([]), runs)
    with pytest.raises(RuntimeError):
        await agent.run_turn(session, "first question")

    run = await runs.get(agent.last_run["run_id"])
    assert run["status"] == "error"
    assert run["input_tokens"] == 1234  # the summary, despite the dead turn
    assert run["output_tokens"] == 567


async def test_hook_fires_exactly_one_pair_per_compaction_in_a_tool_loop(db):
    """maybe_compact runs EVERY step; a multi-step turn that compacts on
    step 0 must still produce ONE start and ONE done — and EchoTool keeps the
    turn alive past the compaction step."""
    store = SessionStore(db)
    runs = RunStore(db)
    session = await Session.create(store)
    await fill(session)

    registry = ToolRegistry()
    registry.register(EchoTool())
    provider = ScriptedProvider(
        [
            LLMResponse(text="", tool_calls=[ToolCall(id="t1", name="echo", arguments={"text": "go"})]),
            LLMResponse(text="all done"),
        ]
    )
    compactor = Compactor(summarizer=summarizer(), budget_tokens=BUDGET, keep_tokens=KEEP)
    agent, phases = make_agent(compactor, provider, runs, registry)
    await agent.run_turn(session, "first question")

    assert [p for p, _ in phases] == ["start", "done"]
    assert phases[1][1]["checkpoint_id"]  # step 0's compaction, reported once


async def test_uncuttable_over_budget_session_fires_no_events(db):
    """Over budget but nothing cuttable (the opening IS the whole history):
    maybe_compact returns None, so NOT A START may fire either — a pre-call
    hook would strand the live spinner exactly here."""
    session = Session()
    session.messages = [Message(role="user", text="x" * 300_000)]
    phases: list[tuple] = []
    compactor = Compactor(summarizer=None, budget_tokens=1_000, keep_tokens=300)

    info = await compactor.maybe_compact(session, on_compaction=lambda p, i=None: phases.append((p, i)))

    assert info is None
    assert phases == []


# --- (b) the live SSE wiring: seq + ring replay through emit() -----------------


@pytest.fixture
async def gw(db):
    return Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy, **kw: None,  # replaced per test
        broadcaster=RunBroadcaster(),
    )


async def test_compacting_chat_turn_emits_one_start_and_one_compaction_frame(gw):
    """Full stack: real Agent + real Compactor behind start_chat_run. The
    frames come through emit(), so they are seq-stamped, ring-replayable,
    and the compaction frame carries the trace payload untouched."""
    session = await Session.create(gw.sessions, channel="web")
    await fill(session)
    provider = ScriptedProvider(
        [LLMResponse(text="an answer", usage=Usage(input_tokens=100, output_tokens=20))]
    )

    def factory(autonomy, **kw):
        compactor = Compactor(
            summarizer=summarizer(), budget_tokens=BUDGET, keep_tokens=KEEP
        )
        return Agent(
            provider=provider,
            registry=ToolRegistry(),
            system_prompt="test",
            policy=PolicyEngine(),
            run_store=gw.runs,
            compactor=compactor,
        )

    gw.agent_factory = factory
    run = await start_chat_run(gw, "first question", session.id)
    await run.task
    assert run.task.exception() is None

    events = [e for e in _drain(run.queue) if e is not None]
    assert [e["type"] for e in events] == [
        "compaction_start", "compaction", "model_wait", "turn_complete", "done",
    ]
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(1, len(events) + 1))  # monotonic, dense, per run

    start, frame = events[0], events[1]
    assert start == {"type": "compaction_start", "seq": 1, "run_id": start["run_id"]}

    # The live frame and the persisted row are the SAME payload (Step 5 reads
    # the row to redraw the seam after a reload — they must not drift).
    row = await compaction_event_row(gw.runs, start["run_id"])
    assert {k: v for k, v in frame.items() if k not in ("type", "seq", "run_id")} == row
    cp_row = await gw.sessions.load_checkpoint(session.id)
    assert frame["checkpoint_id"] == cp_row["id"]
    assert frame["anchor_idx"] == cp_row["anchor_idx"]
    assert frame["summary_usage"]["input_tokens"] == 1234

    # Ring replay carries both frames with their delivered numbers.
    replayed = run.ring.replay(0)["events"]
    assert [(e["type"], e["seq"]) for e in replayed[:2]] == [
        ("compaction_start", 1), ("compaction", 2),
    ]

    # Usage accounting reached the runs row through the real path too.
    run_row = await gw.runs.get(start["run_id"])
    assert run_row["input_tokens"] == 100 + 1234


def test_compaction_event_frames():
    from harness.gateway.routes._sse import compaction_event

    assert compaction_event("start", None) == {"type": "compaction_start"}
    assert compaction_event("start", {"x": 1}) == {"type": "compaction_start"}
    assert compaction_event("done", {"anchor_idx": 7}) == {
        "type": "compaction", "anchor_idx": 7,
    }
    assert compaction_event("done", None) == {"type": "compaction"}


def _drain(q: asyncio.Queue) -> list[dict | None]:
    out: list[dict | None] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out
