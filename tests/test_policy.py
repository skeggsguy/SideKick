"""Policy engine: gating decisions per ActionClass, approver, and allowlist."""

from __future__ import annotations

import importlib
import pkgutil

from harness.core.policy import ActionClass, Autonomy, PolicyEngine, EGRESS_TOOLS
from harness.store.audit import AllowlistStore
from harness.tools.base import Tool
from conftest import gotcha


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


async def test_read_only_runs_without_approval():
    policy = PolicyEngine()
    decision, _ = await policy.check("read_file", {}, ActionClass.READ_ONLY)
    assert decision == "auto"


async def test_gated_action_denied_without_approver():
    policy = PolicyEngine()
    for cls in (
        ActionClass.MUTATING_LOCAL,
        ActionClass.OUTWARD,
        ActionClass.DESTRUCTIVE,
    ):
        decision, reason = await policy.check("edit_file", {}, cls)
        assert decision == "denied"
        assert "no approver" in reason


async def test_approver_yes_and_no():
    policy = PolicyEngine()
    approve = approver_returning("yes")
    policy.approver = approve
    decision, _ = await policy.check("bash", {"command": "ls"}, ActionClass.MUTATING_LOCAL)
    assert decision == "approved"
    # Classification-gated calls carry no egress gate kind.
    assert approve.calls[0][3] is None

    policy.approver = approver_returning("no")
    decision, _ = await policy.check("bash", {"command": "ls"}, ActionClass.MUTATING_LOCAL)
    assert decision == "denied"


async def test_always_persists_to_allowlist(db):
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist)
    approve = approver_returning("always")
    policy.approver = approve

    decision, _ = await policy.check("edit_file", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "approved"
    assert len(approve.calls) == 1

    # Next call is auto via the allowlist — the approver is not consulted.
    decision, reason = await policy.check("edit_file", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "auto"
    assert reason == "allowlisted"
    assert len(approve.calls) == 1


async def test_outward_always_does_not_persist(db):
    # "always" on an OUTWARD send is treated as a one-time yes — it must not
    # land on the allowlist, so the next send prompts again.
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist)
    approve = approver_returning("always")
    policy.approver = approve

    decision, reason = await policy.check("gmail_send", {}, ActionClass.OUTWARD)
    assert decision == "approved"
    assert reason == "approved by user"  # not "allowlisted for next time"
    assert not await allowlist.contains("gmail_send")

    decision, _ = await policy.check("gmail_send", {}, ActionClass.OUTWARD)
    assert decision == "approved"
    assert len(approve.calls) == 2  # prompted both times


async def test_destructive_never_allowlisted(db):
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist)
    approve = approver_returning("always")
    policy.approver = approve

    decision, _ = await policy.check("drop_table", {}, ActionClass.DESTRUCTIVE)
    assert decision == "approved"
    # "always" must not stick for DESTRUCTIVE: the next call prompts again.
    decision, _ = await policy.check("drop_table", {}, ActionClass.DESTRUCTIVE)
    assert decision == "approved"
    assert len(approve.calls) == 2
    assert not await allowlist.contains("drop_table")

async def test_allowlist_only_fires_interactive(db):
    """Trigger provenance: a grant added to smooth interactive chat must NOT
    auto-fire in a supervised run — an injected email steering a triage run
    through an allowlisted gmail_draft_reply would otherwise stage attacker
    content with no human seeing it (v3 audit Q1.3)."""
    allowlist = AllowlistStore(db)
    await allowlist.add("gmail_draft_reply")

    interactive = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.INTERACTIVE)
    decision, reason = await interactive.check(
        "gmail_draft_reply", {}, ActionClass.MUTATING_LOCAL
    )
    assert (decision, reason) == ("auto", "allowlisted")

    supervised = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.SUPERVISED)
    approve = approver_returning("yes")
    supervised.approver = approve
    decision, _ = await supervised.check(
        "gmail_draft_reply", {}, ActionClass.MUTATING_LOCAL
    )
    assert decision == "approved"  # went through the (deferred) approver
    assert len(approve.calls) == 1, (
        "allowlist fires only INTERACTIVE/AUTONOMOUS; SUPERVISED parks even allowlisted tools — by design, don't 'fix'"
    )


