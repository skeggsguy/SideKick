"""Trigger, approval and session stores against a real migrated database."""

from __future__ import annotations

import dataclasses
import json

from harness.store.approvals import ApprovalStore, derive_source, why_gated
from harness.store.audit import AuditStore
from harness.store.triggers import TriggerStore
from conftest import gotcha

# history() intentionally omits predicted_p: the forecast is surfaced via
# ApprovalStore.stats() / the calibration tool, not the history payload.
# Any OTHER Approval field missing here is the gotcha firing — fix history(),
# don't grow this set without a decision.
INTENTIONAL_HISTORY_OMISSIONS = {"predicted_p"}


async def test_audit_log_row_shape(db):
    """The append-only audit trail: one log() call, one fully-populated row."""
    store = AuditStore(db)
    await store.log(
        "run1",
        "gmail_send",
        {"to": "x@example.com"},
        "OUTWARD",
        "approved",
        "digest123",
        is_error=False,
    )

    cur = await db.conn.execute("SELECT * FROM audit_log")
    rows = await cur.fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run1"
    assert row["tool"] == "gmail_send"
    assert json.loads(row["args_json"]) == {"to": "x@example.com"}
    assert row["classification"] == "OUTWARD"
    assert row["decision"] == "approved"
    assert row["result_digest"] == "digest123"
    assert row["is_error"] == 0
    assert row["ts"]  # timestamp populated


async def test_trigger_round_trip(db):
    store = TriggerStore(db)
    tid = await store.add("cron", "30 7 * * 1-5", "morning_briefing")

    t = await store.get(tid)
    assert t is not None
    assert t.kind == "cron"
    assert t.enabled and not t.one_shot
    assert t.last_fired_at is None

    await store.mark_fired(tid)
    t = await store.get(tid)
    assert t.last_fired_at is not None
    assert t.enabled  # repeating triggers stay enabled

    await store.delete(tid)
    assert await store.get(tid) is None


async def test_trigger_label_and_partial_update(db):
    store = TriggerStore(db)
    tid = await store.add("cron", "0 9 * * *", "morning_briefing", label="Briefing")

    t = await store.get(tid)
    assert t.label == "Briefing"

    # Partial update: only the passed fields change; label omitted stays put.
    await store.update(tid, spec="0 10 * * *", one_shot=True)
    t = await store.get(tid)
    assert t.spec == "0 10 * * *"
    assert t.one_shot is True
    assert t.label == "Briefing"  # untouched
    assert t.playbook == "morning_briefing"  # untouched

    # label=None explicitly clears it (distinct from omitting the arg).
    await store.update(tid, label=None)
    t = await store.get(tid)
    assert t.label is None

    # No fields → no-op, no crash.
    await store.update(tid)
    assert (await store.get(tid)).spec == "0 10 * * *"


async def test_one_shot_trigger_disables_after_firing(db):
    store = TriggerStore(db)
    tid = await store.add(
        "cron", "0 16 13 6 *", "Remind me: submit the report", one_shot=True
    )
    await store.mark_fired(tid)
    t = await store.get(tid)
    assert not t.enabled
    assert (await store.list(enabled_only=True)) == []


async def test_approval_lifecycle(db):
    store = ApprovalStore(db)
    aid = await store.create("run123", "gmail_send", {"to": "x@y.z"})

    (pending,) = await store.pending()
    assert pending.id == aid
    assert pending.args == {"to": "x@y.z"}
    # Orphan run_id (no runs row) must degrade safely, not crash the joins.
    assert pending.source_kind == "unknown"

    assert await store.resolve(aid, "approved") is True
    assert await store.pending() == []
    assert (await store.get(aid)).status == "approved"

    # A second decision must not overwrite the first.
    assert await store.resolve(aid, "denied") is False
    assert (await store.get(aid)).status == "approved"


# ---- approval source / why-gated derivation ----------------------------------

