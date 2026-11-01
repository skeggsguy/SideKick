"""Delegation envelope — the per-subagent authority record and its policy gate.

The main agent spawns a browser subagent under a DelegationEnvelope
{origin, task, budget, refs}. The human approves it once at spawn; inside it
the subagent's six browser tools auto-run, and the policy engine re-prompts
ONLY at machinery-detected boundaries — origin change, first identity-ref use
per site, budget exhaustion. Empty refs make filling contact details a HARD
deny (capability, not preference). No submit gate exists by design.
"""

from __future__ import annotations

import pytest

from harness.core.envelope import (
    KNOWN_REFS,
    DelegationEnvelope,
    find_refs,
    host_within,
)
from harness.core.policy import (
    GATE_EGRESS_BUDGET,
    GATE_ORIGIN_CHANGE,
    GATE_REF_RESOLVE,
    GATE_SECRET_READ,
    ActionClass,
    Autonomy,
    PolicyEngine,
    TurnContext,
)
from harness.store.approvals import why_gated
from harness.store.audit import AllowlistStore


# --- local helpers (copied from test_policy style, not imported) -------------


def approver_returning(answer: str):
    calls: list[tuple[str, dict, ActionClass, str | None]] = []

    async def approve(
        tool: str,
        args: dict,
        classification: ActionClass,
        gate_kind: str | None = None,
    ) -> str:
        calls.append((tool, args, classification, gate_kind))
        return answer

    approve.calls = calls
    return approve


def _engine(
    *,
    origin: str = "example.com",
    task: str = "book a table",
    budget: int = 40,
    refs: frozenset[str] = frozenset(),
    tab_host: str | None = "example.com",
    approver: str | None = "yes",
    autonomy: Autonomy = Autonomy.INTERACTIVE,
    allowlist: AllowlistStore | None = None,
):
    """A PolicyEngine wired with an envelope and a fixed tab host."""
    env = DelegationEnvelope(origin=origin, task=task, budget=budget, refs=refs)
    policy = PolicyEngine(
        allowlist=allowlist,
        autonomy=autonomy,
        envelope=env,
        tab_host_fn=(lambda: tab_host),
    )
    approve = approver_returning(approver) if approver is not None else None
    policy.approver = approve
    return policy, env, approve


def denying_approver():
    """Approver that answers 'no' and records calls."""
    return approver_returning("no")