async def test_supervised_always_does_not_persist(db):
    """An "always" answered on a supervised run's deferred approval must not
    create a standing grant — that would let one background approval widen
    future unattended runs."""
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.SUPERVISED)
    policy.approver = approver_returning("always")

    decision, reason = await policy.check("edit_file", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "approved"
    assert reason == "approved by user"  # not "allowlisted for next time"
    assert not await allowlist.contains("edit_file")

# ---- autonomous: per-job allowlist opt-in -----------------------------------

async def test_allowlist_fires_in_autonomous(db):
    """An autonomous job honors the global allowlist for MUTATING_LOCAL — the
    user opted that job in explicitly (via an always-ask schedule change)."""
    allowlist = AllowlistStore(db)
    await allowlist.add("calendar_event_create")

    autonomous = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.AUTONOMOUS)
    decision, reason = await autonomous.check(
        "calendar_event_create", {}, ActionClass.MUTATING_LOCAL
    )
    assert (decision, reason) == ("auto", "allowlisted")


async def test_autonomous_unallowlisted_goes_to_approver(db):
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.AUTONOMOUS)
    approve = approver_returning("yes")
    policy.approver = approve
    decision, _ = await policy.check("edit_file", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "approved"
    assert len(approve.calls) == 1


async def test_autonomous_outward_never_auto(db):
    """OUTWARD parks for approval in autonomous runs — sending is never
    automatic regardless of the job's autonomy."""
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.AUTONOMOUS)
    approve = approver_returning("yes")
    policy.approver = approve
    decision, _ = await policy.check("gmail_send", {}, ActionClass.OUTWARD)
    assert decision == "approved"
    assert len(approve.calls) == 1


async def test_autonomous_denies_destructive():
    policy = PolicyEngine(autonomy=Autonomy.AUTONOMOUS)
    policy.approver = approver_returning("yes")
    decision, reason = await policy.check("drop_table", {}, ActionClass.DESTRUCTIVE)
    assert decision == "denied"
    assert "unattended" in reason


