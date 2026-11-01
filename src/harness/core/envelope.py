"""Delegation envelope — the per-subagent-run authority record.

A browser subagent (spawned by the main agent) runs under one envelope: the
human approves it ONCE at spawn, and inside its {origin, task, budget, refs}
the subagent's browser actions auto-run. The policy engine (core/policy.py)
re-prompts ONLY at machinery-detected boundaries — the tab moving off the
approved origin, the first use of an identity ref on a site, budget
exhaustion. Empty refs make filling contact details MECHANICALLY impossible
(a hard deny, not a prompt): the capability was never granted at spawn.

This module is pure and depends only on core.egress + stdlib — it must never
import policy (policy imports it). Host matching mirrors EgressLedger.allows_host
(exact normalized host or subdomain-of-any).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .egress import _normalize_host

#: The identity refs a subagent may be granted at spawn. `find_refs` matches
#: exactly the literal placeholder tokens {email} / {phone} in typed text.
KNOWN_REFS = frozenset({"email", "phone"})

# Only the two literal placeholder tokens — never a generic {name} or a stray
# brace. Case-sensitive by design: the subagent inserts these verbatim.
_REF_TOKEN = re.compile(r"\{(email|phone)\}")


def _normalize_origin(raw: str) -> str:
    """Normalize a spawn-time origin to a bare host (scheme/www/case stripped).

    Rejects anything non-host-like — empty, containing a path or whitespace, or
    without a dot — with ValueError, so an envelope can only ever be scoped to a
    real hostname.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("delegation origin is empty")
    # Strip a leading scheme if the caller passed a URL-shaped origin.
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    if " " in raw or "/" in raw:
        raise ValueError(f"delegation origin is not host-like: {raw!r}")
    host = _normalize_host(raw)
    if not host or "." not in host:
        raise ValueError(f"delegation origin is not host-like: {raw!r}")
    return host


def host_within(host: str | None, hosts: set[str]) -> bool:
    """True when `host` equals or is a subdomain of any host in `hosts`.

    Same semantics as EgressLedger.allows_host: exact normalized match or
    endswith("." + h). A None host (no parseable host) is never within.
    """
    if host is None:
        return False
    normalized = _normalize_host(host)
    if not normalized:
        return False
    return normalized in hosts or any(
        normalized.endswith("." + h) for h in hosts
    )


def find_refs(text: str) -> frozenset[str]:
    """The identity-ref placeholder tokens ({email}/{phone}) present in text."""
    return frozenset(_REF_TOKEN.findall(text))


@dataclass
class DelegationEnvelope:
    """One subagent run's authority: where it may act, what it's doing, how
    many actions it may take, and which identity refs it may fill.

    origin is normalized to a bare host at construction; approved_hosts is
    seeded with it. refs must be a subset of KNOWN_REFS and budget >= 1.
    approved_hosts / ref_resolved_hosts grow as the human clears boundaries
    (origin changes, first ref use per site) — the policy engine mutates them
    in place across the run.
    """

    origin: str
    task: str
    budget: int = 40
    refs: frozenset[str] = frozenset()
    approved_hosts: set[str] = field(default_factory=set)
    ref_resolved_hosts: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.origin = _normalize_origin(self.origin)
        self.refs = frozenset(self.refs)
        if not (self.refs <= KNOWN_REFS):
            raise ValueError(
                f"unknown refs {sorted(self.refs - KNOWN_REFS)} "
                f"(known: {sorted(KNOWN_REFS)})"
            )
        if self.budget < 1:
            raise ValueError(f"delegation budget must be >= 1, got {self.budget}")
        self.approved_hosts.add(self.origin)
