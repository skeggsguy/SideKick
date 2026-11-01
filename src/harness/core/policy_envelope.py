"""Delegation-envelope gate: the browser subagent's boundary checks.

Split out of core/policy.py (which keeps the classification matrix and the
ordinary egress gates). The envelope REPLACES classification gating for the
browser interaction tools: decision order taints (attack signals) → origin
(tab drift + navigate target) → budget → identity refs → auto within the
envelope. There is deliberately no submit/click heuristic; the envelope's
origin + refs boundaries are the only gates. The origin/ref boundaries WIDEN
and continue (the human's approval admits the host, then the remaining
checks run) — but a call that needed any boundary approval reports
'approved', not 'auto', so the audit row records the human's part.

This module never imports policy.py (policy imports THIS one); the engine
is duck-typed — only .envelope, .approver, .tab_host_fn and
.element_host_fn are read, and ActionClass values pass through untouched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .egress import _normalize_host, host_of
from .envelope import find_refs, host_within

if TYPE_CHECKING:  # pragma: no cover
    from .policy import PolicyEngine, TurnContext

# Machine-readable kinds for the two boundary gates (persisted on the
# approvals row like the egress kinds — see store/approvals.why_gated).
GATE_ORIGIN_CHANGE = "origin_change"
GATE_REF_RESOLVE = "ref_resolve"

# The browser interaction tools a delegation envelope governs. MUST mirror
# tools/browser_interact.BROWSER_INTERACTION_TOOL_NAMES (cross-checked in
# tests/test_envelope_policy.py) — policy must not import from tools/
# (layering), so the frozenset is duplicated there.
BROWSER_ENVELOPE_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_scroll",
    }
)

# The subset that counts against a delegation's action budget: the calls that
# ACT on a page (reach a URL, click, fill). Snapshots/reads/scrolls are free —
# the child is told to re-snapshot after every page change, so counting reads
# halved the effective budget in practice (observed 2026-07-17: a simple
# booking spent 22 of 43 calls on reads). Reads stay bounded by the child's
# max_steps and still gate on taints like every EGRESS_TOOLS member.
BROWSER_ACTION_TOOLS = frozenset(
    {"browser_navigate", "browser_click", "browser_type", "browser_select"}
)


class EnvelopeGate:
    """All envelope decisions for one PolicyEngine."""

    def __init__(self, engine: "PolicyEngine") -> None:
        self._engine = engine

    async def decide(
        self,
        tool_name: str,
        args: dict,
        classification: Any,
        context: "TurnContext | None",
    ) -> tuple[str, str]:
        env = self._engine.envelope
        assert env is not None  # guarded by the caller
        host = self._engine.tab_host_fn() if self._engine.tab_host_fn else None

        terminal = await self._taint(tool_name, args, classification, context)
        if terminal is not None:
            return terminal

        terminal, widened = await self._origin(tool_name, args, classification, host)
        if terminal is not None:
            return terminal

        # Budget: page ACTIONS only (navigate/click/type — reads are free);
        # each action past the allocation re-prompts (never widens).
        if (
            tool_name in BROWSER_ACTION_TOOLS
            and context is not None
            and context.action_calls >= env.budget
        ):
            from .policy import GATE_EGRESS_BUDGET

            n = context.action_calls
            return await self._decide_terminal(
                tool_name,
                args,
                classification,
                GATE_EGRESS_BUDGET,
                f"delegation budget reached ({n}/{env.budget} actions)",
                f"approved by user (delegation budget reached {n}/{env.budget})",
            )

        terminal, ref_widened = await self._refs(tool_name, args, classification, host)
        if terminal is not None:
            return terminal
        if widened or ref_widened:
            return "approved", "within delegation envelope (boundary approved by user)"
        return "auto", "within delegation envelope"

    async def _decide_terminal(
        self,
        tool_name: str,
        args: dict,
        classification: Any,
        gate_kind: str,
        deny_reason: str,
        approved_reason: str,
    ) -> tuple[str, str]:
        """Prompt a boundary that RESOLVES the call (taint / budget):
        approve ⇒ (approved, ...), deny/no-approver ⇒ denied. 'always' is
        treated as 'yes' and never persists (a standing grant is meaningless
        for a one-shot subagent run)."""
        approver = self._engine.approver
        if approver is None:
            return "denied", deny_reason
        answer = await approver(tool_name, args, classification, gate_kind=gate_kind)
        if answer in ("yes", "always"):
            return "approved", approved_reason
        return "denied", "denied by user"

    async def _widen_continue(
        self,
        tool_name: str,
        args: dict,
        classification: Any,
        gate_kind: str,
        deny_reason: str,
    ) -> str | None:
        """Prompt a boundary that WIDENS then CONTINUES (origin change / first
        ref use): returns None on approval so the caller admits the host and
        runs the remaining checks, or a deny reason on refusal / no approver.
        'always' is treated as 'yes' and never persists."""
        approver = self._engine.approver
        if approver is None:
            return deny_reason
        answer = await approver(tool_name, args, classification, gate_kind=gate_kind)
        if answer in ("yes", "always"):
            return None
        return "denied by user"

    async def _taint(
        self,
        tool_name: str,
        args: dict,
        classification: Any,
        context: "TurnContext | None",
    ) -> tuple[str, str] | None:
        """Taints outrank everything: a secret read or internal probe this turn
        strips the envelope's auto-run just like an ordinary egress tool."""
        from .policy import _internal_probe_gate, _secret_read_gate

        if context is not None and context.secret_read:
            return await self._decide_terminal(
                tool_name, args, classification, *_secret_read_gate()
            )
        if context is not None and context.internal_probe:
            return await self._decide_terminal(
                tool_name, args, classification, *_internal_probe_gate()
            )
        return None

    async def _origin(
        self,
        tool_name: str,
        args: dict,
        classification: Any,
        host: str | None,
    ) -> tuple[tuple[str, str] | None, bool]:
        """Origin boundary: the live tab drifted off the approved origin, and/or
        a navigate targets a host outside it. Each prompts once, then admits the
        host and continues. Returns (denied tuple on refusal else None,
        whether any host was admitted by the human)."""
        env = self._engine.envelope
        assert env is not None
        widened = False
        if host is not None and not host_within(host, env.approved_hosts):
            deny = (
                f"the page moved to {host} — outside the approved "
                f"{env.origin} delegation"
            )
            reason = await self._widen_continue(
                tool_name, args, classification, GATE_ORIGIN_CHANGE, deny
            )
            if reason is not None:
                return ("denied", reason), widened
            env.approved_hosts.add(_normalize_host(host))
            widened = True

        if tool_name == "browser_navigate":
            # No parseable host ⇒ let it through; the tool's own check_target
            # refuses the URL before any network I/O.
            target = host_of(str(args.get("url", "")))
            if target is not None and not host_within(target, env.approved_hosts):
                deny = (
                    f"navigating to {target} — outside the approved "
                    f"{env.origin} delegation"
                )
                reason = await self._widen_continue(
                    tool_name, args, classification, GATE_ORIGIN_CHANGE, deny
                )
                if reason is not None:
                    return ("denied", reason), widened
                env.approved_hosts.add(target)  # host_of already normalized
                widened = True
        return None, widened

    async def _refs(
        self,
        tool_name: str,
        args: dict,
        classification: Any,
        host: str | None,
    ) -> tuple[tuple[str, str] | None, bool]:
        """Identity refs (typing only): an ungranted ref is a HARD deny —
        capability, not preference — so filling it is mechanically impossible.
        A granted ref's first use per verifiable host prompts once. Returns
        (denied tuple else None, whether a first use was approved here)."""
        if tool_name != "browser_type":
            return None, False
        env = self._engine.envelope
        assert env is not None
        used = find_refs(str(args.get("text", "")))
        disallowed = used - env.refs
        if disallowed:
            bad = sorted(disallowed)[0]
            return (
                "denied",
                f"this delegation has no {{{bad}}} capability — refs are "
                "granted at spawn",
            ), False
        if not used:
            return None, False
        # The destination a typed ref actually reaches: the element's OWNING
        # frame's host when the handle resolves (an OOPIF payment field is the
        # widget's origin, not the page's) — falling back to the tab host for
        # engines without an element_host_fn.
        dest = host
        element_host_fn = self._engine.element_host_fn
        if element_host_fn is not None:
            dest = element_host_fn(str(args.get("handle", "")))
        # Refs need a verifiable destination — none = no first-use boundary to
        # check against, so refuse rather than guess.
        if dest is None:
            return (
                "denied",
                "cannot verify the destination frame for ref use",
            ), False
        if not host_within(dest, env.ref_resolved_hosts):
            deny = f"first use of {', '.join(sorted(used))} on {dest}"
            if host is not None and dest != host:
                deny += f" (an embedded frame on {host})"
            reason = await self._widen_continue(
                tool_name, args, classification, GATE_REF_RESOLVE, deny
            )
            if reason is not None:
                return ("denied", reason), False
            env.ref_resolved_hosts.add(_normalize_host(dest))
            return None, True
        return None, False
