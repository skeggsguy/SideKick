"""Orchestrator: events become runs with the right autonomy and context."""

from __future__ import annotations

import asyncio

import pytest

from harness.core.approvals import ApprovalWaiter
from harness.core.events import ApprovalDecision, CronFired, EventBus, WatchHit
from harness.core.orchestrator import Orchestrator
from harness.core.policy import Autonomy
from harness.store.approvals import ApprovalStore
from harness.store.sessions import SessionStore
from conftest import gotcha


class FakeAgent:
    """Records run_turn calls; optionally slow or failing."""

    def __init__(self, log: list, delay: float = 0, fail: bool = False) -> None:
        self.log = log
        self.delay = delay
        self.fail = fail

    async def run_turn(self, session, user_input, **kwargs):
        self.log.append({"input": user_input, "session": session, **kwargs})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("boom")
        return "briefing text"


class StreamingFakeAgent:
    """Calls the streaming hooks the orchestrator wires onto it, like the real
    loop: thinking, then a text delta, a tool call + result, a final delta."""

    def __init__(self, log: list) -> None:
        self.log = log
        self.on_text_delta = lambda t: None
        self.on_tool_call = lambda n, a: None
        self.on_tool_result = lambda n, c, e: None
        self.on_thinking_delta = lambda t: None
        self.on_thinking_done = lambda: None
        self.on_truncated = lambda: None

    async def run_turn(self, session, user_input, **kwargs):
        self.log.append({"input": user_input, "session": session, **kwargs})
        self.on_thinking_delta("hmm, prune stale facts")
        self.on_thinking_done()
        self.on_text_delta("Dropping ")
        self.on_tool_call("forget", {"fact_ids": [3, 7]})
        self.on_tool_result("forget", "Forgot 2 facts: [3, 7].", False)
        self.on_text_delta("done.")
        return "pruned 2 facts"


def make_orchestrator(db, log, on_complete=None, on_event=None, agent=None, **kw):
    factory_calls = []

    def factory(autonomy: Autonomy):
        factory_calls.append(autonomy)
        return agent or FakeAgent(log, **kw)

    orchestrator = Orchestrator(
        bus=EventBus(),
        sessions=SessionStore(db),
        agent_factory=factory,
        waiter=ApprovalWaiter(ApprovalStore(db)),
        on_run_complete=on_complete,
        on_run_event=on_event,
    )
    return orchestrator, factory_calls


async def test_cron_event_runs_playbook_supervised(db):
    log, completed = [], []

    async def on_complete(title, text, channel, session_id):
        completed.append((title, text, channel, session_id))

    orchestrator, factory_calls = make_orchestrator(db, log, on_complete)
    await orchestrator.handle(
        CronFired(trigger_id="t1", playbook="morning_briefing", late=True)
    )
    await orchestrator.drain()

    assert factory_calls == [Autonomy.SUPERVISED]
    (run,) = log
    assert "morning briefing" in run["input"]  # playbook file content
    assert "LATE catch-up" in run["input"]
    assert run["trigger"] == "cron:t1"
    assert run["playbook"] == "morning_briefing"
    # Scheduled runs never fire a completion push — jobs alert via notify.
    assert completed == []


async def test_inline_reminder_prompt_used_verbatim(db):
    log = []
    orchestrator, _ = make_orchestrator(db, log)
    await orchestrator.handle(
        CronFired(trigger_id="t2", playbook="Remind me: submit the report")
    )
    await orchestrator.drain()
    assert "Remind me: submit the report" in log[0]["input"]
    assert log[0]["playbook"] is None


async def test_job_label_titles_the_session(db):
    """A labelled job's session is named by the label, not the prompt's first
    line — self-contained jobs have no playbook name to fall back on."""
    log = []
    orchestrator, _ = make_orchestrator(db, log)
    await orchestrator.handle(
        CronFired(
            trigger_id="t9",
            playbook="Review the week and plan the next check-in.",
            label="Weekly review",
        )
    )
    await orchestrator.drain()
    (row,) = await SessionStore(db).list()
    assert row["title"] == "Weekly review"  # session/chat title


async def test_autonomous_cron_gets_hard_allocation(db):
    log = []
    orchestrator, factory_calls = make_orchestrator(db, log)
    await orchestrator.handle(
        CronFired(
            trigger_id="t3",
            playbook="Do the weekly review and plan the next one.",
            autonomy="autonomous",
            tools=["calendar_event_create", "schedule_edit"],
        )
    )
    await orchestrator.drain()
    assert factory_calls == [Autonomy.AUTONOMOUS]
    assert log[0]["tool_profile"] == ["calendar_event_create", "schedule_edit"]
    # An autonomous job's allocation is its boundary, and since search_tools
    # was retired (2026-08-11) every profile is closed by construction —
    # there is no longer a per-run kwarg that could reopen one.
    assert "hard_profile" not in log[0]


