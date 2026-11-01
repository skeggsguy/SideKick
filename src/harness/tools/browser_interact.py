"""Lane B interaction tools — six thin Tool wrappers over one BrowserSession.

The harness agent drives ONE persistent Chrome tab across a turn: navigate to a
page, snapshot its accessibility tree for [eN] handles, then click / type /
scroll by handle, re-snapshotting whenever the page changes. All six share a
single injected BrowserSessionManager so the tab persists between calls.

Only browser_navigate lazily connects to Chrome (and pre-checks the target for
SSRF before touching it); the other five operate on the already-open tab and
refuse with an actionable message when none exists — they never auto-connect,
so the model can't silently spin up a tab by reading/clicking. Every body is
wrapped so an unexpected CDPError surfaces as an error string, never a raise
into the agent loop (Tool.run is contractually -> str).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from ..core.envelope import find_refs
from ..core.policy import ActionClass
from ._browser_manager import BrowserSessionManager
from ._browser_session import BrowserSession
from ._cdp import CDPError
from ._refs import RefResolver
from ._netguard import (
    InternalRefusalNote,
    Refused,
    RefusedInternal,
    check_target,
)
from .base import Tool
from .browser_fetch import ChromeUnavailable

#: The interaction tool names, exported so the composition root can build the
#: shared manager once and the policy envelope set has one source of truth
#: (cross-checked against BROWSER_ENVELOPE_TOOLS in test_envelope_policy.py).
BROWSER_INTERACTION_TOOL_NAMES = frozenset(
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

_NO_SESSION_MSG = (
    "no open browser session — call browser_navigate first to open a page, "
    "then snapshot / click / type against it."
)


async def _on_session(
    manager: BrowserSessionManager,
    tool_name: str,
    action: Callable[[BrowserSession], Awaitable[str]],
) -> str:
    """Run `action` against the already-open session (never auto-connecting).
    Returns an actionable message when no session exists and turns an
    unexpected CDPError into a string rather than raising into the turn."""
    session = manager.get()
    if session is None:
        return _NO_SESSION_MSG
    try:
        return await action(session)
    except CDPError as exc:
        return f"{tool_name} error: {exc}"


class BrowserNavigateTool(Tool):
    name = "browser_navigate"
    description = (
        "Open a URL in the persistent interactive browser and return a snapshot "
        "of the page as an accessibility tree with [eN] handles (e.g. e14). "
        "This is the entry point for interactive browsing: navigate here, then "
        "act on the same tab with browser_snapshot / browser_click / "
        "browser_type / browser_scroll, or read its text with browser_read. "
        "Re-snapshot after the page changes. Public web only; private and "
        "internal addresses are refused."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to open."},
        },
        "required": ["url"],
    }
    classification = ActionClass.OUTWARD
    remote = True

    def __init__(self, manager: BrowserSessionManager) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        # SSRF pre-check BEFORE get_or_create: get_or_create lazily connects to
        # Chrome first, so without this a private URL would open a real (idle)
        # tab before navigate()'s own check refused it. navigate() keeps that
        # internal check as defense in depth; this shares the manager's resolver.
        try:
            await check_target(url, self._manager.resolver)
        except RefusedInternal as exc:
            # Internal target = attack-in-progress; the typed note taints the turn.
            return InternalRefusalNote(f"browser_navigate error: {exc}")
        except Refused as exc:
            return f"browser_navigate error: {exc}"

        try:
            session = await self._manager.get_or_create()
        except ChromeUnavailable as exc:
            return str(exc)  # already carries the start-the-services remedy
        try:
            return await session.navigate(url)
        except CDPError as exc:
            return f"browser_navigate error: {exc}"


class BrowserSnapshotTool(Tool):
    name = "browser_snapshot"
    description = (
        "Re-read the current interactive browser tab as an accessibility tree "
        "with fresh [eN] handles (e.g. e14). Call this after any navigation or "
        "page change to get valid handles before browser_click / browser_type. "
        "Embedded widgets (cross-origin iframes, e.g. checkout/booking forms) "
        "appear as '--- iframe: <url> ---' sections labeled with their site. "
        "Requires an open session — call browser_navigate first."
    )
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(self, manager: BrowserSessionManager) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        return await _on_session(self._manager, self.name, lambda s: s.snapshot())


class BrowserReadTool(Tool):
    name = "browser_read"
    description = (
        "Return the current interactive browser tab's readable text (the same "
        "HTML-to-text pipeline as web_fetch) — for reading page content rather "
        "than acting on it. Embedded widgets (cross-origin iframes) are "
        "appended as '--- iframe: <url> ---' sections. Optional max_chars caps "
        "the returned text. Requires an open session — call browser_navigate "
        "first."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "max_chars": {
                "type": "integer",
                "description": "Truncate the returned text to this many characters.",
            },
        },
    }
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(self, manager: BrowserSessionManager) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        raw = args.get("max_chars")
        max_chars: int | None = None
        if raw is not None:
            try:
                max_chars = int(raw)
            except (TypeError, ValueError):
                max_chars = None

        async def action(s: BrowserSession) -> str:
            return await s.read() if max_chars is None else await s.read(max_chars)

        return await _on_session(self._manager, self.name, action)


class BrowserClickTool(Tool):
    name = "browser_click"
    description = (
        "Click an element in the interactive browser tab by its [eN] handle "
        '(e.g. "e14") from the latest browser_snapshot. The page may change — '
        "take a new browser_snapshot afterwards. Requires an open session — "
        "call browser_navigate first."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": 'An element handle from the latest browser_snapshot, e.g. "e14".',
            },
        },
        "required": ["handle"],
    }
    classification = ActionClass.OUTWARD
    remote = True

    def __init__(self, manager: BrowserSessionManager) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        handle = str(args.get("handle", "")).strip()
        return await _on_session(self._manager, self.name, lambda s: s.click(handle))


class BrowserTypeTool(Tool):
    name = "browser_type"
    description = (
        "Type text into a field in the interactive browser tab by its [eN] "
        'handle (e.g. "e14") from the latest browser_snapshot. clear (default '
        "true) selects and clears the field first; press_enter (default false) "
        "presses Enter after typing. Re-snapshot afterwards if the page "
        "changes. Requires an open session — call browser_navigate first."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": 'An element handle from the latest browser_snapshot, e.g. "e14".',
            },
            "text": {"type": "string", "description": "The text to type into the field."},
            "press_enter": {
                "type": "boolean",
                "description": "Press Enter after typing (default false).",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the field before typing (default true).",
            },
        },
        "required": ["handle", "text"],
    }
    classification = ActionClass.OUTWARD
    remote = True

    def __init__(
        self,
        manager: BrowserSessionManager,
        ref_resolver: RefResolver | None = None,
    ) -> None:
        self._manager = manager
        # None in the main catalog / M1 use; browser_delegate's child factory
        # passes a resolver bound to that run's envelope + client config.
        self._ref_resolver = ref_resolver

    async def run(self, args: dict[str, Any]) -> str:
        handle = str(args.get("handle", "")).strip()
        text = str(args.get("text", ""))
        press_enter = bool(args.get("press_enter", False))
        clear = bool(args.get("clear", True))

        # `text` may carry {email}/{phone} placeholders. `to_type` is what
        # actually reaches the field — the ONLY surface the real value may
        # touch. `text` (and thus args, the result string, and the trace) keeps
        # the placeholder so no resolved identity value enters LLM context,
        # audit, or session history.
        to_type = text
        if self._ref_resolver is not None:
            to_type, err = await self._ref_resolver.resolve(text, handle)
            if err is not None:
                return f"browser_type error: {err}"
        elif find_refs(text):
            # No resolver but the model asked for a ref: refuse rather than
            # silently type the literal "{email}" into the page.
            return (
                "browser_type error: identity refs are only available in a "
                "delegated browser run"
            )

        # The result string ("typed into [eN]") deliberately echoes NEITHER the
        # original text NOR the substituted value — it must never carry the
        # resolved identity value into the model's context or the run trace.
        return await _on_session(
            self._manager,
            self.name,
            lambda s: s.type(handle, to_type, press_enter=press_enter, clear=clear),
        )


class BrowserSelectTool(Tool):
    name = "browser_select"
    description = (
        "Select an option in a dropdown (native <select>) in the interactive "
        'browser tab. Pass the OPTION\'s [eN] handle (e.g. "e29") — an '
        '"option" row from the latest browser_snapshot, usually listed under '
        "a combobox. Use this for dropdown menus: clicking an option row does "
        "NOT work on native dropdowns (the tool drives the selection by "
        "keyboard instead, firing real change events). Re-snapshot afterwards "
        "if the page changes. Requires an open session — call browser_navigate "
        "first."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": 'The option\'s handle from the latest browser_snapshot, e.g. "e29".',
            },
        },
        "required": ["handle"],
    }
    classification = ActionClass.OUTWARD
    remote = True

    def __init__(self, manager: BrowserSessionManager) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        handle = str(args.get("handle", "")).strip()
        return await _on_session(
            self._manager, self.name, lambda s: s.select_option(handle)
        )


class BrowserScrollTool(Tool):
    name = "browser_scroll"
    description = (
        "Scroll the interactive browser tab up or down (default 600px) to bring "
        "off-screen content into view, then take a new browser_snapshot to pick "
        "up newly rendered handles. Requires an open session — call "
        "browser_navigate first."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Scroll direction.",
            },
            "pixels": {
                "type": "integer",
                "description": "How far to scroll, in pixels (default 600).",
            },
        },
        "required": ["direction"],
    }
    classification = ActionClass.MUTATING_LOCAL
    remote = True

    def __init__(self, manager: BrowserSessionManager) -> None:
        self._manager = manager

    async def run(self, args: dict[str, Any]) -> str:
        direction = str(args.get("direction", "")).strip().lower()
        if direction not in ("up", "down"):
            return "browser_scroll error: direction must be 'up' or 'down'."
        raw = args.get("pixels", 600)
        try:
            pixels = int(raw)
        except (TypeError, ValueError):
            pixels = 600
        return await _on_session(
            self._manager, self.name, lambda s: s.scroll(direction, pixels)
        )


def browser_interaction_tools(
    manager: BrowserSessionManager,
    ref_resolver: RefResolver | None = None,
) -> list[Tool]:
    """The seven Lane B tools, all sharing one persistent-tab manager. Only
    browser_type takes the identity-ref resolver (chunk 6's child factory
    passes one; the main catalog leaves it None)."""
    return [
        BrowserNavigateTool(manager),
        BrowserSnapshotTool(manager),
        BrowserReadTool(manager),
        BrowserClickTool(manager),
        BrowserTypeTool(manager, ref_resolver),
        BrowserSelectTool(manager),
        BrowserScrollTool(manager),
    ]