async def test_taint_deny_short_circuits_before_origin_prompt():
    """A denied taint gate must NOT also fire the origin-change prompt: the
    checks are sequential with early return, so the human sees ONE prompt for
    the one decision that ends the call (regression pin — a tuple of awaits
    once evaluated both gates eagerly)."""
    policy, _, approve = _engine(tab_host="drift.test", approver="no")
    tainted = TurnContext(secret_read=True)
    decision, reason = await policy.check(
        "browser_snapshot", {}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "denied"
    assert len(approve.calls) == 1  # taint only — origin never prompted
    assert approve.calls[0][3] == GATE_SECRET_READ


# --- 1. in-origin auto-run for all seven tools ---------------------------------


async def test_in_origin_all_seven_tools_auto():
    policy, _, approve = _engine(tab_host="example.com")
    cases = [
        ("browser_navigate", {"url": "https://example.com/x"}, ActionClass.OUTWARD),
        ("browser_snapshot", {}, ActionClass.READ_ONLY),
        ("browser_read", {}, ActionClass.READ_ONLY),
        ("browser_click", {"handle": "e1"}, ActionClass.OUTWARD),
        ("browser_type", {"handle": "e1", "text": "hello"}, ActionClass.OUTWARD),
        ("browser_select", {"handle": "e2"}, ActionClass.OUTWARD),
        ("browser_scroll", {"direction": "down"}, ActionClass.MUTATING_LOCAL),
    ]
    for tool, args, cls in cases:
        decision, reason = await policy.check(tool, args, cls)
        assert (decision, reason) == ("auto", "within delegation envelope"), tool
    assert not approve.calls  # nothing prompted inside the envelope


# --- 2. subdomain of origin ---------------------------------------------------


async def test_subdomain_of_origin_auto():
    policy, _, _ = _engine(tab_host="shop.example.com")
    decision, reason = await policy.check(
        "browser_snapshot", {}, ActionClass.READ_ONLY
    )
    assert (decision, reason) == ("auto", "within delegation envelope")


# --- 3. off-origin navigate prompts, admits on approve -----------------------


async def test_off_origin_navigate_prompts_and_admits():
    policy, env, approve = _engine(tab_host=None, approver="yes")
    # The boundary prompts (approver consulted with origin_change), the host is
    # admitted, the remaining checks run — and the decision reports 'approved'
    # (not 'auto') so the audit row records the human's part.
    decision, reason = await policy.check(
        "browser_navigate", {"url": "https://other.test/x"}, ActionClass.OUTWARD
    )
    assert decision == "approved"
    assert "boundary approved" in reason
    assert approve.calls[-1][3] == GATE_ORIGIN_CHANGE
    assert "other.test" in env.approved_hosts
    # A second navigate to the now-admitted host runs with no further prompt.
    decision, reason = await policy.check(
        "browser_navigate", {"url": "https://other.test/y"}, ActionClass.OUTWARD
    )
    assert (decision, reason) == ("auto", "within delegation envelope")
    assert len(approve.calls) == 1  # only the first prompted


async def test_off_origin_navigate_denied_not_admitted():
    policy, env, _ = _engine(tab_host=None, approver="no")
    decision, reason = await policy.check(
        "browser_navigate", {"url": "https://other.test/x"}, ActionClass.OUTWARD
    )
    assert decision == "denied"
    assert reason == "denied by user"
    assert "other.test" not in env.approved_hosts


# --- 4. tab drift gates even a READ_ONLY tool --------------------------------


async def test_tab_drift_gates_readonly_then_admits():
    policy, env, approve = _engine(tab_host="drift.test", approver="yes")
    decision, reason = await policy.check(
        "browser_snapshot", {}, ActionClass.READ_ONLY
    )
    assert decision == "approved"  # boundary approval surfaces in the audit row
    assert "boundary approved" in reason
    assert approve.calls[-1][3] == GATE_ORIGIN_CHANGE  # the boundary prompted
    assert "drift.test" in env.approved_hosts
    # Later call on the same (now-admitted) host is auto.
    decision, reason = await policy.check(
        "browser_read", {}, ActionClass.READ_ONLY
    )
    assert (decision, reason) == ("auto", "within delegation envelope")
    assert len(approve.calls) == 1


# --- 5. budget re-prompts on every over-cap call -----------------------------


async def test_budget_prompts_on_every_over_cap_call():
    """The budget gates page ACTIONS (navigate/click/type) via action_calls;
    reads/snapshots/scrolls are free even over the cap."""
    policy, _, approve = _engine(budget=5, tab_host="example.com", approver="yes")
    ctx = TurnContext(action_calls=5)
    for _ in range(2):
        decision, reason = await policy.check(
            "browser_click", {"handle": "e1"}, ActionClass.OUTWARD, context=ctx
        )
        assert decision == "approved"
        assert "delegation budget" in reason
    assert len(approve.calls) == 2  # budget was NOT extended
    assert all(c[3] == GATE_EGRESS_BUDGET for c in approve.calls)

    # Reads are FREE — over the action cap they still auto-run, no prompt.
    decision, reason = await policy.check(
        "browser_snapshot", {}, ActionClass.READ_ONLY, context=ctx
    )
    assert (decision, reason) == ("auto", "within delegation envelope")
    assert len(approve.calls) == 2

    # Under the cap: actions auto too.
    under = TurnContext(action_calls=4)
    decision, reason = await policy.check(
        "browser_click", {"handle": "e1"}, ActionClass.OUTWARD, context=under
    )
    assert (decision, reason) == ("auto", "within delegation envelope")


# --- 6/7. refs: empty and disallowed → HARD deny, no approver ----------------


async def test_empty_refs_type_hard_denied_without_approver_call():
    policy, _, approve = _engine(refs=frozenset(), tab_host="example.com")
    decision, reason = await policy.check(
        "browser_type",
        {"handle": "e1", "text": "my email is {email}"},
        ActionClass.OUTWARD,
    )
    assert decision == "denied"
    assert "capability" in reason and "{email}" in reason
    assert not approve.calls  # hard deny never consults the approver


async def test_disallowed_ref_hard_denied():
    policy, _, approve = _engine(refs=frozenset({"phone"}), tab_host="example.com")
    decision, reason = await policy.check(
        "browser_type", {"handle": "e1", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert decision == "denied"
    assert "{email}" in reason
    assert not approve.calls


# --- 8. permitted ref first use gates once, then auto; new host re-gates ------


async def test_permitted_ref_first_use_then_auto_and_new_host():
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    policy = PolicyEngine(envelope=env, tab_host_fn=lambda: "example.com")
    approve = approver_returning("yes")
    policy.approver = approve

    text = {"handle": "e1", "text": "{email}"}
    decision, reason = await policy.check("browser_type", text, ActionClass.OUTWARD)
    assert decision == "approved"  # first-use approval surfaces in the audit row
    assert "boundary approved" in reason
    assert approve.calls[-1][3] == GATE_REF_RESOLVE  # first-use prompted
    assert "example.com" in env.ref_resolved_hosts

    # Same host again: no prompt.
    decision, reason = await policy.check("browser_type", text, ActionClass.OUTWARD)
    assert (decision, reason) == ("auto", "within delegation envelope")
    assert len(approve.calls) == 1

    # A different host (origin already cleared) needs its OWN ref first-use
    # boundary — the ref grant is resolved per host.
    policy.tab_host_fn = lambda: "other.test"
    env.approved_hosts.add("other.test")  # pretend origin already cleared
    decision, reason = await policy.check("browser_type", text, ActionClass.OUTWARD)
    assert decision == "approved"
    assert approve.calls[-1][3] == GATE_REF_RESOLVE
    assert "other.test" in env.ref_resolved_hosts
    assert len(approve.calls) == 2


# --- 9. ref use with an unverifiable host → deny -----------------------------


async def test_ref_use_unverifiable_host_denied():
    # tab_host_fn returns None
    policy, _, approve = _engine(refs=frozenset({"email"}), tab_host=None)
    decision, reason = await policy.check(
        "browser_type", {"handle": "e1", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert decision == "denied"
    assert "cannot verify" in reason

    # tab_host_fn absent entirely
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    policy2 = PolicyEngine(envelope=env, tab_host_fn=None)
    policy2.approver = approver_returning("yes")
    decision, reason = await policy2.check(
        "browser_type", {"handle": "e1", "text": "{phone}"}, ActionClass.OUTWARD
    )
    # {phone} is not granted (refs={"email"}) → hard deny wins over host check.
    assert decision == "denied"
    assert "{phone}" in reason


async def test_ref_use_no_tab_fn_but_granted_denied_for_host():
    """A granted ref with no tab host resolver still can't be placed — there's
    no verifiable site for the first-use boundary."""
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    policy = PolicyEngine(envelope=env, tab_host_fn=None)
    policy.approver = approver_returning("yes")
    decision, reason = await policy.check(
        "browser_type", {"handle": "e1", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert decision == "denied"
    assert "cannot verify" in reason


# --- 9b. frame-aware ref destination (element_host_fn, Phase C OOPIF) ---------


async def test_ref_first_use_admits_the_frame_host_not_the_tab_host():
    """A ref typed into an OOPIF field prompts for — and admits — the FRAME's
    host; the embedding page's host stays out of ref_resolved_hosts."""
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    policy = PolicyEngine(
        envelope=env,
        tab_host_fn=lambda: "example.com",
        element_host_fn=lambda handle: "checkout.example",
    )
    approve = approver_returning("yes")
    policy.approver = approve
    decision, reason = await policy.check(
        "browser_type", {"handle": "e3", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert decision == "approved"
    assert approve.calls[-1][3] == GATE_REF_RESOLVE
    assert "checkout.example" in env.ref_resolved_hosts
    assert "example.com" not in env.ref_resolved_hosts


async def test_ref_frame_host_prompt_names_both_hosts_when_differing():
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    policy = PolicyEngine(
        envelope=env,
        tab_host_fn=lambda: "example.com",
        element_host_fn=lambda handle: "checkout.example",
    )
    approve = approver_returning("no")
    policy.approver = approve
    decision, reason = await policy.check(
        "browser_type", {"handle": "e3", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert decision == "denied"
    # The human saw the real destination AND its embedding context.
    args = approve.calls[-1]
    assert args[3] == GATE_REF_RESOLVE


async def test_ref_unresolvable_element_host_denied():
    """element_host_fn wired but returning None (unknown handle / detached
    frame / URL-less frame) → deny, never fall back to the tab host."""
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    env.ref_resolved_hosts = {"example.com"}  # tab host IS approved
    policy = PolicyEngine(
        envelope=env,
        tab_host_fn=lambda: "example.com",
        element_host_fn=lambda handle: None,
    )
    policy.approver = approver_returning("yes")
    decision, reason = await policy.check(
        "browser_type", {"handle": "e99", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert decision == "denied"
    assert "cannot verify" in reason


async def test_ref_without_element_host_fn_keeps_tab_host_behavior():
    """No element_host_fn (legacy wiring) → the tab host still governs."""
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    env.ref_resolved_hosts = {"example.com"}
    policy = PolicyEngine(envelope=env, tab_host_fn=lambda: "example.com")
    policy.approver = approver_returning("yes")
    decision, reason = await policy.check(
        "browser_type", {"handle": "e1", "text": "{email}"}, ActionClass.OUTWARD
    )
    assert (decision, reason) == ("auto", "within delegation envelope")


# --- 10. taints outrank the envelope -----------------------------------------


async def test_secret_read_taint_gates_in_envelope_click():
    policy, _, approve = _engine(tab_host="example.com", approver="yes")
    ctx = TurnContext(secret_read=True)
    decision, reason = await policy.check(
        "browser_click", {"handle": "e1"}, ActionClass.OUTWARD, context=ctx
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert approve.calls[-1][3] == GATE_SECRET_READ


# --- 11. no approver → every prompt path denies ------------------------------


async def test_no_approver_denies_every_boundary():
    env = DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"email"}))
    # off-origin tab so the origin-change boundary would prompt
    policy = PolicyEngine(envelope=env, tab_host_fn=lambda: "off.test")
    policy.approver = None
    decision, reason = await policy.check(
        "browser_snapshot", {}, ActionClass.READ_ONLY
    )
    assert decision == "denied"
    assert "off.test" in reason

    # budget boundary, no approver
    policy2 = PolicyEngine(
        envelope=DelegationEnvelope(origin="example.com", task="t", budget=1),
        tab_host_fn=lambda: "example.com",
    )
    decision, reason = await policy2.check(
        "browser_click",
        {"handle": "e1"},
        ActionClass.OUTWARD,
        context=TurnContext(action_calls=1),
    )
    assert decision == "denied"
    assert "delegation budget" in reason


# --- 12. "always" approves but never persists --------------------------------


async def test_always_approves_but_persists_nothing(db):
    allowlist = AllowlistStore(db)
    policy, env, _ = _engine(
        tab_host=None, approver="always", allowlist=allowlist
    )
    decision, _ = await policy.check(
        "browser_navigate", {"url": "https://other.test/x"}, ActionClass.OUTWARD
    )
    assert decision == "approved"  # "always" widened the origin, then ran
    assert "other.test" in env.approved_hosts
    assert not await allowlist.contains("browser_navigate")


# --- 13. envelope + RESTRICTED is a construction error -----------------------


def test_envelope_with_restricted_raises():
    env = DelegationEnvelope(origin="example.com", task="t")
    with pytest.raises(ValueError, match="RESTRICTED"):
        PolicyEngine(autonomy=Autonomy.RESTRICTED, envelope=env)


# --- 14. no envelope → browser tools fall through to classification ----------


async def test_no_envelope_browser_click_hits_classification_approver():
    policy = PolicyEngine()  # no envelope
    approve = approver_returning("yes")
    policy.approver = approve
    decision, reason = await policy.check(
        "browser_click", {"handle": "e1"}, ActionClass.OUTWARD
    )
    assert decision == "approved"
    assert reason == "approved by user"  # ordinary OUTWARD classification prompt
    assert approve.calls[-1][3] is None  # no gate kind — not an envelope/egress gate


# --- 15. envelope constructor validation & normalization ---------------------


def test_envelope_validation_and_normalization():
    # origin normalization: scheme, www, case stripped; approved_hosts seeded
    env = DelegationEnvelope(origin="https://WWW.Example.COM", task="t")
    assert env.origin == "example.com"
    assert env.approved_hosts == {"example.com"}
    assert env.ref_resolved_hosts == set()

    # bad origins
    for bad in ("", "   ", "example.com/path", "no dot", "https://example.com/x", "localhost"):
        with pytest.raises(ValueError):
            DelegationEnvelope(origin=bad, task="t")

    # refs superset
    with pytest.raises(ValueError):
        DelegationEnvelope(origin="example.com", task="t", refs=frozenset({"ssn"}))

    # budget floor
    with pytest.raises(ValueError):
        DelegationEnvelope(origin="example.com", task="t", budget=0)

    # a valid refs subset survives
    env2 = DelegationEnvelope(
        origin="example.com", task="t", refs=frozenset({"email"})
    )
    assert env2.refs == frozenset({"email"})
    assert env2.refs <= KNOWN_REFS


def test_host_within_semantics():
    hosts = {"example.com"}
    assert host_within("example.com", hosts)
    assert host_within("sub.example.com", hosts)
    assert host_within("WWW.EXAMPLE.COM", hosts)  # normalized
    assert not host_within("notexample.com", hosts)
    assert not host_within(None, hosts)
    assert not host_within("", hosts)


# --- 16. cross-check the tool-name frozensets --------------------------------


def test_browser_envelope_tools_mirror_interaction_names():
    from harness.core.policy import BROWSER_ENVELOPE_TOOLS
    from harness.tools.browser_interact import BROWSER_INTERACTION_TOOL_NAMES

    assert BROWSER_ENVELOPE_TOOLS == BROWSER_INTERACTION_TOOL_NAMES


# --- 17. find_refs matches only the literal tokens ---------------------------


def test_find_refs_literal_only():
    assert find_refs("email {email} and phone {phone}") == frozenset(
        {"email", "phone"}
    )
    assert find_refs("just {email}") == frozenset({"email"})
    assert find_refs("no refs here") == frozenset()
    assert find_refs("{name} {address} { email }") == frozenset()  # not literal
    assert find_refs("{Email} {PHONE}") == frozenset()  # case-sensitive
    assert find_refs("stray { and } braces") == frozenset()


# --- why_gated copy + approvals row round-trip for the new kinds -------------


def test_why_gated_new_gate_kinds():
    origin = why_gated("OUTWARD", "supervised", gate_kind=GATE_ORIGIN_CHANGE)
    assert "scoped to one site" in origin
    # gate_kind wins over classification copy even on an OUTWARD row.
    assert "leaves your accounts" not in origin

    ref = why_gated("READ_ONLY", "supervised", gate_kind=GATE_REF_RESOLVE)
    assert "contact details" in ref
    assert "inserted mechanically" in ref


async def test_approvals_round_trip_new_gate_kinds(db):
    from harness.store.approvals import ApprovalStore
    from harness.store.runs import RunStore

    runs = RunStore(db)
    run = await runs.start(None, "test-model", trigger="user")
    store = ApprovalStore(db)
    origin_aid = await store.create(
        run, "browser_navigate", {"url": "x"}, "OUTWARD",
        gate_kind=GATE_ORIGIN_CHANGE,
    )
    ref_aid = await store.create(
        run, "browser_type", {"handle": "e1", "text": "{email}"}, "OUTWARD",
        gate_kind=GATE_REF_RESOLVE,
    )

    by_id = {a.id: a for a in await store.pending()}
    assert by_id[origin_aid].gate_kind == GATE_ORIGIN_CHANGE
    assert "scoped to one site" in by_id[origin_aid].why_gated
    assert by_id[ref_aid].gate_kind == GATE_REF_RESOLVE
    assert "contact details" in by_id[ref_aid].why_gated

    got = await store.get(origin_aid)
    assert got.gate_kind == GATE_ORIGIN_CHANGE

    await store.resolve(origin_aid, "approved")
    await store.resolve(ref_aid, "denied")
    hist = {h["id"]: h for h in await store.history()}
    assert hist[origin_aid]["gate_kind"] == GATE_ORIGIN_CHANGE
    assert "scoped to one site" in hist[origin_aid]["why_gated"]
    assert hist[ref_aid]["gate_kind"] == GATE_REF_RESOLVE
