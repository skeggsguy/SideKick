"""Action classification + approval gates, autonomy-aware.

Every tool declares an ActionClass; the PolicyEngine decides per call whether
it runs automatically, needs approval, or is denied. The decision matrix
depends on the run's autonomy level (PLAN-v2 §5):

| Autonomy    | READ_ONLY | MUTATING_LOCAL              | OUTWARD           | DESTRUCTIVE |
|-------------|-----------|-----------------------------|-------------------|-------------|
| interactive | auto      | prompt y/n/always           | prompt            | prompt      |
| supervised  | auto      | deferred approval           | deferred approval | deny        |
| autonomous  | auto      | allowlisted⇒auto, else defer| deferred approval | deny        |
| restricted  | auto      | deny                        | deny              | deny        |

Overlay: tools in ALWAYS_ASK_TOOLS (schedule mutations — the agent editing its
own future jobs and privileges) never auto-run and are never allowlistable, but
they are never hard-denied in unattended runs either: they go to the approver,
which in daemon runs parks a deferred approval. That keeps self-rescheduling
jobs possible with a human signing off every schedule change.

The approver is an injected async callable: the CLI provides the y/n/always
prompt; supervised runs get a deferred approver (core/approvals.py) that
parks the run on a pending approvals row. "always" persists to the allowlist
— except for DESTRUCTIVE, which always prompts and is never allowlisted.

The allowlist applies in interactive and autonomous runs only. A grant added
to smooth chat must not auto-fire in a supervised run whose trigger is
attacker-influenced (an injected email steering a triage run through an
allowlisted gmail_draft_reply) — supervised runs always park a deferred
approval, so a human reviews the full args. Autonomous is different: the user
opted that specific job in, and its toolset is a hard allocation chosen at
creation (both gates human-approved via the always-ask schedule tools).
"always" from any non-interactive run degrades to a plain approval instead
of writing a standing grant.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from ..store.audit import AllowlistStore
from .egress import EgressLedger, host_of
from .envelope import DelegationEnvelope
from .policy_envelope import (
    BROWSER_ACTION_TOOLS,  # noqa: F401  (re-export: consumers import from here)
    BROWSER_ENVELOPE_TOOLS,  # noqa: F401
    GATE_ORIGIN_CHANGE,  # noqa: F401
    GATE_REF_RESOLVE,  # noqa: F401
    EnvelopeGate,
)


class ActionClass(enum.Enum):
    READ_ONLY = "READ_ONLY"
    MUTATING_LOCAL = "MUTATING_LOCAL"
    OUTWARD = "OUTWARD"
    DESTRUCTIVE = "DESTRUCTIVE"


class Autonomy(enum.Enum):
    INTERACTIVE = "interactive"  # CLI / web chat turns
    SUPERVISED = "supervised"  # cron / watcher runs
    # Unattended runs the user explicitly opted in per job: the global
    # allowlist is honored (like interactive) over a HARD tool allocation.
    # Still unattended — DESTRUCTIVE stays denied and "always" never persists.
    AUTONOMOUS = "autonomous"
    RESTRICTED = "restricted"  # sub-runs, eval runs


class Approver(Protocol):
    """(tool_name, args, classification) -> "yes" | "no" | "always".

    gate_kind is set only when an egress gate (not the classification) parked
    the call — one of the GATE_* constants — so the approval surface can say
    WHY a READ_ONLY tool is asking. Implementations must accept it as a
    defaulted keyword."""

    def __call__(
        self,
        tool_name: str,
        args: dict,
        classification: ActionClass,
        gate_kind: str | None = None,
    ) -> Awaitable[str]: ...

# A new outbound tool joins EGRESS_TOOLS + declares approval context in gateway/
# preview.py (+ DESTINATION_GATED_TOOLS if it takes a URL); private refusals return InternalRefusalNote.
# Tools that send request data off-host. READ_ONLY by class (they change no
# state), but they are the egress step of the read-secret → exfiltrate chain,
# so a tainted turn strips their auto-run. (notify is deliberately omitted —
# destination is pinned to Tom's own push_subscriptions, never model-controlled.
# maps_search and maps_route send queries/addresses off-host, same channel.
# browser_fetch drives a real browser at model-chosen targets. The Lane B
# browser_* interaction tools drive the persistent tab (navigate/click/type
# reach model-chosen targets; snapshot/read/scroll read the current tab) — all
# remote, so all gate on the taints/budget for the rest of the turn.
# browser_delegate spawns a browser subagent (remote=True) that reaches the
# network, and a secret-read-tainted parent turn must gate the spawn.)
EGRESS_TOOLS = frozenset(
    {
        "web_fetch",
        "browser_fetch",
        "web_search",
        "weather",
        "maps_search",
        "maps_route",
        "browser_navigate",
        "browser_snapshot",
        "browser_read",
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_scroll",
        "browser_delegate",
        "rss_fetch",
        "reddit_comments",
        "hn_comments",
    }
)

# Schedule mutations are the agent editing its own future jobs — including a
# job's autonomy and tool allocation, i.e. its own future privileges. They
# never auto-run and are never allowlistable, in EVERY autonomy mode; but
# unlike DESTRUCTIVE they are not hard-denied unattended — they park a
# deferred approval, so a background job can propose rescheduling itself and
# a human reviews the exact args. schedule_disable is deliberately absent
# (reversible, plain MUTATING_LOCAL). Must stay in sync with the gateway's
# /api/tools allowlistable flag, POST /api/allowlist's 422 and settings.js's always_ask rendering.
ALWAYS_ASK_TOOLS = frozenset({"schedule_add", "schedule_edit", "schedule_delete"})

# The arbitrary-URL egress primitives: the model chooses the full destination,
# so these are additionally gated on the session's EgressLedger (a host the
# user never named and no search surfaced is where exfil goes). web_search/
# weather/maps hit pinned API endpoints — query text still leaks, which the
# secret/probe taints and the budget cover. browser_navigate takes an arbitrary
# url too (the other browser_* tools act on the already-open tab, so their
# destination was gated at navigate time).
DESTINATION_GATED_TOOLS = frozenset(
    {
        "web_fetch",
        "browser_fetch",
        "browser_navigate",
        "rss_fetch",
        # the feeds comment tools take a model-chosen post url — the gate
        # reads args["url"], so the arg is named url for exactly that
        "reddit_comments",
        "hn_comments",
    }
)

# Per-turn ceiling on executed egress calls before each further one needs
# approval. Heavy scheduled research turns legitimately run 15–20 outbound
# calls (observed), so the cap sits well above that while staying below
# useful chunked-exfil volume; override with HARNESS_EGRESS_BUDGET (0 gates
# every outbound call).
DEFAULT_EGRESS_BUDGET = 40

# Machine-readable egress gate kinds. Persisted on the approvals row
# (gate_kind column) so the approval card / push / CLI prompt can explain
# WHY a READ_ONLY call parked — see store/approvals.why_gated. Never re-derive
# "why gated" from classification: the egress gates made that lie.
GATE_SECRET_READ = "secret_read"
GATE_INTERNAL_PROBE = "internal_probe"
GATE_EGRESS_BUDGET = "egress_budget"
GATE_NEW_DESTINATION = "new_destination"
def _secret_read_gate() -> tuple[str, str, str]:
    """(gate_kind, deny_reason, approved_reason) for the secret-read taint —
    single-sourced so the egress gate and the envelope gate share the copy."""
    return (
        GATE_SECRET_READ,
        "a secret-class file was read this turn — outbound "
        "requests are blocked for the rest of the turn",
        "approved by user (secret-class read this turn)",
    )


def _internal_probe_gate() -> tuple[str, str, str]:
    """(gate_kind, deny_reason, approved_reason) for the internal-probe taint."""
    return (
        GATE_INTERNAL_PROBE,
        "an internal/private address was probed this turn — outbound "
        "requests are blocked for the rest of the turn",
        "approved by user (internal address probed this turn)",
    )


@dataclass
class TurnContext:
    """Per-turn state the agent threads into every policy check.

    secret_read flips when a read_file call targets a secret-shaped path
    (core/paths.is_sensitive_read); internal_probe flips when an outbound
    fetch was refused for targeting a private/internal address (attack-in-
    progress signal); egress_calls counts executed EGRESS_TOOLS calls. All
    three reset at the start of the next turn. ledger is a REFERENCE to the
    agent's session-scoped EgressLedger — None (the default, for bare test
    contexts) disables the destination gate, so only Agent.run_turn may
    construct the real TurnContext.
    """

    secret_read: bool = False
    internal_probe: bool = False
    egress_calls: int = 0
    # Executed BROWSER_ACTION_TOOLS calls (navigate/click/type) — the counter a
    # delegation envelope's action budget gates on; reads don't count.
    action_calls: int = 0
    ledger: EgressLedger | None = None


class PolicyEngine:
    def __init__(
        self,
        allowlist: AllowlistStore | None = None,
        autonomy: Autonomy = Autonomy.INTERACTIVE,
        egress_budget: int = DEFAULT_EGRESS_BUDGET,
        *,
        envelope: DelegationEnvelope | None = None,
        tab_host_fn: Callable[[], str | None] | None = None,
        element_host_fn: Callable[[str], str | None] | None = None,
    ) -> None:
        if envelope is not None and autonomy is Autonomy.RESTRICTED:
            raise ValueError(
                "a delegation envelope cannot be combined with RESTRICTED"
            )
        self.allowlist = allowlist
        self.autonomy = autonomy
        self.egress_budget = egress_budget
        self.approver: Optional[Approver] = None
        # A per-run delegation envelope (browser subagent) and a callable that
        # reports the current tab's host — both None for ordinary runs, which
        # keep the unchanged classification/egress flow.
        self.envelope = envelope
        self.envelope_gate = EnvelopeGate(self) if envelope is not None else None
        self.tab_host_fn = tab_host_fn
        # Frame-aware host for a specific element handle: an OOPIF field's
        # REAL destination is the frame's origin, not the tab's. Used by the
        # ref-use gate; None falls back to the tab host (pre-OOPIF behavior).
        self.element_host_fn = element_host_fn

    def _egress_gate_reason(
        self, tool_name: str, args: dict, context: TurnContext
    ) -> tuple[str, str, str] | None:
        """The first egress condition gating this call, as (gate_kind,
        deny_reason, approved_reason) — or None when the call may auto-run.
        Ordering matters: the taints are attack signals and outrank the
        softer gates.
        """
        if tool_name not in EGRESS_TOOLS:
            return None
        if context.secret_read:
            return _secret_read_gate()
        if context.internal_probe:
            return _internal_probe_gate()
        if context.egress_calls >= self.egress_budget:
            return (
                GATE_EGRESS_BUDGET,
                f"egress budget reached ({context.egress_calls} outbound "
                "calls this turn) — further outbound calls are blocked",
                f"approved by user (over the {self.egress_budget}-call "
                "egress budget)",
            )
        if tool_name in DESTINATION_GATED_TOOLS and context.ledger is not None:
            # Keys LITERALLY on args["url"]: a gated tool whose URL arg has any other name silently no-ops the gate.
            host = host_of(str(args.get("url", "")))
            # No parseable host ⇒ skip: the tool's own check_target refuses
            # the URL before any network I/O.
            if host is not None and not context.ledger.allows_host(host):
                return (
                    GATE_NEW_DESTINATION,
                    f"new destination this session: {host} — not named by "
                    "you and not surfaced by a search",
                    f"approved by user (new destination {host})",
                )
        return None

    async def check(
        self,
        tool_name: str,
        args: dict,
        classification: ActionClass,
        context: TurnContext | None = None,
    ) -> tuple[str, str]:
        """Return (decision, reason); decision is auto | approved | denied."""
        # A delegation envelope (browser subagent) owns the six interaction
        # tools entirely: their normal egress/classification gating is replaced
        # by the envelope boundary checks. Everything else falls through
        # unchanged, and with no envelope this branch never runs.
        if self.envelope_gate is not None and tool_name in BROWSER_ENVELOPE_TOOLS:
            return await self.envelope_gate.decide(
                tool_name, args, classification, context
            )

        # Trifecta break + egress hardening: an egress tool loses its
        # read-only auto-run when the turn is tainted (secret read / internal
        # probe), the turn's egress budget is spent, or the destination host
        # was never legitimized this session — a human approves the outbound
        # request instead (and nothing persists; approving an unseen
        # destination admits it to the ledger only because the fetch then
        # executes, agent._dispatch side). This runs BEFORE classification, so for a URL-taking tool
        # "MUTATING_LOCAL + allowlist ⇒ auto" is never the whole story: a new host still parks.
        gate = (
            self._egress_gate_reason(tool_name, args, context)
            if context is not None
            else None
        )
        if gate is not None:
            gate_kind, deny_reason, approved_reason = gate
            if self.autonomy is Autonomy.RESTRICTED or self.approver is None:
                return "denied", deny_reason
            answer = await self.approver(
                tool_name, args, classification, gate_kind=gate_kind
            )
            if answer in ("yes", "always"):
                return "approved", approved_reason
            return "denied", "denied by user"

        if classification is ActionClass.READ_ONLY:
            return "auto", "read-only"

        if self.autonomy is Autonomy.RESTRICTED:
            return "denied", "restricted run: only read-only tools allowed"

        # Always-ask overlay: schedule mutations go to the approver in every
        # mode — never auto (even if somehow allowlisted), never persisted,
        # never hard-denied unattended (deferred approval instead), so a
        # background job can propose rescheduling itself under human review.
        if tool_name in ALWAYS_ASK_TOOLS:
            if self.approver is None:
                return "denied", "no approver available (non-interactive run)"
            answer = await self.approver(tool_name, args, classification)
            if answer in ("yes", "always"):
                return "approved", "approved by user (schedule changes always ask)"
            return "denied", "denied by user"

        if (
            classification is ActionClass.DESTRUCTIVE
            and self.autonomy in (Autonomy.SUPERVISED, Autonomy.AUTONOMOUS)
        ):
            return "denied", "destructive actions are never allowed in unattended runs"

        # Only MUTATING_LOCAL is allowlistable, and the allowlist fires only
        # in INTERACTIVE and AUTONOMOUS runs — autonomous is the user's
        # explicit per-job opt-in, bounded by that job's hard tool allocation.
        # OUTWARD (e.g. sending mail) can never auto-fire — a human must see
        # the full content every time, which also closes the allowlist-by-name
        # hole and the prompt-injection → autonomous-send path. DESTRUCTIVE
        # always prompts (and never persists). Supervised runs ignore the
        # allowlist: their triggers can be attacker-influenced (email → triage),
        # so gated calls always park a deferred approval a human reviews.
        if (
            classification is ActionClass.MUTATING_LOCAL
            and self.autonomy in (Autonomy.INTERACTIVE, Autonomy.AUTONOMOUS)
            and self.allowlist is not None
            and await self.allowlist.contains(tool_name)
        ):
            return "auto", "allowlisted"

        if self.approver is None:
            return "denied", "no approver available (non-interactive run)"

        answer = await self.approver(tool_name, args, classification)
        if answer == "always":
            # Persisting a standing grant stays INTERACTIVE-only: an
            # AUTONOMOUS run's "always" degrades to a plain approval —
            # grants are only ever written by a human in chat.
            if (
                classification is ActionClass.MUTATING_LOCAL
                and self.autonomy is Autonomy.INTERACTIVE
                and self.allowlist
            ):
                await self.allowlist.add(tool_name)
                return "approved", "approved (allowlisted for next time)"
            return "approved", "approved by user"
        if answer == "yes":
            return "approved", "approved by user"
        return "denied", "denied by user"


def deferred_approver(waiter, run_id_fn: Callable[[], str | None]) -> Approver:
    """Approver for supervised runs: park the call on a pending approvals row
    until a human decides (or it times out ⇒ denied). run_id_fn is called at
    request time because the run id doesn't exist when the approver is built."""

    async def approve(
        tool: str,
        args: dict,
        classification: ActionClass,
        gate_kind: str | None = None,
    ) -> str:
        approved = await waiter.request(
            run_id_fn(), tool, args, classification, gate_kind=gate_kind
        )
        return "yes" if approved else "no"

    return approve