async def test_row_tools_override_playbook_frontmatter(db):
    """A trigger row's allocation beats the named playbook's tools: line."""
    log = []
    orchestrator, _ = make_orchestrator(db, log)
    await orchestrator.handle(
        CronFired(
            trigger_id="t4",
            playbook="morning_briefing",
            tools=["gmail_search"],
        )
    )
    await orchestrator.drain()
    assert log[0]["tool_profile"] == ["gmail_search"], (
        "a trigger row's tools allocation BEATS playbook frontmatter "
        "(playbooks.resolve_job); only legacy name-rows read frontmatter"
    )


async def test_unsafe_rows_downgrade_to_supervised(db, capsys):
    """Hand-edited rows fail toward fewer privileges: unknown autonomy and
    autonomous-without-tools both run supervised (with a stderr note)."""
    log = []
    orchestrator, factory_calls = make_orchestrator(db, log)
    await orchestrator.handle(
        CronFired(trigger_id="t5", playbook="p", autonomy="yolo")
    )
    await orchestrator.handle(
        CronFired(trigger_id="t6", playbook="p", autonomy="autonomous", tools=None)
    )
    await orchestrator.drain()
    assert factory_calls == [Autonomy.SUPERVISED, Autonomy.SUPERVISED]
    err = capsys.readouterr().err
    assert "t5" in err and "t6" in err


async def test_approval_decision_resolves_waiter(db):
    log = []
    orchestrator, _ = make_orchestrator(db, log)
    request = asyncio.ensure_future(
        orchestrator.waiter.request("run1", "gmail_send", {})
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if (pending := await orchestrator.waiter.store.pending()):
            break

    await orchestrator.handle(ApprovalDecision(pending[0].id, approved=True))
    assert await request is True


async def test_concurrency_capped_at_two(db):
    log = []
    orchestrator, _ = make_orchestrator(db, log, delay=0.05)
    for i in range(4):
        await orchestrator.handle(CronFired(trigger_id=f"t{i}", playbook="p"))

    await asyncio.sleep(0.02)  # first wave running, rest queued on semaphore
    assert len(log) == 2
    await orchestrator.drain()
    assert len(log) == 4


async def test_background_run_broadcasts_live_events(db):
    log, events = [], []
    agent = StreamingFakeAgent(log)
    orchestrator, _ = make_orchestrator(db, log, on_event=events.append, agent=agent)

    await orchestrator.handle(CronFired(trigger_id="t1", playbook="memory_hygiene"))
    await orchestrator.drain()

    kinds = [e["type"] for e in events]
    assert kinds == [
        "run_start",
        "thinking_delta",
        "thinking_done",
        "delta",
        "tool_call",
        "tool_result",
        "delta",
        "run_done",
    ]
    # Every event is tagged with the run's session so the UI routes it.
    sid = log[0]["session"].id
    assert {e["session_id"] for e in events} == {sid}
    start = events[0]
    assert start["title"] == "memory_hygiene" and start["trigger"] == "cron:t1"
    thinking = next(e for e in events if e["type"] == "thinking_delta")
    assert thinking["text"] == "hmm, prune stale facts"
    tool_call = next(e for e in events if e["type"] == "tool_call")
    assert tool_call["tool"] == "forget" and tool_call["args"] == {"fact_ids": [3, 7]}
    assert events[-1]["text"] == "pruned 2 facts"


async def test_run_done_emitted_even_when_run_fails(db):
    log, events = [], []
    orchestrator, _ = make_orchestrator(db, log, on_event=events.append, fail=True)
    await orchestrator.handle(CronFired(trigger_id="t1", playbook="p"))
    await orchestrator.drain()

    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_done"
    assert events[-1]["text"] == ""  # no final text on failure


async def test_no_run_event_hook_is_a_noop(db):
    # The CLI/tests wire no hub; background runs must still work untouched.
    log = []
    orchestrator, _ = make_orchestrator(db, log)  # on_event=None
    await orchestrator.handle(CronFired(trigger_id="t1", playbook="p"))
    await orchestrator.drain()
    assert len(log) == 1


async def test_failed_run_is_surfaced_not_silent(db, capsys):
    log = []
    orchestrator, _ = make_orchestrator(db, log, fail=True)
    await orchestrator.handle(CronFired(trigger_id="t1", playbook="p"))
    await orchestrator.drain()
    err = capsys.readouterr().err
    assert "run failed" in err and "boom" in err


async def test_watch_run_broadcasts_live_events(db):
    """Watch events must emit run_start/run_done through the broadcaster or
    they are invisible until completion. Background runs include watcher hits
    — the pattern must be tested alongside cron hits.

    Pinned in tests/test_orchestrator.py."""
    log, events = [], []
    agent = StreamingFakeAgent(log)
    orchestrator, _ = make_orchestrator(db, log, on_event=events.append, agent=agent)

    await orchestrator.handle(WatchHit(watcher="gmail"))
    await orchestrator.drain()

    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_start", gotcha(
        "wire-live-watch-path",
        "background watch runs must emit run_start first or they are invisible until completion"
    )
    assert kinds[-1] == "run_done", gotcha(
        "wire-live-watch-path",
        "background watch runs must emit run_done last or they are invisible until completion"
    )
    start = events[0]
    assert start["trigger"] == "watch:gmail", gotcha(
        "wire-live-watch-path",
        "watch run event trigger must be watch:<watcher_name>"
    )


