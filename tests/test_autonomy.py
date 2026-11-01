"""Autonomy levels and deferred approvals."""

from __future__ import annotations

import asyncio

import pytest

from harness.core.approvals import ApprovalWaiter
from harness.core.policy import ActionClass, Autonomy, PolicyEngine, deferred_approver
from harness.store.approvals import ApprovalStore
from harness.store.audit import AllowlistStore


async def test_restricted_denies_everything_gated():
    policy = PolicyEngine(autonomy=Autonomy.RESTRICTED)
    for cls in (
        ActionClass.MUTATING_LOCAL,
        ActionClass.OUTWARD,
        ActionClass.DESTRUCTIVE,
    ):
        decision, reason = await policy.check("t", {}, cls)
        assert decision == "denied"
        assert "restricted" in reason
    decision, _ = await policy.check("t", {}, ActionClass.READ_ONLY)
    assert decision == "auto"


async def test_supervised_denies_destructive_even_with_approver():
    policy = PolicyEngine(autonomy=Autonomy.SUPERVISED)

    async def never_called(tool, args, cls):
        raise AssertionError("approver must not be consulted for DESTRUCTIVE")

    policy.approver = never_called
    decision, reason = await policy.check("rm_rf", {}, ActionClass.DESTRUCTIVE)
    assert decision == "denied"
    assert "unattended" in reason


async def test_supervised_ignores_allowlist(db):
    # Reversal of the pre-v3 behavior: the allowlist is an interactive-chat
    # convenience and must not auto-fire in supervised runs, whose triggers can
    # be attacker-influenced (injected email → triage → allowlisted draft).
    allowlist = AllowlistStore(db)
    await allowlist.add("task_create")
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.SUPERVISED)
    decision, reason = await policy.check("task_create", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "denied"  # no approver wired → gated call denied
    assert "no approver" in reason


async def test_outward_never_allowlisted(db):
    # An OUTWARD send must never auto-fire, even with a matching allowlist row
    # and no approver — a human always sees the full content first.
    allowlist = AllowlistStore(db)
    await allowlist.add("gmail_send")
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.SUPERVISED)
    decision, reason = await policy.check("gmail_send", {}, ActionClass.OUTWARD)
    assert decision == "denied"
    assert "no approver" in reason


async def test_deferred_approval_approved(db):
    waiter = ApprovalWaiter(ApprovalStore(db))
    policy = PolicyEngine(autonomy=Autonomy.SUPERVISED)
    policy.approver = deferred_approver(waiter, lambda: "run42")

    check = asyncio.ensure_future(
        policy.check("gmail_send", {"to": "a@b.c"}, ActionClass.OUTWARD)
    )
    # Let the check park itself on a pending approval, then decide it.
    for _ in range(50):
        await asyncio.sleep(0)
        if (pending := await waiter.store.pending()):
            break
    assert pending[0].run_id == "run42"
    assert await waiter.resolve(pending[0].id, approved=True)

    decision, _ = await check
    assert decision == "approved"
    assert (await waiter.store.get(pending[0].id)).status == "approved"


async def test_deferred_approval_denied(db):
    waiter = ApprovalWaiter(ApprovalStore(db))
    policy = PolicyEngine(autonomy=Autonomy.SUPERVISED)
    policy.approver = deferred_approver(waiter, lambda: None)

    check = asyncio.ensure_future(
        policy.check("gmail_send", {}, ActionClass.OUTWARD)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if (pending := await waiter.store.pending()):
            break
    await waiter.resolve(pending[0].id, approved=False)

    decision, reason = await check
    assert decision == "denied"
    assert reason == "denied by user"


async def test_schedule_edit_parks_deferred_in_background_run(db):
    """The self-rescheduling path: a background job (supervised OR autonomous)
    calling schedule_edit parks a deferred approval — a human decides, but
    the call is never hard-denied like DESTRUCTIVE would be."""
    for autonomy in (Autonomy.SUPERVISED, Autonomy.AUTONOMOUS):
        waiter = ApprovalWaiter(ApprovalStore(db))
        policy = PolicyEngine(autonomy=autonomy)
        policy.approver = deferred_approver(waiter, lambda: "run7")

        check = asyncio.ensure_future(
            policy.check(
                "schedule_edit",
                {"trigger_id": "abc", "cron": "0 9 3 8 *"},
                ActionClass.MUTATING_LOCAL,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0)
            if (pending := await waiter.store.pending()):
                break
        assert pending[0].tool == "schedule_edit"
        await waiter.resolve(pending[0].id, approved=True)
        decision, _ = await check
        assert decision == "approved"


async def test_deferred_approval_timeout_expires(db):
    waiter = ApprovalWaiter(ApprovalStore(db), timeout_s=0.05)
    approved = await waiter.request("run1", "gmail_send", {})
    assert approved is False
    cur = await db.conn.execute("SELECT status FROM approvals")
    assert (await cur.fetchone())["status"] == "expired"


async def test_double_decision_rejected(db):
    waiter = ApprovalWaiter(ApprovalStore(db))
    request = asyncio.ensure_future(waiter.request(None, "t", {}))
    for _ in range(50):
        await asyncio.sleep(0)
        if (pending := await waiter.store.pending()):
            break
    assert await waiter.resolve(pending[0].id, approved=True) is True
    assert await waiter.resolve(pending[0].id, approved=False) is False
    assert await request is True


async def test_approval_classification_round_trips(db):
    store = ApprovalStore(db)
    approval_id = await store.create("r1", "gmail_send", {"to": "x"}, "OUTWARD")
    assert (await store.get(approval_id)).classification == "OUTWARD"
    assert (await store.pending())[0].classification == "OUTWARD"


async def test_outward_request_uses_long_timeout(db):
    # The waiter's base timeout is tiny; a MUTATING_LOCAL request would expire,
    # but an OUTWARD send must use the 6h window and stay pending.
    waiter = ApprovalWaiter(ApprovalStore(db), timeout_s=0.05)
    req = asyncio.ensure_future(
        waiter.request("r1", "gmail_send", {}, ActionClass.OUTWARD)
    )
    for _ in range(50):
        await asyncio.sleep(0)
        if await waiter.store.pending():
            break
    await asyncio.sleep(0.1)  # well past the 0.05s local timeout
    pending = await waiter.store.pending()
    assert pending, "outward request expired on the short local timeout"
    await waiter.resolve(pending[0].id, approved=True)
    assert await req is True


async def test_on_pending_hook_fires(db):
    seen: list[tuple[str, str, ActionClass]] = []

    async def on_pending(approval_id, tool, args, classification) -> None:
        seen.append((approval_id, tool, classification))

    waiter = ApprovalWaiter(ApprovalStore(db), timeout_s=0.05, on_pending=on_pending)
    # MUTATING_LOCAL so the 0.05s timeout applies and the request expires fast;
    # OUTWARD would use the 6h window and never return here (no resolver).
    await waiter.request(None, "task_create", {}, ActionClass.MUTATING_LOCAL)
    assert len(seen) == 1
    assert seen[0][1] == "task_create"
    assert seen[0][2] is ActionClass.MUTATING_LOCAL
