"""Lane B's OOPIF layer: cross-origin iframes that Chrome auto-attaches as
flattened targets. FrameRegistry tracks the attach/detach events; the
functions render one frame's AX/read sections and resolve its viewport
offset. No BrowserSession state crosses in — everything takes the CDPClient
and the frame dicts explicitly.

NEVER sends any `Runtime.*` command on a frame session (CF bot-detection
signal — see _browser_session's module docstring).
"""

from __future__ import annotations

from dataclasses import dataclass

from ._axtree import format_ax_tree
from ._cdp import CDPClient, CDPError
from ._extract import UNTRUSTED_NOTE, render_page

# Below this much remaining char budget an iframe section isn't attempted.
_FRAME_MIN_CHARS = 400
# An OOPIF whose outerHTML is under this is an empty shell / tracking pixel.
_EMPTY_FRAME_HTML_CHARS = 200

# waitForDebuggerOnStart MUST stay False: resuming a paused target requires
# Runtime.runIfWaitingForDebugger, and the Runtime domain is forbidden (CF
# bot-detection signal — see the _browser_session docstring).
_AUTO_ATTACH_PARAMS = {
    "autoAttach": True,
    "waitForDebuggerOnStart": False,
    "flatten": True,
}


@dataclass
class FrameInfo:
    """One auto-attached OOPIF (cross-origin iframe) target on this tab."""

    session_id: str
    target_id: str  # == the OOPIF's frameId
    parent_session_id: str  # the tab's sid, or another frame's when nested
    url: str
    initialized: bool = False  # child-session domains enabled
    failed: str | None = None  # sticky init/capture failure → disclosure line


class FrameRegistry:
    """The auto-attached OOPIFs of one tab, keyed by session id, in attach
    order. Fed the CDP event stream; only iframes ever join (workers etc.
    must never receive commands on their sessions)."""

    def __init__(self) -> None:
        self.frames: dict[str, FrameInfo] = {}

    def on_event(self, msg: dict) -> None:
        """Sync bookkeeping off Target.* events. Never raises past a bad
        message."""
        method = msg.get("method")
        if method == "Target.attachedToTarget":
            params = msg.get("params") or {}
            info = params.get("targetInfo") or {}
            sid = params.get("sessionId")
            if info.get("type") == "iframe" and sid:
                self.frames[sid] = FrameInfo(
                    session_id=sid,
                    target_id=str(info.get("targetId") or ""),
                    parent_session_id=str(msg.get("sessionId") or ""),
                    url=str(info.get("url") or ""),
                )
        elif method == "Target.detachedFromTarget":
            sid = (msg.get("params") or {}).get("sessionId")
            self.frames.pop(sid, None)
        elif method == "Target.targetInfoChanged":
            info = (msg.get("params") or {}).get("targetInfo") or {}
            for frame in self.frames.values():
                if frame.target_id == info.get("targetId"):
                    frame.url = str(info.get("url") or frame.url)


async def ensure_frame_ready(cdp: CDPClient, frame: FrameInfo) -> bool:
    """Lazily enable the child session's domains (DOM only — NEVER Runtime)
    and auto-attach so nested OOPIFs surface on later snapshots."""
    if frame.failed is not None:
        return False
    if frame.initialized:
        return True
    try:
        await cdp.send("DOM.enable", {}, frame.session_id)
        await cdp.send(
            "Target.setAutoAttach", dict(_AUTO_ATTACH_PARAMS), frame.session_id
        )
    except CDPError as exc:
        frame.failed = str(exc)
        return False
    frame.initialized = True
    return True


async def frame_ax_section(
    cdp: CDPClient, frame: FrameInfo, start: int, max_chars: int
) -> tuple[str, dict[str, int], int] | None:
    """One '--- iframe: url ---' snapshot section, or None for an empty
    frame. The frame's existence is always disclosed on failure."""
    header = f"--- iframe: {frame.url or 'about:blank'} ---"
    if not await ensure_frame_ready(cdp, frame):
        return f"{header}\n[content could not be captured]", {}, start
    try:
        res = await cdp.send("Accessibility.getFullAXTree", {}, frame.session_id)
    except CDPError as exc:
        frame.failed = str(exc)
        return f"{header}\n[content could not be captured]", {}, start
    text, handles, nxt = format_ax_tree(
        res.get("nodes", []), start=start, max_chars=max_chars
    )
    if not text:
        return None
    return f"{header}\n{text}", handles, nxt


async def read_frame_sections(
    cdp: CDPClient, frames: dict[str, FrameInfo], remaining: int
) -> list[str]:
    """One extracted-text section per attached OOPIF within what's left of
    the char budget. The top-of-output untrusted note covers the whole
    read — each section strips its own copy."""
    parts: list[str] = []
    for frame in list(frames.values()):
        if remaining < _FRAME_MIN_CHARS:
            break
        header = f"--- iframe: {frame.url or 'about:blank'} ---"
        if not await ensure_frame_ready(cdp, frame):
            parts.append(f"{header}\n[could not be read]")
            continue
        try:
            doc = await cdp.send("DOM.getDocument", {"depth": 0}, frame.session_id)
            res = await cdp.send(
                "DOM.getOuterHTML", {"nodeId": doc["root"]["nodeId"]},
                frame.session_id,
            )
        except CDPError:
            parts.append(f"{header}\n[could not be read]")
            continue
        html = res.get("outerHTML", "")
        if len(html) < _EMPTY_FRAME_HTML_CHARS:
            continue  # empty shell — nothing to read
        text = await render_page(
            html, frame.url or "", remaining, "auto",
            allow_js_note=False, tool_name="browser_read",
        )
        body = text.removeprefix(UNTRUSTED_NOTE + "\n")
        section = f"{header}\n{body}"
        parts.append(section)
        remaining -= len(section)
    return parts


async def frame_viewport_offset(
    cdp: CDPClient, tab_sid: str, frames: dict[str, FrameInfo], frame: FrameInfo
) -> tuple[float, float]:
    """Viewport offset of an OOPIF: the top-left of its <iframe> element in
    the parent (DOM.getFrameOwner), accumulated up the chain for nested
    frames. Raises CDPError when an owner can't be resolved."""
    ox, oy = 0.0, 0.0
    current: FrameInfo | None = frame
    for _ in range(8):  # nested-depth bound
        if current is None:
            break
        parent_sid = (
            current.parent_session_id
            if current.parent_session_id in frames
            else tab_sid
        )
        owner = await cdp.send(
            "DOM.getFrameOwner", {"frameId": current.target_id}, parent_sid
        )
        owner_backend = owner.get("backendNodeId")
        if owner_backend is None:
            raise CDPError(f"no frame owner for frame {current.target_id}")
        quads = (
            await cdp.send(
                "DOM.getContentQuads",
                {"backendNodeId": owner_backend},
                parent_sid,
            )
        ).get("quads") or [[0.0] * 8]
        ox += quads[0][0]
        oy += quads[0][1]
        if parent_sid == tab_sid:
            break
        current = frames.get(parent_sid)
    return ox, oy