async def test_watch_run_fires_completion_push(db):
    """Watcher completions still push — only scheduled (cron) runs dropped
    the automatic completion push (jobs alert explicitly via notify)."""
    log, completed = [], []

    async def on_complete(title, text, channel, session_id):
        completed.append((title, text, channel, session_id))

    orchestrator, _ = make_orchestrator(db, log, on_complete)
    await orchestrator.handle(
        WatchHit(watcher="gmail", payload={"label": "urgent", "subject": "Boiler"})
    )
    await orchestrator.drain()

    ((title, text, channel, session_id),) = completed
    assert title == "Urgent email: Boiler"
    assert (text, channel) == ("briefing text", "web")
    assert session_id == log[0]["session"].id


# -- background runs own their session (BK-013) --------------------------------


class OwnershipProbe:
    """Records whether its session was registered as background-owned at the
    moment run_turn ran; then returns, raises, or hangs until cancelled."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.orchestrator: Orchestrator | None = None
        self.session_id: str | None = None
        self.owned_during_turn: bool | None = None
        self.started = asyncio.Event()

    async def run_turn(self, session, user_input, **kwargs):
        self.session_id = session.id
        self.owned_during_turn = session.id in self.orchestrator.background_sessions
        self.started.set()
        if self.mode == "fail":
            raise RuntimeError("boom")
        if self.mode == "hang":
            await asyncio.Event().wait()
        return "done"


def _probe_orchestrator(db, mode, on_complete=None):
    probe = OwnershipProbe(mode)
    orchestrator, _ = make_orchestrator(db, [], on_complete, agent=probe)
    probe.orchestrator = orchestrator
    return orchestrator, probe


_CRON = CronFired(trigger_id="t1", playbook="Remind me: water the plants")
_WATCH = WatchHit(watcher="gmail", payload={"label": "urgent", "subject": "Boiler"})


@pytest.mark.parametrize("event", [_CRON, _WATCH], ids=["cron", "watch"])
@pytest.mark.parametrize("mode", ["ok", "fail"])
async def test_background_run_owns_its_session_while_running(db, event, mode):
    """The 2026-10-25 collision: a chat resumed a session a cron run was still
    writing (two Session objects, two _next_idx counters → UNIQUE failure).
    The run's session id is in background_sessions for the whole turn — that
    set is what the chat route's 409 reads — and leaves it on return/raise."""
    orchestrator, probe = _probe_orchestrator(db, mode)
    await orchestrator.handle(event)
    await orchestrator.drain()

    assert probe.owned_during_turn is True
    assert probe.session_id not in orchestrator.background_sessions
    assert orchestrator.background_sessions == set()


async def test_cancelled_background_run_releases_its_session_silently(db):
    """Stop/shutdown cancels a run: ownership is released and NO failure push
    fires (CancelledError is not a failure)."""
    completed = []

    async def on_complete(*args):
        completed.append(args)

    for event in (_CRON, _WATCH):
        orchestrator, probe = _probe_orchestrator(db, "hang", on_complete)
        await orchestrator.handle(event)
        await asyncio.wait_for(probe.started.wait(), timeout=2)
        assert probe.session_id in orchestrator.background_sessions
        orchestrator.cancel_inflight()
        await orchestrator.drain()
        assert orchestrator.background_sessions == set()
    assert completed == []


@pytest.mark.parametrize("event", [_CRON, _WATCH], ids=["cron", "watch"])
async def test_failed_background_run_pushes_once_and_still_raises(db, event):
    """The property run died silently. A raising run now pushes exactly one
    "run failed" notification (deep-linked to its session) and the exception
    still propagates — _spawn's traceback print and the runs row's
    status=error stay as they were. A watcher's success push never fires."""
    completed = []

    async def on_complete(title, text, channel, session_id):
        completed.append((title, text, channel, session_id))

    orchestrator, probe = _probe_orchestrator(db, "fail", on_complete)
    run = (
        orchestrator._scheduled_run(event)
        if isinstance(event, CronFired)
        else orchestrator._watch_run(event)
    )
    with pytest.raises(RuntimeError, match="boom"):
        await run

    ((title, text, channel, session_id),) = completed
    assert title.endswith(" — run failed")
    assert text == "RuntimeError: boom"
    assert channel == "web"
    assert session_id == probe.session_id


