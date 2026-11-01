"""Identity-ref resolution — {email}/{phone} placeholders → real stored values
at dispatch time, WITHOUT the value ever entering LLM context, audit, traces,
or session history.

A delegated browser subagent (chunk 6) types `{email}` into a form field; this
resolver swaps in the real value stored in ClientConfigStore only at the moment
the CDP Input.insertText fires. Every other surface (tool args, the tool result
string, run traces) keeps the placeholder. The resolver is the run-time
enforcement half of the policy engine's envelope check: policy is the SOLE
granter of ref_resolved_hosts (it prompts the human on first use per site); this
resolver only re-verifies the granted set at dispatch, closing the check→dispatch
TOCTOU (the tab could have moved between the two).

NEVER log, print, or return a resolved value — the error strings below are
placeholder-only by construction so a missing/wrong value can't leak here.
"""

from __future__ import annotations

from ..core.envelope import DelegationEnvelope, find_refs, host_within
from ..store.client_config import REF_KEYS, ClientConfigStore
from ._browser_manager import BrowserSessionManager


class RefResolver:
    """Substitutes granted identity refs into typed text at dispatch time.

    Bound to one delegation envelope + the shared browser manager (for the live
    current host) + the client-config store (for the values). Only chunk 6's
    child agent factory wires one in; the M1/main-catalog BrowserTypeTool gets
    None and never resolves refs.
    """

    def __init__(
        self,
        client_config: ClientConfigStore,
        envelope: DelegationEnvelope,
        manager: BrowserSessionManager,
    ) -> None:
        self._client_config = client_config
        self._envelope = envelope
        self._manager = manager

    async def resolve(self, text: str, handle: str) -> tuple[str, str | None]:
        """Return (substituted_text, error) for text typed into `handle`.

        error is a placeholder-only message; when it is non-None NO substitution
        happened (the caller must not type). On success error is None and every
        {ref} present has been replaced with its real stored value.
        """
        used = find_refs(text)
        if not used:
            return text, None  # no refs — pass through untouched

        # Defense in depth: policy already hard-denies an ungranted ref, but a
        # resolver reached without the capability must refuse, never resolve.
        ungranted = used - self._envelope.refs
        if ungranted:
            bad = sorted(ungranted)[0]
            return text, f"this delegation has no {{{bad}}} capability"

        # Re-verify the element's REAL destination against the granted set at
        # dispatch (the TOCTOU close): frame-aware — an OOPIF field belongs to
        # the frame's origin, and a frame that navigated between policy check
        # and dispatch is caught here. Policy is the sole granter of
        # ref_resolved_hosts; this only reads it.
        host = self._manager.element_host(handle)
        if host is None or not host_within(host, self._envelope.ref_resolved_hosts):
            return text, "ref use on this site has not been approved"

        # Fetch each used ref's value; a missing/empty value names ONLY the ref
        # placeholder — never another ref's stored value.
        substituted = text
        for ref in used:
            value = await self._client_config.get(REF_KEYS[ref])
            if not value:
                return (
                    text,
                    f"no saved {{{ref}}} — set it in Settings → Contact details",
                )
            substituted = substituted.replace(f"{{{ref}}}", value)
        return substituted, None