async def test_autonomous_always_does_not_persist(db):
    """'always' in an autonomous run degrades to a one-time approval — grants
    are only ever written by a human in interactive chat."""
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.AUTONOMOUS)
    policy.approver = approver_returning("always")

    decision, reason = await policy.check("edit_file", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "approved"
    assert reason == "approved by user"
    assert not await allowlist.contains("edit_file")


# ---- always-ask schedule tools ----------------------------------------------

async def test_always_ask_never_auto_even_when_allowlisted(db):
    """schedule_add/edit/delete go to the approver in every autonomy mode —
    a poisoned allowlist row must not let the agent silently edit its own
    jobs (and thereby its own future permissions)."""
    allowlist = AllowlistStore(db)
    for tool in ("schedule_add", "schedule_edit", "schedule_delete"):
        await allowlist.add(tool)  # poisoned by hand; the API refuses these

    for autonomy in (Autonomy.INTERACTIVE, Autonomy.SUPERVISED, Autonomy.AUTONOMOUS):
        policy = PolicyEngine(allowlist=allowlist, autonomy=autonomy)
        approve = approver_returning("yes")
        policy.approver = approve
        for tool in ("schedule_add", "schedule_edit", "schedule_delete"):
            decision, reason = await policy.check(
                tool, {}, ActionClass.MUTATING_LOCAL
            )
            assert decision == "approved", (autonomy, tool)
            assert "always ask" in reason
        assert len(approve.calls) == 3  # every call prompted


async def test_always_ask_parks_not_denies_unattended():
    """Unlike DESTRUCTIVE, schedule mutations in background runs reach the
    (deferred) approver rather than being denied — self-rescheduling jobs
    stay possible with a human signing off."""
    for autonomy in (Autonomy.SUPERVISED, Autonomy.AUTONOMOUS):
        policy = PolicyEngine(autonomy=autonomy)
        policy.approver = approver_returning("no")
        decision, reason = await policy.check(
            "schedule_edit", {}, ActionClass.MUTATING_LOCAL
        )
        assert (decision, reason) == ("denied", "denied by user")  # human, not policy


async def test_always_ask_never_persists(db):
    allowlist = AllowlistStore(db)
    policy = PolicyEngine(allowlist=allowlist, autonomy=Autonomy.INTERACTIVE)
    policy.approver = approver_returning("always")
    decision, _ = await policy.check("schedule_add", {}, ActionClass.MUTATING_LOCAL)
    assert decision == "approved"
    assert not await allowlist.contains("schedule_add")


async def test_always_ask_denied_in_restricted_and_approverless():
    restricted = PolicyEngine(autonomy=Autonomy.RESTRICTED)
    restricted.approver = approver_returning("yes")
    decision, _ = await restricted.check(
        "schedule_add", {}, ActionClass.MUTATING_LOCAL
    )
    assert decision == "denied"

    bare = PolicyEngine()
    decision, reason = await bare.check(
        "schedule_add", {}, ActionClass.MUTATING_LOCAL
    )
    assert decision == "denied"
    assert "no approver" in reason


# ---- secret-read taint → egress gate (trifecta break) ----------------------

async def test_taint_gates_egress_tools():
    """After a secret-class read, web_fetch/browser_fetch/web_search/
    weather/maps_search/maps_route lose their auto-run for the rest of the turn
    (v3 audit Q1.1)."""
    from harness.core.policy import TurnContext

    tainted = TurnContext(secret_read=True)

    # Interactive with an approver: prompted, "yes" approves this call only.
    policy = PolicyEngine()
    approve = approver_returning("yes")
    policy.approver = approve
    decision, reason = await policy.check(
        "web_fetch", {"url": "https://x.test"}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert len(approve.calls) == 1
    # The approver is told WHICH gate parked the call — persisted on the
    # approvals row so the UI copy matches the actual gate.
    assert approve.calls[0][3] == "secret_read"

    # browser_fetch is egress too (real-browser fetch) — same gate.
    approve_bf = approver_returning("yes")
    policy.approver = approve_bf
    decision, reason = await policy.check(
        "browser_fetch", {"url": "https://x.test"}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert len(approve_bf.calls) == 1

    # rss_fetch is egress too (fetches model-chosen feed URLs) — same gate.
    approve_rf = approver_returning("yes")
    policy.approver = approve_rf
    decision, reason = await policy.check(
        "rss_fetch", {"url": "https://x.test/feed.xml"}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert len(approve_rf.calls) == 1
    assert approve_rf.calls[0][3] == "secret_read"

    # reddit_comments / hn_comments are egress too (fetch model-named post
    # URLs) — same gate, and denied outright with no approver.
    for feeds_tool in ("reddit_comments", "hn_comments"):
        approve_fc = approver_returning("yes")
        policy.approver = approve_fc
        decision, reason = await policy.check(
            feeds_tool, {"url": "https://x.test/item"}, ActionClass.READ_ONLY,
            context=tainted,
        )
        assert decision == "approved", feeds_tool
        assert "secret-class" in reason
        assert len(approve_fc.calls) == 1
        assert approve_fc.calls[0][3] == "secret_read"

        decision, reason = await PolicyEngine().check(
            feeds_tool, {"url": "https://x.test/item"}, ActionClass.READ_ONLY,
            context=tainted,
        )
        assert decision == "denied", feeds_tool
        assert "secret-class" in reason

    # Lane B interaction tools are egress too — a tainted turn strips their
    # auto-run and parks the call with GATE_SECRET_READ (browser_click here is
    # OUTWARD by class, but the taint gate outranks the classification prompt).
    approve_bc = approver_returning("yes")
    policy.approver = approve_bc
    decision, reason = await policy.check(
        "browser_click", {"handle": "e2"}, ActionClass.OUTWARD, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert len(approve_bc.calls) == 1
    assert approve_bc.calls[0][3] == "secret_read"

    policy.approver = approver_returning("no")
    decision, _ = await policy.check(
        "web_fetch", {}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "denied"

    # Weather also gated (remote tool, egress).
    policy.approver = approver_returning("yes")  # Weather needs approver to see reason
    decision, reason = await policy.check(
        "weather", {"place": "NYC"}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason

    # Maps tools also gated (queries/addresses are exfil channels).
    approve_maps = approver_returning("yes")
    policy.approver = approve_maps
    decision, reason = await policy.check(
        "maps_search", {"query": "pharmacy in Springfield"}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert len(approve_maps.calls) == 1

    decision, reason = await policy.check(
        "maps_route", {"origin": "Home", "destination": "Office"}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "approved"
    assert "secret-class" in reason
    assert len(approve_maps.calls) == 2

    # No approver (supervised without wiring, tests): denied outright —
    # every EGRESS tool must hit this path, so each is exercised by name.
    bare = PolicyEngine()
    for tool in (
        "web_search", "web_fetch", "browser_fetch",
        "weather", "maps_search", "maps_route",
        "browser_navigate", "browser_snapshot", "browser_read",
        "browser_click", "browser_type", "browser_select", "browser_scroll",
    ):
        decision, reason = await bare.check(
            tool, {}, ActionClass.READ_ONLY, context=tainted
        )
        assert decision == "denied", tool
        assert "secret-class" in reason

    # Restricted: denied even with an approver.
    restricted = PolicyEngine(autonomy=Autonomy.RESTRICTED)
    restricted.approver = approver_returning("yes")
    decision, _ = await restricted.check(
        "web_fetch", {}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "denied"


async def test_taint_leaves_other_tools_alone():
    from harness.core.policy import TurnContext

    tainted = TurnContext(secret_read=True)
    policy = PolicyEngine()

    # Non-egress read-only tools still auto-run on a tainted turn.
    decision, _ = await policy.check(
        "gmail_read", {}, ActionClass.READ_ONLY, context=tainted
    )
    assert decision == "auto"

    # And egress tools auto-run on an untainted turn.
    decision, _ = await policy.check(
        "web_fetch", {}, ActionClass.READ_ONLY, context=TurnContext()
    )
    assert decision == "auto"


# ---- egress tools catalog invariant -----------------------------------------

def test_all_remote_inprocess_tools_are_egress_gated():
    """All outbound in-process tools must be registered in EGRESS_TOOLS.

    This is a structural scan of harness.tools submodules (so a future tool is
    seen without a hand-list). Tools with remote=True are outbound and carry
    secret/taint risk — they must go through the egress gate.

    Coverage boundary: in-process tools only. MCP-adapted tools are runtime
    instances (MCPTool class, harness.mcp.manager) and are invisible to the
    class scan — that case is prose-enforced at policy.EGRESS_TOOLS's comment.

    Pinned in tests/test_policy.py."""
    import harness.tools

    # Collect all Tool subclasses from harness.tools and its submodules
    remote_tools = set()
    for _, mod_name, _ in pkgutil.iter_modules(harness.tools.__path__):
        full_name = f"harness.tools.{mod_name}"
        try:
            mod = importlib.import_module(full_name)
        except ImportError:
            continue

        # Find all Tool subclasses in this module
        for name in dir(mod):
            obj = getattr(mod, name)
            if (isinstance(obj, type) and
                issubclass(obj, Tool) and
                obj is not Tool and
                obj.__module__.startswith("harness.tools") and
                getattr(obj, "remote", False)):
                remote_tools.add(obj.name if hasattr(obj, "name") else name)

    missing = remote_tools - EGRESS_TOOLS
    assert not missing, gotcha(
        "egress-tools-taint",
        f"remote in-process tools must join EGRESS_TOOLS (or a secret read / internal probe no longer "
        f"strips their auto-run): {missing}. The scan keys on obj.name if present else the ClassName — "
        f"never give a name-less intermediate base remote=True"
    )


# ---- egress hardening: destination gate / budget / probe taint --------------
# (egress-hardening-note controls A–C — `git show da386d5:docs/plans/egress-hardening-note.md`;
# D lives in _netguard)

def _ctx(**kwargs):
    from harness.core.egress import EgressLedger
    from harness.core.policy import TurnContext

    ledger = kwargs.pop("ledger", None) or EgressLedger()
    return TurnContext(ledger=ledger, **kwargs), ledger


async def test_destination_gate_unseen_prompts_seen_autoruns():
    """A web_fetch/browser_fetch to a host never legitimized this session
    routes to the approver; a seeded host auto-runs."""
    ctx, ledger = _ctx()
    ledger.seed_text("please check https://rightmove.co.uk/for-sale")

    policy = PolicyEngine()
    approve = approver_returning("yes")
    policy.approver = approve

    decision, _ = await policy.check(
        "web_fetch", {"url": "https://rightmove.co.uk/x"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "auto"
    assert not approve.calls

    for tool in ("web_fetch", "browser_fetch", "rss_fetch", "reddit_comments", "hn_comments"):
        decision, reason = await policy.check(
            tool, {"url": "https://evil.test/?d=x"}, ActionClass.READ_ONLY, context=ctx
        )
        assert decision == "approved", tool
        assert "new destination" in reason
    assert len(approve.calls) == 5
    assert all(c[3] == "new_destination" for c in approve.calls)

    policy.approver = approver_returning("no")
    decision, reason = await policy.check(
        "web_fetch", {"url": "https://evil.test/"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "denied"
    assert reason == "denied by user"


async def test_destination_gate_denied_without_approver_or_restricted():
    ctx, _ = _ctx()
    bare = PolicyEngine()
    decision, reason = await bare.check(
        "web_fetch", {"url": "https://evil.test/"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "denied"
    assert "new destination" in reason

    restricted = PolicyEngine(autonomy=Autonomy.RESTRICTED)
    restricted.approver = approver_returning("yes")
    decision, _ = await restricted.check(
        "browser_fetch", {"url": "https://evil.test/"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "denied"


async def test_destination_gate_applies_only_to_url_fetch_tools():
    """web_search/weather/maps hit pinned API endpoints — an empty ledger
    must not gate them (the taints and budget still do)."""
    ctx, _ = _ctx()
    policy = PolicyEngine()
    for tool, args in (
        ("web_search", {"query": "anything"}),
        ("weather", {"place": "NYC"}),
        ("maps_search", {"query": "pharmacy"}),
        ("maps_route", {"origin": "a", "destination": "b"}),
    ):
        decision, _ = await policy.check(
            tool, args, ActionClass.READ_ONLY, context=ctx
        )
        assert decision == "auto", tool


async def test_destination_gate_browser_navigate():
    """browser_navigate takes an arbitrary url, so it is destination-gated: an
    unseen host prompts GATE_NEW_DESTINATION; a seeded host clears the gate but
    then hits the OUTWARD classification prompt (navigate is OUTWARD, not
    READ_ONLY), so it prompts WITHOUT a gate kind."""
    ctx, ledger = _ctx()
    ledger.seed_text("open https://rightmove.co.uk/for-sale")

    policy = PolicyEngine()
    approve = approver_returning("yes")
    policy.approver = approve

    # Unseen host: the destination gate fires first (and carries its kind).
    decision, reason = await policy.check(
        "browser_navigate", {"url": "https://evil.test/?d=x"},
        ActionClass.OUTWARD, context=ctx,
    )
    assert decision == "approved"
    assert "new destination" in reason
    assert approve.calls[-1][3] == "new_destination"

    # Seeded host: destination gate passes, but OUTWARD still prompts — no gate.
    decision, reason = await policy.check(
        "browser_navigate", {"url": "https://rightmove.co.uk/x"},
        ActionClass.OUTWARD, context=ctx,
    )
    assert decision == "approved"
    assert reason == "approved by user"  # classification prompt, not an egress gate
    assert approve.calls[-1][3] is None


async def test_destination_gate_skipped_without_ledger_or_url():
    """A bare TurnContext (no ledger — test/legacy construction) and a
    hostless URL both skip the gate: the tool's own check_target refuses a
    bad URL before any network I/O."""
    from harness.core.policy import TurnContext

    policy = PolicyEngine()
    decision, _ = await policy.check(
        "web_fetch", {"url": "https://evil.test/"}, ActionClass.READ_ONLY,
        context=TurnContext(),
    )
    assert decision == "auto"

    ctx, _ = _ctx()
    for args in ({}, {"url": "not a url"}, {"url": "file:///etc/passwd"}):
        decision, _ = await policy.check(
            "web_fetch", args, ActionClass.READ_ONLY, context=ctx
        )
        assert decision == "auto", args


def test_domain_matching_normalization():
    """www-strip, case-folding, subdomain-of-seed allowed, parent NOT
    allowed, IP literals exact, and no registrable-domain collapse (a co.uk
    seed must not trust all of *.co.uk)."""
    from harness.core.egress import EgressLedger

    ledger = EgressLedger()
    ledger.seed_text("see https://www.Example.com and 93.184.216.34")
    assert ledger.allows("https://example.com/x")
    assert ledger.allows("https://WWW.EXAMPLE.COM/x")
    assert ledger.allows("https://sub.example.com/x")
    assert ledger.allows("http://93.184.216.34/x")
    assert not ledger.allows("http://93.184.216.35/x")
    assert not ledger.allows("https://notexample.com/x")

    ledger2 = EgressLedger()
    ledger2.seed_text("listings on rightmove.co.uk today")
    assert ledger2.allows("https://www.rightmove.co.uk/props")
    assert not ledger2.allows("https://zoopla.co.uk/")  # no *.co.uk collapse

    ledger3 = EgressLedger()
    ledger3.seed_text("check portal.example.com")
    assert not ledger3.allows("https://example.com/")  # parent not admitted


async def test_egress_budget_over_cap_prompts_under_cap_auto():
    ctx, _ = _ctx(egress_calls=2)
    policy = PolicyEngine(egress_budget=3)
    decision, _ = await policy.check(
        "web_search", {"query": "q"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "auto"

    ctx.egress_calls = 3
    approve = approver_returning("yes")
    policy.approver = approve
    decision, reason = await policy.check(
        "web_search", {"query": "q"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "approved"
    assert "egress budget" in reason
    assert len(approve.calls) == 1
    assert approve.calls[0][3] == "egress_budget"

    # Non-egress tools are untouched by a spent budget.
    decision, _ = await policy.check(
        "gmail_read", {}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "auto"


async def test_egress_budget_denied_without_approver():
    ctx, _ = _ctx(egress_calls=99)
    bare = PolicyEngine()
    decision, reason = await bare.check(
        "web_search", {"query": "q"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "denied"
    assert "egress budget" in reason


async def test_internal_probe_taint_gates_egress_tools():
    """internal_probe gates every EGRESS tool exactly like secret_read —
    a refused internal-target fetch is treated as attack-in-progress."""
    ctx, ledger = _ctx(internal_probe=True)
    ledger.seed_text("https://ok.test")  # seen destination: probe still wins

    bare = PolicyEngine()
    for tool in EGRESS_TOOLS:
        decision, reason = await bare.check(
            tool, {"url": "https://ok.test/"}, ActionClass.READ_ONLY, context=ctx
        )
        assert decision == "denied", tool
        assert "internal/private address was probed" in reason

    policy = PolicyEngine()
    approve = approver_returning("yes")
    policy.approver = approve
    decision, reason = await policy.check(
        "web_fetch", {"url": "https://ok.test/"}, ActionClass.READ_ONLY, context=ctx
    )
    assert decision == "approved"
    assert "internal address probed" in reason
    assert approve.calls[0][3] == "internal_probe"