async def test_failure_push_body_is_capped(db):
    completed = []

    async def on_complete(title, text, channel, session_id):
        completed.append(text)

    class LongFailure(OwnershipProbe):
        async def run_turn(self, session, user_input, **kwargs):
            raise ValueError("x" * 1000)

    probe = LongFailure()
    orchestrator, _ = make_orchestrator(db, [], on_complete, agent=probe)
    with pytest.raises(ValueError):
        await orchestrator._scheduled_run(_CRON)
    assert completed == [("ValueError: " + "x" * 1000)[:200]]


class PersistingFakeAgent(StreamingFakeAgent):
    """The real loop's order: persist the user turn, THEN call the model
    (on_model_call), then stream — optionally pausing in the model wait."""

    def __init__(self, log: list, order: list, wait: float = 0) -> None:
        super().__init__(log)
        self.order = order
        self.wait = wait
        self.on_model_call = None
        self.on_tool_start = None

    async def run_turn(self, session, user_input, **kwargs):
        self.log.append({"input": user_input, "session": session, **kwargs})
        await session.add_user(user_input)
        self.order.append("persisted")
        self.on_model_call()
        if self.wait:
            await asyncio.sleep(self.wait)
        self.on_tool_start("web_search")
        self.on_text_delta("done.")
        return "ok"


@pytest.mark.parametrize("event", [
    CronFired(trigger_id="t1", playbook="p"), WatchHit(watcher="gmail"),
])
async def test_run_start_follows_the_user_turn_persist(db, event):
    """BK-041 G2a: an auto-opened chat (Fire now) fetches /messages on
    run_start — published before the prompt is stored, it renders an empty
    session and the blue prompt never appears until the run ends."""
    log, order = [], []
    agent = PersistingFakeAgent(log, order)
    orchestrator, _ = make_orchestrator(
        db, log, on_event=lambda e: order.append(e["type"]), agent=agent
    )
    await orchestrator.handle(event)
    await orchestrator.drain()
    assert order.index("run_start") > order.index("persisted"), gotcha(
        "bg-run-start-after-persist",
        f"run_start must publish AFTER the user turn is stored: {order}",
    )
    assert order[-1] == "run_done" and order.count("run_start") == 1


async def test_run_start_still_fires_when_no_model_call_happens(db):
    # A run that dies before its first model call still opens and closes.
    log, events = [], []
    orchestrator, _ = make_orchestrator(db, log, on_event=events.append, fail=True)
    await orchestrator.handle(CronFired(trigger_id="t1", playbook="p"))
    await orchestrator.drain()
    assert [e["type"] for e in events] == ["run_start", "run_done"]


async def test_bg_run_reports_model_wait_status_and_tool_start(db):
    """BK-041 G2b/G3: a bg run's row gets the same phases a chat's does —
    model_wait per model call, the SlotWatcher's busy/reading status while
    it waits, tool_start when a tool_use block begins. All session-tagged."""
    from harness.gateway.model_status import SlotWatcher

    async def fetch():
        return [{"id_task": 9, "is_processing": True, "n_prompt_tokens": 100,
                 "n_prompt_tokens_cache": 50, "n_prompt_tokens_processed": 0}]

    log, events, order = [], [], []
    agent = PersistingFakeAgent(log, order, wait=0.1)
    orchestrator = Orchestrator(
        bus=EventBus(), sessions=SessionStore(db),
        agent_factory=lambda _a: agent,
        waiter=ApprovalWaiter(ApprovalStore(db)),
        on_run_event=events.append,
        slot_watcher=lambda emit: SlotWatcher(fetch, emit, interval=0.01),
    )
    await orchestrator.handle(CronFired(trigger_id="t1", playbook="p"))
    await orchestrator.drain()
    kinds = [e["type"] for e in events]
    assert kinds[:3] == ["run_start", "model_wait", "status"], kinds
    assert events[2]["phase"] == "reading" and events[2]["pct"] == 50
    assert kinds[-3:] == ["tool_start", "delta", "run_done"], kinds
    assert next(e for e in events if e["type"] == "tool_start")["tool"] == "web_search"
    sid = log[0]["session"].id
    assert {e["session_id"] for e in events} == {sid}
    await asyncio.sleep(0.05)  # the watcher was cancelled with the run
    assert kinds == [e["type"] for e in events]
