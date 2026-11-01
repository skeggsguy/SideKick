"""Approval-preview fields for the Lane B browser tools (split from preview.py,
which owns the dispatch and PreviewError; these return None where it raises).

A click/select/type arg is a bare [eN] handle, which names nothing a human
can review. The element comes from the live tab the parked run drives: its
line in the model's last snapshot ('button "Upload image"') and its host.
That is PAGE text: the card renders it via esc/textContent, the push shows
it as plain text, and it never reaches the model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..core.envelope import find_refs
from ..store.client_config import REF_KEYS
from ..tools._browser_manager import manager_for_run

if TYPE_CHECKING:
    from ..store.client_config import ClientConfigStore

_CLIP = 300  # the same snippet cap as preview.py's _SNIPPET_CHARS


def _clip(text: str) -> str:
    return text[:_CLIP] + ("…" if len(text) > _CLIP else "")


def element_fields(args: dict, run_id: str | None, key: str) -> dict | None:
    """{key: the element's snapshot line, "on": its host}, or None when the
    run has no live tab or the handle is stale/unknown."""
    manager = manager_for_run(run_id)
    handle = str(args.get("handle", "")).strip()
    label = manager.element_label(handle) if manager else None
    if label is None:
        return None
    out = {key: _clip(label)}
    host = manager.element_host(handle)
    if host:
        out["on"] = host
    return out


async def typed_fields(args: dict, client_config: "ClientConfigStore | None") -> dict | None:
    """When the typed text carries {email}/{phone} placeholders, resolve each
    to the REAL stored value so the human approves what will actually be
    filled — the value is NOT in the args (only the placeholder is). With no
    placeholders, just echo the text under review. None = placeholders but no
    store wired (the caller degrades to raw args, which show only the
    placeholder — never a value).

    This is the ONE place a resolved value is surfaced to the human, at their
    own request (the approval card) — never into the model's context."""
    text = str(args.get("text", ""))
    used = find_refs(text)
    if not used:
        return {"will type": _clip(text)}
    if client_config is None:
        return None
    # Flat string value (not a nested dict): the web UI's generic resolver
    # renderer does String(v), so a dict would show as "[object Object]".
    fills = []
    for ref in sorted(used):
        value = await client_config.get(REF_KEYS[ref])
        fills.append(f"{ref} = {value if value else '(not set)'}")
    return {"will fill": "; ".join(fills)}