def test_derive_source_table():
    # (run_trigger, label, autonomy) -> (kind, label, effective_autonomy)
    assert derive_source("user", None, None) == (
        "chat", "You asked for this in chat", "interactive")
    kind, label, autonomy = derive_source("cron:abc123", "Morning briefing", "supervised")
    assert (kind, autonomy) == ("scheduled", "supervised")
    assert label == "Scheduled run — Morning briefing"
    # Cron trigger deleted after the run: label degrades, autonomy assumes supervised.
    assert derive_source("cron:gone", None, None)[1] == "Scheduled run"
    # Interactive-autonomy cron MUST carry interactive through (why-copy depends on it).
    assert derive_source("cron:abc123", "X", "interactive")[2] == "interactive"
    kind, label, autonomy = derive_source("watch:gmail", None, None)
    assert (kind, autonomy) == ("watch", "supervised")
    assert "incoming email" in label
    assert derive_source("watch:foo", None, None)[1] == "Triggered by a background watcher (foo)"
    # The `delegate` TOOL was retired 2026-08-11, but the audit log is
    # append-only and real runs still carry trigger="delegate:<id>". This
    # branch interprets that history — it is NOT dead code.
    assert derive_source("delegate:r1", None, None)[0] == "delegate", gotcha(
        "delegate-history-interpreters",
        "derive_source must still map a legacy 'delegate:<id>' trigger to the "
        "'delegate' source kind — the tool is gone, its history is not (search_tools/delegate are "
        "retired; RunStore.summary and app.js's delegate regex read the same history — not dead code)",
    )
    assert derive_source(None, None, None) == (
        "unknown", "Origin unknown (run record missing)", "supervised")


def test_why_gated_copy():
    # READ_ONLY approval rows come from the four egress gates — the persisted
    # gate_kind picks the copy, so a budget-gated call never claims a secret
    # was read (the pre-gate_kind bug).
    assert "secret-shaped file" in why_gated(
        "READ_ONLY", "interactive", gate_kind="secret_read"
    )
    assert "private/internal network address" in why_gated(
        "READ_ONLY", "interactive", gate_kind="internal_probe"
    )
    assert "per-turn limit" in why_gated(
        "READ_ONLY", "interactive", gate_kind="egress_budget"
    )
    assert "wasn't named by you" in why_gated(
        "READ_ONLY", "interactive", gate_kind="new_destination"
    )
    # Rows from before the gate_kind column: generic guard copy, no guessing.
    assert "outbound-request guard" in why_gated("READ_ONLY", "interactive")
    assert "secret" not in why_gated("READ_ONLY", "interactive")
    assert "never be made automatic" in why_gated("OUTWARD", "supervised")
    assert "permanently removes" in why_gated("DESTRUCTIVE", "interactive")
    assert "always-allow list" in why_gated("MUTATING_LOCAL", "interactive")
    assert "attacker-influenced" in why_gated("MUTATING_LOCAL", "supervised")
    assert "attacker-influenced" in why_gated("MUTATING_LOCAL", "restricted")
    assert "autonomous job" in why_gated("MUTATING_LOCAL", "autonomous")
    assert why_gated(None, "supervised") == ""  # pre-migration rows: omit the line
    # Always-ask schedule tools get their own copy regardless of autonomy.
    for autonomy in ("interactive", "supervised", "autonomous"):
        assert "always ask" in why_gated(
            "MUTATING_LOCAL", autonomy, tool="schedule_edit"
        )


async def test_approval_source_joined_through_runs(db):
    from harness.store.runs import RunStore

    approvals = ApprovalStore(db)
    runs = RunStore(db)
    triggers = TriggerStore(db)

    # Chat-sourced: trigger='user' → interactive → not-on-allowlist copy.
    chat_run = await runs.start(None, "test-model", trigger="user")
    chat_aid = await approvals.create(chat_run, "remember", {"content": "x"}, "MUTATING_LOCAL")

    # Watch-sourced: static label + supervised parked-despite-allowlist copy.
    watch_run = await runs.start(None, "test-model", trigger="watch:gmail")
    watch_aid = await approvals.create(watch_run, "gmail_draft_reply", {}, "MUTATING_LOCAL")

    # Cron-sourced with a live labelled trigger row.
    tid = await triggers.add("cron", "0 9 * * *", "briefing", label="Briefing")
    cron_run = await runs.start(None, "test-model", trigger=f"cron:{tid}")
    cron_aid = await approvals.create(cron_run, "task_create", {}, "MUTATING_LOCAL")

    by_id = {a.id: a for a in await approvals.pending()}
    chat, watch, cron = by_id[chat_aid], by_id[watch_aid], by_id[cron_aid]

    assert chat.source_kind == "chat"
    assert chat.source_label == "You asked for this in chat"
    assert "always-allow list" in chat.why_gated

    assert watch.source_kind == "watch"
    assert "incoming email" in watch.source_label
    assert "attacker-influenced" in watch.why_gated

    assert cron.source_kind == "scheduled"
    assert cron.source_label == "Scheduled run — Briefing"
    assert "attacker-influenced" in cron.why_gated  # default supervised autonomy

    # get() carries the same joins as pending().
    got = await approvals.get(watch_aid)
    assert got.source_kind == "watch" and got.why_gated == watch.why_gated

    # history() (hand-rolled dicts) must carry the derived fields too.
    await approvals.resolve(watch_aid, "denied")
    hist = {h["id"]: h for h in await approvals.history()}
    assert hist[watch_aid]["source_kind"] == "watch"
    assert "incoming email" in hist[watch_aid]["source_label"]
    assert "attacker-influenced" in hist[watch_aid]["why_gated"]


