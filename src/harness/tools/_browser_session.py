"""Lane B — one stateful CDP tab the harness agent loop drives interactively.

Unlike browser_fetch (Lane A), which creates and closes a target per call, a
`BrowserSession` holds ONE tab open for the harness session's lifetime and
exposes navigate/snapshot/click/type/scroll/read against it. Reads the page as
an accessibility tree (`Accessibility.getFullAXTree` — NOT a bot signal) and
hands the model stable `[eN]` handles keyed to durable backendDOMNodeIds.

Reuses browser_fetch's transport helpers (ws-url health check, lifecycle-event
settle ordering, stable-DOM poll) and the _netguard SSRF guard verbatim.
NEVER sends any `Runtime.*` command — Runtime.enable is a Cloudflare
bot-detection signal (the whole reason Playwright was retired); the a11y tree
and DOM byte reads need no Runtime domain.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

from ..core.egress import host_of
from ._axtree import (
    _AX_MAX_CHARS,
    _ax_prop,
    _ax_value,
    _quad_center,
    find_select_context,
    format_ax_tree,
)
from ._browser_frames import (
    _AUTO_ATTACH_PARAMS,
    _FRAME_MIN_CHARS,
    FrameInfo,
    FrameRegistry,
    frame_ax_section,
    frame_viewport_offset,
    read_frame_sections,
)
from ._browser_input import (
    clear_field,
    click_at,
    press_enter as send_enter,
    press_key,
    scroll_wheel,
)
from ._cdp import CDPClient, CDPError
from ._extract import render_page
from ._netguard import (
    InternalRefusalNote,
    Refused,
    RefusedInternal,
    Resolver,
    check_target,
    default_resolver,
)
from .browser_fetch import (
    _CDP_PORT,
    _SETTLE_S,
    _STABLE_S,
    ChromeUnavailable,
    _browser_ws_url,
    _lifecycle_match,
    _wait_stable_dom,
)

# The only real sleep in navigate lives inside browser_fetch._wait_stable_dom,
# governed by browser_fetch._POLL_INTERVAL_S — offline tests zero THAT constant
# (plus _SETTLE_S/_STABLE_S below) to drop all waits. Mirrored here for symmetry.
_POLL_INTERVAL_S = 0.5

# How long click/type wait for a resulting main-frame navigation before
# comparing the AX tree for the (un)changed note. Also the settle window that
# lets in-page JS react before the comparison snapshot.
_ACTION_SETTLE_S = 1.2

_DEFAULT_READ_CHARS = 15_000

class BrowserSession:
    """One persistent CDP tab. Lazily connected; driven across many tool calls."""

    def __init__(
        self,
        *,
        cdp_port: int = _CDP_PORT,
        resolver: Resolver = default_resolver,
        ws_url_getter: Callable[[int], Awaitable[str]] = _browser_ws_url,
    ) -> None:
        self._cdp_port = cdp_port
        self._resolve = resolver
        self._ws_url_getter = ws_url_getter
        self._cdp: CDPClient | None = None
        self._sid: str | None = None
        self._target_id: str | None = None
        self._frame_id: str | None = None
        self._current_url: str | None = None
        # handle -> (owning session id, backendDOMNodeId); OOPIF elements
        # belong to their frame's session, main-frame elements to the tab's.
        self._handles: dict[str, tuple[str, int]] = {}
        # handle -> its snapshot line minus the handle ('button "Upload image"'),
        # from the SAME snapshot as _handles — what an approval card shows for
        # a click (gateway/preview.py). Page text: display only, never the model.
        self._labels: dict[str, str] = {}
        # Last AX text the model saw (snapshot/navigate) or that an action
        # compared against — the baseline for the "(page unchanged)" note.
        self._last_ax_text: str | None = None
        # Auto-attached OOPIFs by session id, in attach order.
        self._registry = FrameRegistry()
        self._oopif_unavailable = False

    @property
    def _frames(self) -> dict[str, FrameInfo]:
        return self._registry.frames

    # --- lifecycle ------------------------------------------------------------

    async def connect(self) -> None:
        """Open a fresh tab and enable the domains we drive. Raises
        ChromeUnavailable if the debug endpoint can't be reached."""
        ws_url = await self._ws_url_getter(self._cdp_port)
        self._cdp = await CDPClient.connect(ws_url)
        try:
            created = await self._cdp.send("Target.createTarget", {"url": "about:blank"})
            self._target_id = created["targetId"]
            attached = await self._cdp.send(
                "Target.attachToTarget", {"targetId": self._target_id, "flatten": True}
            )
            self._sid = attached["sessionId"]

            await self._cdp.send("Page.enable", {}, self._sid)
            await self._cdp.send("DOM.enable", {}, self._sid)
            await self._cdp.send("Network.enable", {}, self._sid)
            await self._cdp.send(
                "Page.setLifecycleEventsEnabled", {"enabled": True}, self._sid
            )
            # Runtime.enable is deliberately NEVER sent (CF detection signal).

            tree = await self._cdp.send("Page.getFrameTree", {}, self._sid)
            self._frame_id = tree["frameTree"]["frame"]["id"]
            self._cdp.add_listener(self._on_event)
            # Auto-attach OOPIFs (cross-origin iframes) so snapshot/read can
            # see into them. Best-effort: an old Chrome without it still works,
            # minus iframe visibility.
            try:
                await self._cdp.send(
                    "Target.setAutoAttach", dict(_AUTO_ATTACH_PARAMS), self._sid
                )
            except CDPError:
                self._oopif_unavailable = True
        except BaseException:
            # A half-open session would leak the ws + read-loop task.
            await self.close()
            raise

    def _on_event(self, msg: dict) -> None:
        """Sync bookkeeping off the CDP event stream: the main frame's URL and
        the OOPIF registry. Never awaits, never raises past a bad message."""
        if msg.get("method") == "Page.frameNavigated":
            self._on_frame_navigated(msg)
        else:
            self._registry.on_event(msg)

    def _on_frame_navigated(self, msg: dict) -> None:
        """Track the main frame's URL as it navigates (redirects, SPA nav)."""
        if msg.get("sessionId") != self._sid:
            return
        frame = (msg.get("params") or {}).get("frame") or {}
        # Main frame only: same-process subframes have a different id / a parent.
        if frame.get("id") != self._frame_id or frame.get("parentId"):
            return
        url = frame.get("url")
        if url:
            self._current_url = url

    async def close(self) -> None:
        """Best-effort teardown; never raises."""
        cdp = self._cdp
        if cdp is None:
            return
        self._cdp = None
        if self._target_id:
            try:
                await cdp.send("Target.closeTarget", {"targetId": self._target_id})
            except CDPError:
                pass  # tab may already be gone — cleanup only
        await cdp.close()  # swallows its own transport errors

    # --- state ----------------------------------------------------------------

    @property
    def current_url(self) -> str | None:
        return self._current_url

    @property
    def current_host(self) -> str | None:
        return host_of(self._current_url) if self._current_url else None

    # --- actions --------------------------------------------------------------

    async def navigate(self, url: str) -> str:
        """SSRF-check, navigate, settle, invalidate handles, return a snapshot."""
        try:
            await check_target(url, self._resolve)
        except RefusedInternal as exc:
            # Internal target = attack-in-progress; the typed note taints the turn.
            return InternalRefusalNote(f"browser_navigate error: {exc}")
        except Refused as exc:
            return f"browser_navigate error: {exc}"

        cdp, sid = self._require()
        # Optimistic; Page.frameNavigated (via _on_event) overrides with the
        # real post-redirect URL if it fires during settle.
        self._current_url = url
        # Register the settle waiter BEFORE navigating (see _lifecycle_match).
        idle = cdp.expect_event(_lifecycle_match(sid, "networkAlmostIdle"))
        await cdp.send("Page.navigate", {"url": url}, sid)
        await cdp.wait_event(idle, _SETTLE_S)
        await _wait_stable_dom(cdp, sid, timeout=_STABLE_S)

        self._handles = {}  # stale after navigation
        return await self.snapshot()

    async def snapshot(self) -> str:
        """Read the page's accessibility tree — the main frame plus a labeled
        section per attached OOPIF — and refresh the handle map."""
        text, handles = await self._capture()
        self._handles = handles
        self._labels = {
            m[1]: m[2] for m in re.finditer(r"^\s*\[(e\d+)\] (.+)$", text, re.M)
        }
        self._last_ax_text = text
        return text

    async def _capture(self) -> tuple[str, dict[str, tuple[str, int]]]:
        """One full-page AX capture: main tree first, then iframe sections in
        attach order, sharing the char cap. Frame failures disclose instead of
        raising — the main tree must always render."""
        cdp, sid = self._require()
        res = await cdp.send("Accessibility.getFullAXTree", {}, sid)
        text, main_handles, counter = format_ax_tree(res.get("nodes", []))
        handles: dict[str, tuple[str, int]] = {
            h: (sid, b) for h, b in main_handles.items()
        }
        parts: list[str] = [text] if text else []
        remaining = _AX_MAX_CHARS - len(text)
        unshown = 0
        for frame in list(self._frames.values()):
            if remaining < _FRAME_MIN_CHARS:
                unshown += 1
                continue
            section = await frame_ax_section(cdp, frame, counter, remaining)
            if section is None:
                continue  # empty shell (about:blank, tracking pixel)
            stext, fhandles, counter = section
            handles.update({h: (frame.session_id, b) for h, b in fhandles.items()})
            parts.append(stext)
            remaining -= len(stext)
        if unshown:
            parts.append(f"[tree truncated — {unshown} iframe(s) not shown]")
        return "\n".join(parts), handles

    async def read(self, max_chars: int = _DEFAULT_READ_CHARS) -> str:
        """Byte-derived readable text of the current tab (same pipeline as
        web_fetch/browser_fetch), plus a section per attached OOPIF."""
        cdp, sid = self._require()
        doc = await cdp.send("DOM.getDocument", {"depth": 0}, sid)
        node_id = doc["root"]["nodeId"]
        res = await cdp.send("DOM.getOuterHTML", {"nodeId": node_id}, sid)
        html = res.get("outerHTML", "")
        main = await render_page(
            html,
            self._current_url or "",
            max_chars,
            "auto",
            allow_js_note=False,
            tool_name="browser_read",
        )
        parts = [main] + await read_frame_sections(
            cdp, self._frames, max_chars - len(main)
        )
        return "\n".join(parts)

    async def click(self, handle: str) -> str:
        cdp, sid = self._require()
        point = await self._element_point(handle)
        if isinstance(point, str):
            return point  # stale / not-visible message
        x, y = point
        # Register BEFORE dispatching (expect_event convention) — a click can
        # commit a navigation faster than we get back around to waiting.
        # Mouse events ALWAYS go to the top-level session at viewport coords:
        # the browser process hit-tests them into the right frame (spike
        # 2026-07-18 verified this routes into OOPIFs).
        nav = cdp.expect_event(self._main_nav_predicate())
        try:
            await click_at(cdp, sid, x, y)
        except BaseException:
            cdp.discard_waiter(nav)
            raise
        note = await self._nav_note(nav)
        if note is None:
            note = await self._change_note()
        return f"clicked [{handle}]{note}"

    async def type(
        self, handle: str, text: str, press_enter: bool = False, clear: bool = True
    ) -> str:
        cdp, sid = self._require()
        resolved = self._resolve_live(handle)
        if resolved is None:
            return self._stale(handle)
        owner_sid, backend = resolved
        # Focus/keys/insertText all target the OWNING session — an OOPIF field
        # takes its input on the frame's own session (spike 2026-07-18).
        nav = cdp.expect_event(self._main_nav_predicate())
        try:
            await cdp.send("DOM.focus", {"backendNodeId": backend}, owner_sid)
            if clear:
                await clear_field(cdp, owner_sid)
            await cdp.send("Input.insertText", {"text": text}, owner_sid)
            if press_enter:
                await send_enter(cdp, owner_sid)
        except CDPError:
            cdp.discard_waiter(nav)
            return self._stale(handle)
        except BaseException:
            cdp.discard_waiter(nav)
            raise
        note = await self._nav_note(nav)
        if note is None:
            note = await self._typed_value_note(backend, owner_sid)
        return f"typed into [{handle}]{note}"

    async def select_option(self, handle: str) -> str:
        """Select a dropdown option by its handle: focus the owning <select>,
        then Home + N×ArrowDown on the owning session (mouse clicks cannot
        drive native selects — Chrome draws the dropdown out-of-page)."""
        cdp, sid = self._require()
        resolved = self._resolve_live(handle)
        if resolved is None:
            return self._stale(handle)
        owner_sid, backend = resolved
        # Fresh tree at call time: selection state may have drifted since the
        # model's snapshot, and the enabled-option index must be current.
        res = await cdp.send("Accessibility.getFullAXTree", {}, owner_sid)
        ctx = find_select_context(res.get("nodes", []), backend)
        if isinstance(ctx, str):
            return self._select_refusal(ctx, handle)
        select_backend, arrows = ctx
        nav = cdp.expect_event(self._main_nav_predicate())  # jump menus navigate
        try:
            await cdp.send("DOM.focus", {"backendNodeId": select_backend}, owner_sid)
            await press_key(cdp, owner_sid, "Home", 36)
            for _ in range(arrows):
                await press_key(cdp, owner_sid, "ArrowDown", 40)
        except CDPError:
            cdp.discard_waiter(nav)
            return self._stale(handle)
        except BaseException:
            cdp.discard_waiter(nav)
            raise
        note = await self._nav_note(nav)
        if note is None:
            if not await self._option_selected(backend, owner_sid):
                return (
                    f"pressed the keys but [{handle}] does not appear selected — "
                    "this may not be a native dropdown; try browser_click"
                )
            # Baseline update + change hint; the AX-verified selection makes an
            # "unchanged" warning contradictory, so only the changed note stays.
            note = await self._change_note("selection")
            if "page unchanged" in note:
                note = ""
        return f"selected [{handle}]{note}"

    @staticmethod
    def _select_refusal(code: str, handle: str) -> str:
        if code == "option-not-found":
            return BrowserSession._stale(handle)
        if code == "not-an-option":
            return (
                f"[{handle}] is not a dropdown option — pass the [eN] of an "
                "option row from the snapshot"
            )
        if code == "multiselect":
            return f"[{handle}] is in a multi-select list — not supported"
        if code == "option-disabled":
            return f"option [{handle}] is disabled"
        return f"[{handle}] is not inside a dropdown — use browser_click"

    async def _option_selected(self, backend: int, owner_sid: str) -> bool:
        """Post-keys verification: the option's AX `selected` property. A
        missing node or property yields True (no verdict — don't cry wolf)."""
        cdp, _ = self._require()
        try:
            res = await cdp.send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend, "fetchRelatives": False},
                owner_sid,
            )
        except CDPError:
            return True
        nodes = res.get("nodes") or [{}]
        value = _ax_prop(nodes[0], "selected")
        if value is None:
            return True
        return value in (True, "true")

    def _resolve_live(self, handle: str) -> tuple[str, int] | None:
        """(owning session, backendNodeId) for a handle, or None when unknown
        or the owning frame has detached since the snapshot."""
        if self._sid is None:
            return None
        resolved = self._handles.get(handle)
        if resolved is None:
            return None
        owner_sid, backend = resolved
        if owner_sid != self._sid and owner_sid not in self._frames:
            return None
        return owner_sid, backend

    async def _element_point(self, handle: str) -> tuple[float, float] | str:
        """The element's center in VIEWPORT coordinates, or the model-facing
        error string. OOPIF quads are frame-relative (spike 2026-07-18) and get
        the frame's accumulated offset added."""
        cdp, sid = self._require()
        resolved = self._resolve_live(handle)
        if resolved is None:
            return self._stale(handle)
        owner_sid, backend = resolved
        try:
            await cdp.send(
                "DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend}, owner_sid
            )
            got = await cdp.send(
                "DOM.getContentQuads", {"backendNodeId": backend}, owner_sid
            )
            quads = got.get("quads", [])
            if not quads:
                return f"element not visible: [{handle}]"
            x, y = _quad_center(quads[0])
            frame = self._frames.get(owner_sid)
            if frame is not None:
                ox, oy = await frame_viewport_offset(cdp, sid, self._frames, frame)
                x, y = x + ox, y + oy
            return x, y
        except CDPError:
            return self._stale(handle)

    async def scroll(self, direction: str, pixels: int = 600) -> str:
        cdp, sid = self._require()
        await scroll_wheel(cdp, sid, direction, pixels)
        return f"scrolled {direction}"

    # --- helpers --------------------------------------------------------------

    def _main_nav_predicate(self) -> Callable[[dict], bool]:
        """Matches a MAIN-frame Page.frameNavigated on this tab (mirrors
        _on_event's filter — subframes never count as 'the page navigated')."""
        sid, frame_id = self._sid, self._frame_id

        def match(msg: dict) -> bool:
            if msg.get("method") != "Page.frameNavigated" or msg.get("sessionId") != sid:
                return False
            frame = (msg.get("params") or {}).get("frame") or {}
            return frame.get("id") == frame_id and not frame.get("parentId")

        return match

    async def _nav_note(self, nav) -> str | None:
        """Consume the post-action navigation waiter. Navigation committed →
        settle, invalidate handles (they point into the gone document), return
        the suffix; no navigation within _ACTION_SETTLE_S → None. The timeout
        doubles as the settle window before the change-note comparison."""
        cdp, sid = self._require()
        if await cdp.wait_event(nav, _ACTION_SETTLE_S) is None:
            return None
        await _wait_stable_dom(cdp, sid, timeout=_STABLE_S)
        self._handles = {}
        dest = self._current_url or "a new page"
        return f" → page navigated to {dest}; take a new browser_snapshot"

    async def _change_note(self, action: str = "click") -> str:
        """Did the action visibly change the page? Compares the fresh capture
        (main + iframe sections — same text snapshot() builds) against the
        last one; the fresh handles are DISCARDED — the model's [eN] numbering
        stays pinned to its last browser_snapshot."""
        text, _ = await self._capture()
        changed = text != self._last_ax_text
        self._last_ax_text = text
        if changed:
            return " (page changed — take a new browser_snapshot)"
        return f" (page unchanged — the {action} may have had no effect)"

    async def _typed_value_note(self, backend: int, owner_sid: str) -> str:
        """Verify typing registered by reading the node's AXValue (the AX text
        never renders values, so _change_note can't see a successful type).
        The value itself NEVER appears in the note — it may be a resolved
        identity ref."""
        cdp, _ = self._require()
        try:
            res = await cdp.send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": backend, "fetchRelatives": False},
                owner_sid,
            )
        except CDPError:
            return ""  # node gone (widget re-rendered) — no verdict either way
        nodes = res.get("nodes") or [{}]
        if _ax_value(nodes[0].get("value")):
            return ""
        return " (the field appears empty after typing — the input may not have registered)"

    def _require(self) -> tuple[CDPClient, str]:
        if self._cdp is None or self._sid is None:
            raise RuntimeError("BrowserSession is not connected — call connect() first.")
        return self._cdp, self._sid

    @staticmethod
    def _stale(handle: str) -> str:
        return f"stale handle [{handle}]: take a new browser_snapshot"

    def label_for_handle(self, handle: str) -> str | None:
        """What the handle names in the model's last snapshot ('button
        "Upload image"'), or None once it is stale (navigated away)."""
        return self._labels.get(handle) if handle in self._handles else None

    def host_for_handle(self, handle: str) -> str | None:
        """The host an interaction with this handle actually reaches: the
        owning FRAME's host for OOPIF elements, the tab's for main-frame ones.
        None when the handle is unknown, the frame detached, or its URL is not
        yet known — callers must treat None as unverifiable, not as the tab."""
        resolved = self._resolve_live(handle)
        if resolved is None:
            return None
        owner_sid, _ = resolved
        if owner_sid == self._sid:
            return self.current_host
        frame = self._frames.get(owner_sid)
        if frame is None or not frame.url:
            return None
        return host_of(frame.url)