async def test_approval_gate_kind_round_trip(db):
    """gate_kind persists through create() and survives pending(), get(),
    AND history()'s hand-rolled dict, and picks the matching why_gated copy."""
    from harness.store.runs import RunStore

    approvals = ApprovalStore(db)
    runs = RunStore(db)
    run = await runs.start(None, "test-model", trigger="cron:gone")

    budget_aid = await approvals.create(
        run, "web_search", {"query": "q"}, "READ_ONLY", gate_kind="egress_budget"
    )
    secret_aid = await approvals.create(
        run, "web_fetch", {"url": "https://x.test"}, "READ_ONLY",
        gate_kind="secret_read",
    )
    plain_aid = await approvals.create(run, "remember", {}, "MUTATING_LOCAL")

    by_id = {a.id: a for a in await approvals.pending()}
    assert by_id[budget_aid].gate_kind == "egress_budget"
    assert "per-turn limit" in by_id[budget_aid].why_gated
    assert "secret" not in by_id[budget_aid].why_gated
    assert by_id[secret_aid].gate_kind == "secret_read"
    assert "secret-shaped file" in by_id[secret_aid].why_gated
    assert by_id[plain_aid].gate_kind is None

    got = await approvals.get(budget_aid)
    assert got.gate_kind == "egress_budget"

    await approvals.resolve(budget_aid, "denied")
    hist = {h["id"]: h for h in await approvals.history()}
    assert hist[budget_aid]["gate_kind"] == "egress_budget"
    assert "per-turn limit" in hist[budget_aid]["why_gated"]


# ---- approval prediction (007) -----------------------------------------------

async def test_approval_prediction_column(db):
    store = ApprovalStore(db)
    aid = await store.create("run1", "gmail_draft_reply", {"x": 1}, "MUTATING_LOCAL")
    assert (await store.get(aid)).predicted_p is None

    await store.set_prediction(aid, 0.93)
    assert (await store.get(aid)).predicted_p == 0.93
    await store.set_prediction(aid, 7.0)  # clamped
    assert (await store.get(aid)).predicted_p == 1.0

    await store.resolve(aid, "approved")
    rows = await store.calibration_rows()
    assert len(rows) == 1
    assert rows[0]["tool"] == "gmail_draft_reply"
    assert rows[0]["status"] == "approved"
    # Unpredicted rows never reach the calibration set.
    bid = await store.create("run2", "edit_file", {}, "MUTATING_LOCAL")
    await store.resolve(bid, "denied")
    assert len(await store.calibration_rows()) == 1


# ---- approval history field parity ------------------------------------------

async def test_history_carries_every_approval_field(db):
    """The hand-rolled history() dict must carry every Approval field (except
    intentional omissions like predicted_p). A new field missing from history()
    is a silent bug — the API payload silently drops it, while pending() carries
    it, and the UI doesn't know.

    Pinned in tests/test_stores.py."""
    store = ApprovalStore(db)
    aid = await store.create("run1", "gmail_send", {"to": "x@y.z"})
    await store.resolve(aid, "approved")

    # Get the pending-side data and convert to a dict of keys.
    pending = (await store.pending()) or []
    if not pending:
        # Re-fetch after resolve — pending() won't return a resolved row.
        pending_row = await store.get(aid)
    else:
        pending_row = pending[0]
    pending_dict = dataclasses.asdict(pending_row)
    pending_keys = set(pending_dict.keys()) - INTENTIONAL_HISTORY_OMISSIONS

    # Get the history-side data.
    hist = await store.history()
    hist_by_id = {h["id"]: h for h in hist}
    hist_row = hist_by_id[aid]
    hist_keys = set(hist_row.keys())

    # Every pending key (minus omissions) must exist in history.
    missing = pending_keys - hist_keys
    assert not missing, gotcha(
        "approvals-history-field-parity",
        f"history() missing fields from Approval dataclass: {missing} — it builds "
        "a hand-rolled dict, so a new Approval field must be added there too or "
        "/api/approvals/history silently drops it"
    )


async def test_sessions_written_with_tool_result_images_still_load(db):
    """BK-001 (7d76449, reverted 2026-10-25) gave ToolResult an `images` field,
    and append_message persists asdict(message) — so every tool_result written
    while it was live carries an `images` key (usually []). The reverted
    ToolResult has no such field: ToolResult(**tr) would TypeError and every
    one of those sessions would fail to resume. _message_from_dict drops it."""
    from harness.core.session import Session
    from harness.core.types import ToolResult
    from harness.store.db import utcnow
    from harness.store.sessions import SessionStore

    store = SessionStore(db)
    sid = await store.create()
    rows = [
        {"role": "user", "text": "look at the listing"},
        {
            "role": "assistant", "text": None,
            "tool_calls": [{"id": "c1", "name": "web_fetch", "arguments": {"url": "u"}}],
        },
        {
            "role": "tool", "text": None,
            "tool_results": [
                {"tool_call_id": "c1", "content": "page", "is_error": False, "images": []},
                {"tool_call_id": "c2", "content": "shot", "is_error": False,
                 "images": [{"path": "/x/shot.png", "mime": "image/png"}]},
                {"tool_call_id": "c3", "content": "boom", "is_error": True, "images": None},
            ],
        },
    ]
    for idx, payload in enumerate(rows):
        await db.conn.execute(
            "INSERT INTO messages (session_id, idx, role, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, idx, payload["role"], json.dumps(payload), utcnow()),
        )
    await db.conn.commit()

    resumed = await Session.resume(store, sid)

    assert resumed is not None, gotcha(
        "tool-result-images-key",
        "a persisted `images` key broke session resume — rows written while "
        "BK-001 was live (2026-10-24/25) carry it; _message_from_dict must drop it",
    )
    assert resumed.messages[2].tool_results == [
        ToolResult(tool_call_id="c1", content="page"),
        ToolResult(tool_call_id="c2", content="shot"),
        ToolResult(tool_call_id="c3", content="boom", is_error=True),
    ]


async def test_sweep_orphans_marks_dead_daemon_runs_killed(db):
    """BK-014: a daemon killed mid-tool never runs agent.py's finish(), so its
    rows sat 'running' for months. The startup sweep marks every non-CLI
    'running' row killed with ended_at set; a CLI turn may legitimately span
    a daemon restart, so 'cli' rows are left alone, as are finished rows."""
    from harness.core.types import Usage
    from harness.store.runs import RunStore

    runs = RunStore(db)
    web_a = await runs.start(None, "m", trigger="cron:t1", channel="web")
    web_b = await runs.start(None, "m", trigger="user", channel="web")
    cli_live = await runs.start(None, "m", trigger="user", channel="cli")
    done_ok = await runs.start(None, "m", channel="web")
    await runs.finish(done_ok, "ok", Usage(), 0.0)
    done_err = await runs.start(None, "m", channel="web")
    await runs.finish(done_err, "error", Usage(), 0.0)
    before = {rid: await runs.get(rid) for rid in (done_ok, done_err)}

    assert await runs.sweep_orphans() == 2

    for rid in (web_a, web_b):
        row = await runs.get(rid)
        assert row["status"] == "killed"
        assert row["ended_at"] is not None
    cli_row = await runs.get(cli_live)
    assert (cli_row["status"], cli_row["ended_at"]) == ("running", None)
    for rid, row in before.items():
        assert await runs.get(rid) == row  # finished rows untouched

    # Idempotent: a second start finds nothing left to sweep.
    assert await runs.sweep_orphans() == 0
