"""BrowserSession (Lane B) — offline against a FakeChrome speaking CDP over a
real websockets server on 127.0.0.1:0.

Mirrors tests/test_browser_fetch.py's scaffolding (real websockets.serve,
scripted CDP, recorded commands, zeroed poll/settle sleeps) but scripts the
interaction surface: Accessibility.getFullAXTree, DOM.getContentQuads, Input.*.
Every command is recorded WITH ITS PARAMS so coordinate math and key sequences
can be asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import websockets

import harness.tools._browser_session as bs
import harness.tools.browser_fetch as bf
from harness.core.policy import ActionClass
from harness.tools._browser_manager import BrowserSessionManager
from harness.tools._browser_session import BrowserSession
from harness.tools._netguard import InternalRefusalNote
from harness.tools.browser_fetch import ChromeUnavailable
from harness.core.envelope import DelegationEnvelope
from harness.store.client_config import REF_EMAIL_KEY, ClientConfigStore
from harness.tools._refs import RefResolver
from harness.tools.browser_interact import (
    BROWSER_INTERACTION_TOOL_NAMES,
    BrowserClickTool,
    BrowserNavigateTool,
    BrowserReadTool,
    BrowserScrollTool,
    BrowserSelectTool,
    BrowserSnapshotTool,
    BrowserTypeTool,
    browser_interaction_tools,
)

PAGE_HTML = "<html><head><title>T</title></head><body><p>hello lane b</p></body></html>"

AX_NODES = [
    {"role": {"value": "heading"}, "name": {"value": "Book a table"}, "ignored": False},
    {
        "role": {"value": "textbox"},
        "name": {"value": "Name"},
        "ignored": False,
        "backendDOMNodeId": 111,
    },
    {
        "role": {"value": "button"},
        "name": {"value": "Reserve now"},
        "ignored": False,
        "backendDOMNodeId": 222,
    },
]

# A quad whose center is deterministic: mean x = 60, mean y = 40.
DEFAULT_QUAD = [10, 20, 110, 20, 110, 60, 10, 60]

# The <iframe> element's quad in its parent — top-left (200, 150), so a frame
# element at frame-relative center (60, 40) lands at viewport (260, 190).
OWNER_QUAD = [200, 150, 800, 150, 800, 550, 200, 550]


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(bf, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(bs, "_SETTLE_S", 0.2)
    monkeypatch.setattr(bs, "_STABLE_S", 0.5)
    monkeypatch.setattr(bs, "_ACTION_SETTLE_S", 0.2)


async def _public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


class FakeChrome:
    """Scripts CDP responses + events for interactive Lane B driving."""

    def __init__(
        self,
        *,
        ax_nodes: list | None = None,
        quads: list | None = None,
        html: str = PAGE_HTML,
        error_backend: int | None = None,
        frame_navigated: list | None = None,
        frame_id: str = "F1",
        ax_nodes_after_click: list | None = None,
        navigate_on_click: dict | None = None,
        ax_value_after_type: str = "typed-something",
        ax_selected_after_select: bool = True,
        navigate_on_key: dict | None = None,
        iframes: list[dict] | None = None,
        worker_targets: list[dict] | None = None,
        error_sessions: set[str] | None = None,
    ) -> None:
        self.ax_nodes = ax_nodes if ax_nodes is not None else AX_NODES
        self.quads = quads if quads is not None else [DEFAULT_QUAD]
        self.html = html
        self.error_backend = error_backend
        self.frame_navigated = frame_navigated or []
        self.frame_id = frame_id
        # Phase A knobs: swap the AX tree once a click lands / emit a
        # main-frame navigation after the click / script the typed AXValue.
        self.ax_nodes_after_click = ax_nodes_after_click
        self.navigate_on_click = navigate_on_click
        self.ax_value_after_type = ax_value_after_type
        # browser_select knobs: the option's post-keys AX selected state, and
        # an optional jump-menu navigation fired after an ArrowDown keyUp.
        self.ax_selected_after_select = ax_selected_after_select
        self.navigate_on_key = navigate_on_key
        # Phase B knobs: OOPIF targets announced after navigate (attach events
        # ride the parent's session, like real flatten-mode Chrome); worker
        # targets to assert they're ignored; sessions that error every command.
        self.iframes = iframes or []
        self.worker_targets = worker_targets or []
        self.error_sessions = error_sessions or set()
        self.commands: list[tuple[str, dict, str | None]] = []
        self._ws = None  # live connection, for test-injected events

    # --- introspection helpers used by assertions ---
    @property
    def methods(self) -> list[str]:
        return [m for (m, _, _) in self.commands]

    def params_for(self, method: str) -> list[dict]:
        return [p for (m, p, _) in self.commands if m == method]

    def methods_for(self, sid: str | None) -> list[str]:
        return [m for (m, _, s) in self.commands if s == sid]

    def params_for_session(self, method: str, sid: str | None) -> list[dict]:
        return [p for (m, p, s) in self.commands if m == method and s == sid]

    # --- test-injected events (frame detach / navigation) ---
    async def emit_detached(self, sid: str) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "method": "Target.detachedFromTarget",
                    "sessionId": "S1",
                    "params": {"sessionId": sid},
                }
            )
        )

    async def emit_target_info_changed(self, target_id: str, url: str) -> None:
        await self._ws.send(
            json.dumps(
                {
                    "method": "Target.targetInfoChanged",
                    "sessionId": "S1",
                    "params": {
                        "targetInfo": {"targetId": target_id, "type": "iframe", "url": url}
                    },
                }
            )
        )

    def _scripted_frame(self, sid: str | None) -> dict | None:
        for fr in self.iframes:
            if fr["sessionId"] == sid:
                return fr
        return None

    async def handler(self, ws) -> None:
        self._ws = ws
        async for raw in ws:
            msg = json.loads(raw)
            self.commands.append(
                (msg["method"], msg.get("params") or {}, msg.get("sessionId"))
            )
            await self._dispatch(ws, msg)

    async def _dispatch(self, ws, msg: dict) -> None:
        mid, method = msg["id"], msg["method"]
        params = msg.get("params") or {}
        sid = msg.get("sessionId")

        async def error(message: str) -> None:
            await ws.send(json.dumps({"id": mid, "error": {"message": message}}))

        if sid in self.error_sessions:
            await error("Internal error")
            return
        known = (
            {None, "S1"}
            | {fr["sessionId"] for fr in self.iframes}
            | {w["sessionId"] for w in self.worker_targets}
        )
        if sid not in known:
            await error("Session with given id not found")
            return
        frame = self._scripted_frame(sid)

        # Stale-node error mode: any DOM.* op on the doomed backend id errors.
        if (
            self.error_backend is not None
            and method.startswith("DOM.")
            and params.get("backendNodeId") == self.error_backend
        ):
            await error("Could not find node with given id")
            return

        if method == "Target.createTarget":
            result: dict = {"targetId": "T1"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Page.getFrameTree":
            result = {"frameTree": {"frame": {"id": self.frame_id}}}
        elif method == "Page.navigate":
            await ws.send(json.dumps({"id": mid, "result": {"frameId": self.frame_id}}))
            await self._emit_after_navigate(ws)
            return
        elif method == "DOM.getDocument":
            result = {"root": {"nodeId": 1}}
        elif method == "DOM.getOuterHTML":
            result = {"outerHTML": frame["html"] if frame else self.html}
        elif method == "Accessibility.getFullAXTree":
            result = {"nodes": frame["ax_nodes"] if frame else self.ax_nodes}
        elif method == "Accessibility.getPartialAXTree":
            result = {
                "nodes": [
                    {
                        "value": {"value": self.ax_value_after_type},
                        "properties": [
                            {
                                "name": "selected",
                                "value": {"value": self.ax_selected_after_select},
                            }
                        ],
                    }
                ]
            }
        elif (
            method == "Input.dispatchKeyEvent"
            and self.navigate_on_key is not None
            and params.get("key") == "ArrowDown"
            and params.get("type") == "keyUp"
        ):
            await ws.send(json.dumps({"id": mid, "result": {}}))
            frame, self.navigate_on_key = self.navigate_on_key, None
            await ws.send(
                json.dumps(
                    {
                        "method": "Page.frameNavigated",
                        "sessionId": "S1",
                        "params": {"frame": frame},
                    }
                )
            )
            return
        elif method == "DOM.getFrameOwner":
            fr = next(
                (f for f in self.iframes if f["targetId"] == params.get("frameId")),
                None,
            )
            if fr is None:
                await error("Frame with the given id was not found.")
                return
            result = {"backendNodeId": fr.get("owner_backend", 900)}
        elif method == "DOM.getContentQuads":
            owner_of = next(
                (
                    f
                    for f in self.iframes
                    if f.get("owner_backend", 900) == params.get("backendNodeId")
                ),
                None,
            )
            if owner_of is not None:
                # The <iframe> element's own quad in its parent document.
                result = {"quads": owner_of.get("owner_quad", [OWNER_QUAD])}
            else:
                result = {
                    "quads": frame.get("quads", self.quads) if frame else self.quads
                }
        elif method == "Input.dispatchMouseEvent" and params.get("type") == "mouseReleased":
            await ws.send(json.dumps({"id": mid, "result": {}}))
            if self.ax_nodes_after_click is not None:
                self.ax_nodes = self.ax_nodes_after_click
            if self.navigate_on_click is not None:
                await ws.send(
                    json.dumps(
                        {
                            "method": "Page.frameNavigated",
                            "sessionId": "S1",
                            "params": {"frame": self.navigate_on_click},
                        }
                    )
                )
            return
        else:
            result = {}
        await ws.send(json.dumps({"id": mid, "result": result}))

    async def _emit_after_navigate(self, ws) -> None:
        for frame in self.frame_navigated:
            await ws.send(
                json.dumps(
                    {
                        "method": "Page.frameNavigated",
                        "sessionId": "S1",
                        "params": {"frame": frame},
                    }
                )
            )
        # OOPIF/worker attach announcements ride the parent's session, before
        # the lifecycle event so the settle round-trips order them first.
        for fr in self.iframes:
            await ws.send(
                json.dumps(
                    {
                        "method": "Target.attachedToTarget",
                        "sessionId": fr.get("parent", "S1"),
                        "params": {
                            "sessionId": fr["sessionId"],
                            "targetInfo": {
                                "targetId": fr["targetId"],
                                "type": "iframe",
                                "url": fr["url"],
                            },
                            "waitingForDebugger": False,
                        },
                    }
                )
            )
        for w in self.worker_targets:
            await ws.send(
                json.dumps(
                    {
                        "method": "Target.attachedToTarget",
                        "sessionId": "S1",
                        "params": {
                            "sessionId": w["sessionId"],
                            "targetInfo": {
                                "targetId": w["targetId"],
                                "type": "worker",
                                "url": w.get("url", ""),
                            },
                            "waitingForDebugger": False,
                        },
                    }
                )
            )
        await ws.send(
            json.dumps(
                {
                    "method": "Page.lifecycleEvent",
                    "sessionId": "S1",
                    "params": {"name": "networkAlmostIdle"},
                }
            )
        )


async def _serve(fake: FakeChrome):
    server = await websockets.serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/devtools/browser/fake"


async def _connect_session(
    fake: FakeChrome, resolver=_public_resolver
) -> tuple[BrowserSession, object]:
    server, ws_url = await _serve(fake)

    async def getter(cdp_port: int) -> str:
        return ws_url

    session = BrowserSession(resolver=resolver, ws_url_getter=getter)
    await session.connect()
    return session, server


async def _close(session, server) -> None:
    await session.close()
    server.close()
    await server.wait_closed()


# --- 1. connect + navigate: settle waited, handles invalidated, snapshot -------


async def test_connect_navigate_returns_snapshot_and_invalidates_handles():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        out = await session.navigate("https://example.com/book")
        assert '[e1] textbox "Name"' in out
        assert '[e2] button "Reserve now"' in out
        first_handles = dict(session._handles)
        assert first_handles == {"e1": ("S1", 111), "e2": ("S1", 222)}

        # A second navigate must invalidate then rebuild the handle map — with a
        # different tree the old handles are gone.
        fake.ax_nodes = [
            {"role": {"value": "button"}, "name": {"value": "Only"}, "backendDOMNodeId": 999}
        ]
        out2 = await session.navigate("https://example.com/next")
        assert session._handles == {"e1": ("S1", 999)}
        assert "Reserve now" not in out2
        assert "Page.navigate" in fake.methods
    finally:
        await _close(session, server)


# --- 2. snapshot: handle map -> backendDOMNodeIds -------------------------------


async def test_snapshot_maps_handles_to_backend_ids():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        out = await session.snapshot()
        assert session._handles == {"e1": ("S1", 111), "e2": ("S1", 222)}
        assert 'heading "Book a table"' in out
    finally:
        await _close(session, server)


# --- 3. click: moved->pressed->released at quad center, scrollIntoView first ----


async def test_click_dispatches_mouse_sequence_at_quad_center():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.click("e2")
        # FakeChrome serves the same AX tree back → the unchanged note.
        assert res == "clicked [e2] (page unchanged — the click may have had no effect)"

        # scrollIntoViewIfNeeded happens before the quads read.
        assert fake.methods.index("DOM.scrollIntoViewIfNeeded") < fake.methods.index(
            "DOM.getContentQuads"
        )
        mouse = fake.params_for("Input.dispatchMouseEvent")
        assert [m["type"] for m in mouse] == ["mouseMoved", "mousePressed", "mouseReleased"]
        # center of DEFAULT_QUAD = (60, 40)
        for m in mouse:
            assert m["x"] == 60
            assert m["y"] == 40
        assert mouse[1]["button"] == "left" and mouse[1]["clickCount"] == 1
        assert mouse[2]["button"] == "left" and mouse[2]["clickCount"] == 1
        assert "button" not in mouse[0]  # mouseMoved has no button
    finally:
        await _close(session, server)


async def test_click_that_changes_the_page_notes_it():
    changed_nodes = [
        {"role": {"value": "button"}, "name": {"value": "Confirm"}, "backendDOMNodeId": 333}
    ]
    fake = FakeChrome(ax_nodes_after_click=changed_nodes)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.click("e2")
        assert res == "clicked [e2] (page changed — take a new browser_snapshot)"
        # The model's handle numbering stays pinned to its LAST snapshot — the
        # comparison read must not remap handles.
        assert session._handles == {"e1": ("S1", 111), "e2": ("S1", 222)}
    finally:
        await _close(session, server)


async def test_click_change_note_baseline_advances():
    """The comparison baseline moves to the post-action tree: a click that
    changes the page once, then a click on the (now stable) page, reports
    changed then unchanged."""
    changed_nodes = [
        {"role": {"value": "button"}, "name": {"value": "Confirm"}, "backendDOMNodeId": 222}
    ]
    fake = FakeChrome(ax_nodes_after_click=changed_nodes)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        first = await session.click("e2")
        assert "(page changed" in first
        second = await session.click("e2")
        assert "(page unchanged" in second
    finally:
        await _close(session, server)


async def test_click_navigation_reports_url_and_invalidates_handles():
    fake = FakeChrome(
        navigate_on_click={"id": "F1", "url": "https://example.com/after"}
    )
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        assert session._handles  # populated by the navigate snapshot
        res = await session.click("e2")
        assert res == (
            "clicked [e2] → page navigated to https://example.com/after; "
            "take a new browser_snapshot"
        )
        assert session._handles == {}
    finally:
        await _close(session, server)


async def test_type_empty_field_after_typing_notes_it():
    fake = FakeChrome(ax_value_after_type="")
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.type("e1", "hello")
        assert res == (
            "typed into [e1] (the field appears empty after typing — "
            "the input may not have registered)"
        )
        # The verification read targeted the typed node, relatives excluded.
        partial = fake.params_for("Accessibility.getPartialAXTree")
        assert partial == [{"backendNodeId": 111, "fetchRelatives": False}]
    finally:
        await _close(session, server)


async def test_type_stale_node_discards_nav_waiter():
    fake = FakeChrome(error_backend=111)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.type("e1", "x")
        assert "stale handle" in res
        # The pre-registered navigation waiter must not leak.
        assert session._cdp._waiters == []
    finally:
        await _close(session, server)


# --- 4. click empty quads -> not visible ---------------------------------------


async def test_click_empty_quads_reports_not_visible():
    fake = FakeChrome(quads=[])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.click("e2")
        assert res == "element not visible: [e2]"
        assert not fake.params_for("Input.dispatchMouseEvent")
    finally:
        await _close(session, server)


# --- 5. click stale node (CDP error) -> stale-handle message --------------------


async def test_click_stale_node_reports_stale_handle():
    fake = FakeChrome(error_backend=222)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.click("e2")
        assert "stale handle" in res
        assert "browser_snapshot" in res
        assert "[e2]" in res
    finally:
        await _close(session, server)


async def test_click_unknown_handle_reports_stale_handle():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.click("e99")
        assert "stale handle" in res and "browser_snapshot" in res
    finally:
        await _close(session, server)


# --- 6. type: focus + insertText, clear sequence, enter sequence ---------------


async def test_type_focus_insert_clear_and_enter():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.type("e1", "Ada Lovelace", press_enter=True, clear=True)
        assert res == "typed into [e1]"

        assert fake.methods.index("DOM.focus") < fake.methods.index("Input.insertText")
        insert = fake.params_for("Input.insertText")
        assert insert == [{"text": "Ada Lovelace"}]

        keys = fake.params_for("Input.dispatchKeyEvent")
        # clear = Ctrl+A (down/up, modifiers=2) then Delete (down/up) BEFORE insert.
        assert keys[0] == {
            "type": "rawKeyDown",
            "modifiers": 2,
            "key": "a",
            "code": "KeyA",
            "windowsVirtualKeyCode": 65,
        }
        assert keys[1]["type"] == "keyUp" and keys[1]["key"] == "a"
        assert keys[2]["key"] == "Delete" and keys[2]["windowsVirtualKeyCode"] == 46
        # Enter sequence AFTER insertText: vk=13, char carries "\r".
        enter = [k for k in keys if k.get("windowsVirtualKeyCode") == 13]
        assert [k["type"] for k in enter] == ["rawKeyDown", "char", "keyUp"]
        assert enter[1]["text"] == "\r"
    finally:
        await _close(session, server)


async def test_type_without_clear_skips_ctrl_a():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        await session.type("e1", "x", clear=False)
        keys = fake.params_for("Input.dispatchKeyEvent")
        assert keys == []  # no clear, no enter
        assert fake.params_for("Input.insertText") == [{"text": "x"}]
    finally:
        await _close(session, server)


# --- 7. Chrome down -> actionable message naming the services -------------------


async def test_chrome_unavailable_surfaces_services_message():
    async def getter(cdp_port: int) -> str:
        raise ChromeUnavailable("connection refused")

    mgr = BrowserSessionManager(resolver=_public_resolver, ws_url_getter=getter)
    with pytest.raises(ChromeUnavailable) as exc:
        await mgr.get_or_create()
    assert "docs/browser-fallback-setup.md" in str(exc.value)
    assert "chrome-agent" in str(exc.value)
    assert "egress-proxy" in str(exc.value)


# --- 8. tab NOT closed during normal ops ---------------------------------------


async def test_tab_not_closed_during_normal_ops():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        await session.snapshot()
        await session.click("e2")
        await session.type("e1", "hi")
        await session.scroll("down")
        await session.read()
        assert "Target.closeTarget" not in fake.methods
    finally:
        await _close(session, server)


# --- 9. current_url tracks main-frame frameNavigated, ignores subframes ---------


async def test_current_url_updates_on_main_frame_navigation():
    fake = FakeChrome(
        frame_navigated=[{"id": "F1", "url": "https://example.com/redirected"}]
    )
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/start")
        assert session.current_url == "https://example.com/redirected"
        assert session.current_host == "example.com"
    finally:
        await _close(session, server)


async def test_current_url_ignores_subframe_navigation():
    fake = FakeChrome(
        frame_navigated=[
            {"id": "F2", "parentId": "F1", "url": "https://evil.example/iframe"}
        ]
    )
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/start")
        # optimistic value stands; the subframe never overrides it
        assert session.current_url == "https://example.com/start"
    finally:
        await _close(session, server)


# --- 10. NEVER any Runtime.* command -------------------------------------------


async def test_no_runtime_command_ever():
    # iframes included: the pin covers EVERY session, auto-attached OOPIF
    # child sessions too (fake.methods records commands across all sessions).
    fake = FakeChrome(iframes=[{"sessionId": "S2", "targetId": "FR2",
                                "url": "https://widget.example/w",
                                "ax_nodes": [], "html": "<html></html>"}])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        await session.snapshot()
        await session.click("e2")
        await session.type("e1", "hi", press_enter=True)
        await session.read()
        await session.scroll("up")
        assert not any(m.startswith("Runtime.") for m in fake.methods), (
            "no-Runtime covers EVERY session incl. OOPIF children (Cloudflare signal), which is also why "
            "setAutoAttach must send waitForDebuggerOnStart:false"
        )
    finally:
        await _close(session, server)


# --- 11. private/tailnet navigate refused BEFORE any ws connection --------------


async def test_navigate_private_url_refused_before_ws_connect():
    getter_called: list[bool] = []

    async def getter(cdp_port: int) -> str:
        getter_called.append(True)
        raise AssertionError("ws must not be touched on a refused target")

    async def private_resolver(host: str) -> list[str]:
        return ["100.64.0.9"]  # tailnet

    # navigate performs check_target FIRST — never reaching connect().
    session = BrowserSession(resolver=private_resolver, ws_url_getter=getter)
    out = await session.navigate("https://intranet.example/")
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out
    assert not getter_called


# --- 12. read(): render_page output carries the untrusted-data prefix ----------


async def test_read_returns_render_page_output_with_untrusted_prefix():
    fake = FakeChrome(html=PAGE_HTML)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        out = await session.read()
        assert "untrusted external data" in out
        assert "hello lane b" in out
    finally:
        await _close(session, server)


# =============================================================================
# Phase B: OOPIF (cross-origin iframe) visibility — auto-attach, merged
# snapshot/read sections, disclosure on failure, interaction guard.
# =============================================================================

IFRAME_AX = [
    {
        "role": {"value": "textbox"},
        "name": {"value": "Card number"},
        "ignored": False,
        "backendDOMNodeId": 555,
    },
    {
        "role": {"value": "button"},
        "name": {"value": "Pay"},
        "ignored": False,
        "backendDOMNodeId": 556,
    },
]


def _checkout_iframe(**over) -> dict:
    fr = {
        "sessionId": "S2",
        "targetId": "FR2",
        "url": "https://checkout.example/pay",
        "ax_nodes": IFRAME_AX,
        "html": "<html><body><p>"
        + "Card checkout form, accepts Visa and Mastercard. " * 10
        + "</p></body></html>",
    }
    fr.update(over)
    return fr


async def test_auto_attach_requested_with_safe_params():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        # waitForDebuggerOnStart MUST be False: resuming a paused target would
        # need Runtime.runIfWaitingForDebugger (forbidden domain).
        assert fake.params_for_session("Target.setAutoAttach", "S1") == [
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
        ]
    finally:
        await _close(session, server)


async def test_snapshot_merges_iframe_section_with_continued_handles():
    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        out = await session.navigate("https://example.com/")
        assert "--- iframe: https://checkout.example/pay ---" in out
        # Handle numbering continues across the section boundary...
        assert '[e3] textbox "Card number"' in out
        assert '[e4] button "Pay"' in out
        # ...and frame handles map to the OWNING session.
        assert session._handles["e2"] == ("S1", 222)
        assert session._handles["e3"] == ("S2", 555)
        assert session._handles["e4"] == ("S2", 556)
        # The child session got DOM + nested auto-attach — and nothing else.
        assert "DOM.enable" in fake.methods_for("S2")
        assert "Target.setAutoAttach" in fake.methods_for("S2")
        assert "Network.enable" not in fake.methods_for("S2")
    finally:
        await _close(session, server)


async def test_nested_oopif_recorded_with_parent_session():
    nested = _checkout_iframe(
        sessionId="S3",
        targetId="FR3",
        url="https://inner.example/widget",
        parent="S2",
    )
    fake = FakeChrome(iframes=[_checkout_iframe(), nested])
    session, server = await _connect_session(fake)
    try:
        out = await session.navigate("https://example.com/")
        assert session._frames["S3"].parent_session_id == "S2"
        assert "--- iframe: https://inner.example/widget ---" in out
    finally:
        await _close(session, server)


async def test_frame_capture_failure_discloses_but_main_tree_renders():
    fake = FakeChrome(iframes=[_checkout_iframe()], error_sessions={"S2"})
    session, server = await _connect_session(fake)
    try:
        out = await session.navigate("https://example.com/")
        assert '[e2] button "Reserve now"' in out  # main tree intact
        assert (
            "--- iframe: https://checkout.example/pay ---\n"
            "[content could not be captured]" in out
        )
        assert session._frames["S2"].failed is not None
    finally:
        await _close(session, server)


async def test_empty_iframe_skipped_entirely():
    fake = FakeChrome(
        iframes=[_checkout_iframe(ax_nodes=[], html="<html></html>")]
    )
    session, server = await _connect_session(fake)
    try:
        out = await session.navigate("https://example.com/")
        assert "--- iframe" not in out
        read = await session.read()
        assert "--- iframe" not in read
    finally:
        await _close(session, server)


async def test_detached_frame_disappears_and_its_handles_go_stale():
    import asyncio

    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        assert "S2" in session._frames
        await fake.emit_detached("S2")
        await asyncio.sleep(0.05)
        assert "S2" not in session._frames
        out = await session.snapshot()
        assert "--- iframe" not in out
        assert "e3" not in session._handles
        assert "stale handle" in await session.click("e3")
    finally:
        await _close(session, server)


async def test_target_info_changed_updates_frame_url():
    import asyncio

    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        await fake.emit_target_info_changed("FR2", "https://checkout.example/step2")
        await asyncio.sleep(0.05)
        assert session._frames["S2"].url == "https://checkout.example/step2"
        out = await session.snapshot()
        assert "--- iframe: https://checkout.example/step2 ---" in out
    finally:
        await _close(session, server)


async def test_cap_pressure_discloses_unshown_iframes():
    big_main = [
        {
            "role": {"value": "button"},
            "name": {"value": "X" * 200},
            "ignored": False,
            "backendDOMNodeId": i,
        }
        for i in range(200)
    ]
    fake = FakeChrome(ax_nodes=big_main, iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        out = await session.navigate("https://example.com/")
        assert "[tree truncated — 1 iframe(s) not shown]" in out
        assert "Card number" not in out
    finally:
        await _close(session, server)


async def test_read_merges_frame_text_with_single_untrusted_note():
    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        out = await session.read()
        assert "hello lane b" in out
        assert "--- iframe: https://checkout.example/pay ---" in out
        assert "Card checkout form" in out
        # One untrusted-data banner for the whole read, not one per section.
        assert out.count("untrusted external data") == 1
    finally:
        await _close(session, server)


async def test_read_frame_failure_disclosed():
    fake = FakeChrome(iframes=[_checkout_iframe()], error_sessions={"S2"})
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        out = await session.read()
        assert "--- iframe: https://checkout.example/pay ---\n[could not be read]" in out
    finally:
        await _close(session, server)


async def test_worker_targets_never_touched():
    fake = FakeChrome(
        iframes=[_checkout_iframe()],
        worker_targets=[{"sessionId": "W1", "targetId": "WT1"}],
    )
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        await session.snapshot()
        await session.read()
        assert "W1" not in session._frames
        assert fake.methods_for("W1") == []  # no command EVER on a worker session
    finally:
        await _close(session, server)


async def test_frame_click_reads_frame_geometry_but_dispatches_on_main():
    """Phase C routing (spike 2026-07-18): geometry on the frame's session,
    offset from getFrameOwner in the parent, mouse on the MAIN session at
    frame-relative + offset viewport coordinates."""
    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.click("e4")
        assert res.startswith("clicked [e4]")
        assert fake.params_for_session("DOM.scrollIntoViewIfNeeded", "S2") == [
            {"backendNodeId": 556}
        ]
        assert fake.params_for_session("DOM.getContentQuads", "S2") == [
            {"backendNodeId": 556}
        ]
        assert fake.params_for_session("DOM.getFrameOwner", "S1") == [
            {"frameId": "FR2"}
        ]
        mouse = fake.params_for_session("Input.dispatchMouseEvent", "S1")
        assert [m["type"] for m in mouse] == [
            "mouseMoved", "mousePressed", "mouseReleased",
        ]
        for m in mouse:
            # DEFAULT_QUAD center (60, 40) + OWNER_QUAD top-left (200, 150)
            assert m["x"] == 260 and m["y"] == 190
        assert fake.params_for_session("Input.dispatchMouseEvent", "S2") == []
    finally:
        await _close(session, server)


async def test_frame_type_routes_all_input_to_frame_session():
    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.type("e3", "4111", press_enter=True)
        assert res.startswith("typed into [e3]")
        assert fake.params_for_session("DOM.focus", "S2") == [{"backendNodeId": 555}]
        assert fake.params_for_session("Input.insertText", "S2") == [{"text": "4111"}]
        assert fake.params_for_session("Input.insertText", "S1") == []
        # clear + Enter key events target the frame session too...
        assert fake.params_for_session("Input.dispatchKeyEvent", "S1") == []
        assert len(fake.params_for_session("Input.dispatchKeyEvent", "S2")) == 7
        # ...as does the type-verification AXValue read.
        assert fake.params_for_session("Accessibility.getPartialAXTree", "S2") == [
            {"backendNodeId": 555, "fetchRelatives": False}
        ]
    finally:
        await _close(session, server)


async def test_interaction_after_frame_detach_is_stale_without_dispatch():
    """Handle still in the map (no re-snapshot) but the frame detached — the
    live resolution refuses before any CDP call."""
    import asyncio

    fake = FakeChrome(iframes=[_checkout_iframe()])
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        await fake.emit_detached("S2")
        await asyncio.sleep(0.05)
        assert "stale handle" in await session.click("e4")
        assert fake.params_for("Input.dispatchMouseEvent") == []
        assert "stale handle" in await session.type("e3", "x")
        assert fake.params_for("Input.insertText") == []
    finally:
        await _close(session, server)


# =============================================================================
# browser_select: keyboard-driven native <select> (Home + N×ArrowDown on the
# owning session; verified via the option's AX selected prop).
# =============================================================================

# Native select AX shape (verified live 2026-07-19): combobox → MenuListPopup
# → option*. Snapshot handles: e1=combobox(300), e2=opt(311, selected),
# e3=opt(312), e4=opt(313, disabled), e5=opt(314).
SELECT_AX = [
    {"nodeId": "1", "role": {"value": "RootWebArea"}, "childIds": ["2"], "ignored": False},
    {
        "nodeId": "2",
        "role": {"value": "combobox"},
        "name": {"value": "Quantity"},
        "backendDOMNodeId": 300,
        "parentId": "1",
        "childIds": ["3"],
        "ignored": False,
    },
    {
        "nodeId": "3",
        "role": {"value": "MenuListPopup"},
        "backendDOMNodeId": 301,
        "parentId": "2",
        "childIds": ["4", "5", "6", "7"],
        "ignored": False,
    },
    {
        "nodeId": "4",
        "role": {"value": "option"},
        "name": {"value": "1"},
        "backendDOMNodeId": 311,
        "parentId": "3",
        "properties": [{"name": "selected", "value": {"value": True}}],
        "ignored": False,
    },
    {
        "nodeId": "5",
        "role": {"value": "option"},
        "name": {"value": "2"},
        "backendDOMNodeId": 312,
        "parentId": "3",
        "ignored": False,
    },
    {
        "nodeId": "6",
        "role": {"value": "option"},
        "name": {"value": "sold out"},
        "backendDOMNodeId": 313,
        "parentId": "3",
        "properties": [{"name": "disabled", "value": {"value": True}}],
        "ignored": False,
    },
    {
        "nodeId": "7",
        "role": {"value": "option"},
        "name": {"value": "4"},
        "backendDOMNodeId": 314,
        "parentId": "3",
        "ignored": False,
    },
]


async def test_select_focuses_select_then_home_and_arrows_skip_disabled():
    fake = FakeChrome(ax_nodes=SELECT_AX)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.select_option("e5")  # option "4", enabled index 2
        assert res == "selected [e5]"

        # Focus targets the SELECT (300), not the option.
        assert fake.params_for("DOM.focus") == [{"backendNodeId": 300}]
        keys = fake.params_for("Input.dispatchKeyEvent")
        # Home first (vk 36), then 2×ArrowDown (vk 40) — the disabled "sold
        # out" option is skipped by native arrow navigation.
        assert [(k["key"], k["type"]) for k in keys] == [
            ("Home", "rawKeyDown"), ("Home", "keyUp"),
            ("ArrowDown", "rawKeyDown"), ("ArrowDown", "keyUp"),
            ("ArrowDown", "rawKeyDown"), ("ArrowDown", "keyUp"),
        ], "a native <select> can't be driven by quad clicks or insertText — focus + Home + N×ArrowDown only"
        assert keys[0]["windowsVirtualKeyCode"] == 36
        assert keys[2]["windowsVirtualKeyCode"] == 40
        assert fake.methods.index("DOM.focus") < fake.methods.index(
            "Input.dispatchKeyEvent"
        )
        # Verification read targeted the OPTION node.
        assert {"backendNodeId": 314, "fetchRelatives": False} in fake.params_for(
            "Accessibility.getPartialAXTree"
        )
    finally:
        await _close(session, server)


async def test_select_disabled_option_refused_without_keys():
    fake = FakeChrome(ax_nodes=SELECT_AX)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.select_option("e4")  # the disabled "sold out"
        assert res == "option [e4] is disabled"
        assert fake.params_for("Input.dispatchKeyEvent") == []
        assert fake.params_for("DOM.focus") == []
    finally:
        await _close(session, server)


async def test_select_on_non_option_handle_refused():
    fake = FakeChrome(ax_nodes=SELECT_AX)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.select_option("e1")  # the combobox itself
        assert "not a dropdown option" in res
        assert fake.params_for("Input.dispatchKeyEvent") == []
    finally:
        await _close(session, server)


async def test_select_jump_menu_navigation_reported():
    fake = FakeChrome(
        ax_nodes=SELECT_AX,
        navigate_on_key={"id": "F1", "url": "https://example.com/lang/de"},
    )
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.select_option("e3")
        assert res == (
            "selected [e3] → page navigated to https://example.com/lang/de; "
            "take a new browser_snapshot"
        )
        assert session._handles == {}
    finally:
        await _close(session, server)


async def test_select_unverified_selection_notes_it():
    fake = FakeChrome(ax_nodes=SELECT_AX, ax_selected_after_select=False)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        res = await session.select_option("e3")
        assert res == (
            "pressed the keys but [e3] does not appear selected — this may "
            "not be a native dropdown; try browser_click"
        )
    finally:
        await _close(session, server)


async def test_select_in_oopif_routes_all_commands_to_frame_session():
    fake = FakeChrome(
        iframes=[_checkout_iframe(ax_nodes=SELECT_AX, html="<html>select</html>")]
    )
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        # Frame handles continue after the 2 main handles: e3=combobox(300),
        # e4..e7 = options; e7 = option "4" (backend 314).
        assert session._handles["e7"] == ("S2", 314)
        res = await session.select_option("e7")
        assert res.startswith("selected [e7]")
        assert fake.params_for_session("DOM.focus", "S2") == [{"backendNodeId": 300}]
        assert len(fake.params_for_session("Input.dispatchKeyEvent", "S2")) == 6
        assert fake.params_for_session("Input.dispatchKeyEvent", "S1") == []
        assert fake.params_for_session(
            "Accessibility.getPartialAXTree", "S2"
        ) == [{"backendNodeId": 314, "fetchRelatives": False}]
    finally:
        await _close(session, server)


async def test_select_stale_handle():
    fake = FakeChrome(ax_nodes=SELECT_AX)
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/")
        assert "stale handle" in await session.select_option("e99")
    finally:
        await _close(session, server)


# =============================================================================
# Tool-layer tests: the seven Tool wrappers over a shared BrowserSessionManager.
# =============================================================================


async def _tool_manager(
    fake: FakeChrome, resolver=_public_resolver
) -> tuple[BrowserSessionManager, object]:
    server, ws_url = await _serve(fake)

    async def getter(cdp_port: int) -> str:
        return ws_url

    return BrowserSessionManager(resolver=resolver, ws_url_getter=getter), server


async def _close_manager(manager: BrowserSessionManager, server) -> None:
    await manager.close()
    server.close()
    await server.wait_closed()


# --- 13. classification + remote pins for all seven ------------------------------


def test_tool_classification_and_remote_pins():
    tools = {t.name: t for t in browser_interaction_tools(BrowserSessionManager())}
    assert set(tools) == set(BROWSER_INTERACTION_TOOL_NAMES)
    assert tools["browser_navigate"].classification is ActionClass.OUTWARD
    assert tools["browser_snapshot"].classification is ActionClass.READ_ONLY
    assert tools["browser_read"].classification is ActionClass.READ_ONLY
    assert tools["browser_click"].classification is ActionClass.OUTWARD
    assert tools["browser_type"].classification is ActionClass.OUTWARD
    assert tools["browser_select"].classification is ActionClass.OUTWARD
    assert tools["browser_scroll"].classification is ActionClass.MUTATING_LOCAL
    # remote=True on all seven — the EGRESS_TOOLS structural pin requires it.
    assert all(t.remote is True for t in tools.values())


# --- 14. navigate private URL refused before ws connect, no session created ----


async def test_tool_navigate_private_url_refused_before_connect():
    getter_called: list[bool] = []

    async def getter(cdp_port: int) -> str:
        getter_called.append(True)
        raise AssertionError("ws must not be touched on a refused target")

    async def private_resolver(host: str) -> list[str]:
        return ["100.64.0.9"]  # tailnet

    manager = BrowserSessionManager(resolver=private_resolver, ws_url_getter=getter)
    out = await BrowserNavigateTool(manager).run({"url": "https://intranet.example/"})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out
    assert not getter_called  # ws_url_getter never consulted
    assert manager.get() is None  # no session created


# --- 15. non-navigate tools with no session -> actionable message --------------


async def test_non_navigate_tools_without_session_are_actionable():
    manager = BrowserSessionManager()
    tools = [
        (BrowserSnapshotTool(manager), {}),
        (BrowserReadTool(manager), {}),
        (BrowserClickTool(manager), {"handle": "e1"}),
        (BrowserTypeTool(manager), {"handle": "e1", "text": "x"}),
        (BrowserSelectTool(manager), {"handle": "e1"}),
        (BrowserScrollTool(manager), {"direction": "down"}),
    ]
    for tool, args in tools:
        out = await tool.run(args)
        assert "browser_navigate" in out, tool.name
    assert manager.get() is None  # none of them auto-connected


# --- 16. Chrome down via navigate -> services message --------------------------


async def test_tool_navigate_chrome_down_returns_services_message():
    async def getter(cdp_port: int) -> str:
        raise ChromeUnavailable("connection refused")

    manager = BrowserSessionManager(resolver=_public_resolver, ws_url_getter=getter)
    out = await BrowserNavigateTool(manager).run({"url": "https://example.com/"})
    assert "docs/browser-fallback-setup.md" in out
    assert "chrome-agent" in out
    assert "egress-proxy" in out


# --- 17. happy path: navigate -> snapshot -> click through the Tool layer ------


async def test_navigate_snapshot_click_through_tool_layer():
    fake = FakeChrome()
    manager, server = await _tool_manager(fake)
    try:
        out = await BrowserNavigateTool(manager).run({"url": "https://example.com/book"})
        assert '[e1] textbox "Name"' in out
        assert '[e2] button "Reserve now"' in out

        snap = await BrowserSnapshotTool(manager).run({})
        assert '[e2] button "Reserve now"' in snap

        clicked = await BrowserClickTool(manager).run({"handle": "e2"})
        assert clicked == "clicked [e2] (page unchanged — the click may have had no effect)"

        typed = await BrowserTypeTool(manager).run({"handle": "e1", "text": "Tom"})
        assert typed == "typed into [e1]"

        # All calls reused the ONE session the manager holds.
        assert manager.get() is not None
    finally:
        await _close_manager(manager, server)


# =============================================================================
# Identity-ref leak pins (chunk 5): the resolved value reaches ONLY the CDP
# insertText — never the result string, the args dict, or (resolver=None) at all.
# Obviously-fake value so a leak can't masquerade as a real address.
# =============================================================================

REF_EMAIL = "test@example.invalid"


def _ref_envelope(ref_hosts=("example.com",)) -> DelegationEnvelope:
    env = DelegationEnvelope(origin="example.com", task="fill", refs=frozenset({"email"}))
    env.ref_resolved_hosts = set(ref_hosts)
    return env


async def test_ref_value_reaches_insertText_but_not_result_or_args(db):
    """(1) Input.insertText carries the real value; (2) the tool RESULT string
    does not; (3) the args dict is UNCHANGED (still the placeholder)."""
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, REF_EMAIL)
    fake = FakeChrome()
    manager, server = await _tool_manager(fake)
    try:
        await BrowserNavigateTool(manager).run({"url": "https://example.com/"})
        resolver = RefResolver(cc, _ref_envelope(), manager)
        tool = BrowserTypeTool(manager, resolver)
        args = {"handle": "e1", "text": "{email}"}
        res = await tool.run(args)

        # (1) the value is what actually got typed into the field
        assert fake.params_for("Input.insertText") == [{"text": REF_EMAIL}]
        # (2) the result string never echoes the resolved value
        assert res == "typed into [e1]"
        assert REF_EMAIL not in res
        # (3) the dispatched args are untouched — the placeholder survives, so
        # nothing downstream (trace/audit/history) ever sees the value
        assert args["text"] == "{email}", (
            "the real ref value exists ONLY inside BrowserTypeTool.run → insertText; policy alone grants "
            "ref_resolved_hosts, and browser_delegate rejects tasks containing it"
        )
    finally:
        await _close_manager(manager, server)


async def test_ref_without_resolver_errors_and_types_nothing(db):
    """(4) A ref placeholder with no resolver (M1/main-catalog) → an error that
    names the delegated-run requirement; nothing is typed."""
    fake = FakeChrome()
    manager, server = await _tool_manager(fake)
    try:
        await BrowserNavigateTool(manager).run({"url": "https://example.com/"})
        res = await BrowserTypeTool(manager).run({"handle": "e1", "text": "{email}"})
        assert "delegated browser run" in res
        assert not fake.params_for("Input.insertText")
    finally:
        await _close_manager(manager, server)


async def test_ref_unapproved_host_types_nothing(db):
    """A resolver error (site not approved for ref use) types NOTHING — no
    Input.insertText is recorded at all."""
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, REF_EMAIL)
    fake = FakeChrome()
    manager, server = await _tool_manager(fake)
    try:
        await BrowserNavigateTool(manager).run({"url": "https://example.com/"})
        # Empty ref_resolved_hosts: policy never approved ref use on this site.
        resolver = RefResolver(cc, _ref_envelope(ref_hosts=()), manager)
        res = await BrowserTypeTool(manager, resolver).run(
            {"handle": "e1", "text": "{email}"}
        )
        assert "browser_type error" in res
        assert REF_EMAIL not in res
        assert not fake.params_for("Input.insertText")
    finally:
        await _close_manager(manager, server)


async def test_ref_into_frame_requires_the_frame_host(db):
    """The Eventbrite pin: a ref typed into an OOPIF field resolves ONLY when
    the FRAME's host is in ref_resolved_hosts — approval of the embedding
    page's host must not cover the widget's origin."""
    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, REF_EMAIL)
    fake = FakeChrome(iframes=[_checkout_iframe()])
    manager, server = await _tool_manager(fake)
    try:
        await BrowserNavigateTool(manager).run({"url": "https://example.com/"})
        # Main host approved, frame host NOT → refused, nothing typed anywhere.
        resolver = RefResolver(cc, _ref_envelope(ref_hosts=("example.com",)), manager)
        res = await BrowserTypeTool(manager, resolver).run(
            {"handle": "e3", "text": "{email}"}
        )
        assert "browser_type error" in res
        assert not fake.params_for("Input.insertText")

        # Frame host approved → the real value reaches the FRAME's session.
        resolver2 = RefResolver(
            cc, _ref_envelope(ref_hosts=("checkout.example",)), manager
        )
        res2 = await BrowserTypeTool(manager, resolver2).run(
            {"handle": "e3", "text": "{email}"}
        )
        assert res2.startswith("typed into [e3]")
        assert REF_EMAIL not in res2
        assert fake.params_for_session("Input.insertText", "S2") == [
            {"text": REF_EMAIL}
        ]
        assert fake.params_for_session("Input.insertText", "S1") == []
    finally:
        await _close_manager(manager, server)


async def test_ref_denied_when_frame_navigates_before_dispatch(db):
    """TOCTOU close: the frame moves to a new host after policy approval but
    before dispatch — the resolver's live element-host re-check refuses."""
    import asyncio

    cc = ClientConfigStore(db)
    await cc.set(REF_EMAIL_KEY, REF_EMAIL)
    fake = FakeChrome(iframes=[_checkout_iframe()])
    manager, server = await _tool_manager(fake)
    try:
        await BrowserNavigateTool(manager).run({"url": "https://example.com/"})
        resolver = RefResolver(
            cc, _ref_envelope(ref_hosts=("checkout.example",)), manager
        )
        await fake.emit_target_info_changed("FR2", "https://evil.example/steal")
        await asyncio.sleep(0.05)
        res = await BrowserTypeTool(manager, resolver).run(
            {"handle": "e3", "text": "{email}"}
        )
        assert "browser_type error" in res
        assert not fake.params_for("Input.insertText")
    finally:
        await _close_manager(manager, server)


# =============================================================================
# BK-037: relay entry URL refused at the navigate pre-checks (tool layer +
# the defense-in-depth arm in BrowserSession.navigate).
# =============================================================================

_RELAY_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json").read_text()
)

# Hard copy of the board's criterion-2 directive (convention:
# tests/test_relay_block.py:48) — an import of RELAY_DIRECTIVE would make the
# equality check tautological if the constant drifts.
_RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)

# The fixture shapes browser_navigate itself dialed in Oct 2026 (audit id
# 8352's shape is the only one its tool field names — surface map §6).
# Pinned at collection: an empty selection silently SKIPS (step-3 lesson).
_RELAY_NAV_DIALS = [
    e for e in _RELAY_CORPUS["blocked"] if "browser_navigate" in e["tools"]
]
assert len(_RELAY_NAV_DIALS) == 1, (
    f"expected the 1 attested browser_navigate dial shape, got {len(_RELAY_NAV_DIALS)} "
    "— an empty selection would make the BK-037 navigate pins vacuous"
)


def _relay_tripwire_setter():
    """(getter, recording resolver, lookups): the getter fails loudly if ws is
    ever consulted; the resolver records lookups (a relay refusal must record
    none — the block precedes DNS)."""
    lookups: list[str] = []

    async def getter(cdp_port: int) -> str:
        raise AssertionError(
            "GOTCHA REGRESSION [bk-037-browser] Chrome was reached for a relay "
            "entry URL — the pre-check must refuse before any CDP connection"
        )

    async def resolver(host: str) -> list[str]:
        lookups.append(host)
        return ["93.184.216.34"]

    return getter, resolver, lookups


@pytest.mark.parametrize(
    "shape", _RELAY_NAV_DIALS, ids=[f"dial{i}" for i in range(len(_RELAY_NAV_DIALS))]
)
async def test_tool_navigate_relay_url_refused_before_connect(shape):
    # browser_navigate is entry-checked ONLY (D3) — page + redirect traffic
    # is the chrome-proxy's per-hop gate (tests/test_egress_proxy.py). The
    # pre-check at browser_interact.py:104 returns the tool's error string
    # wrapping the directive verbatim: no ws consultation, no resolver lookup,
    # no session, and no taint (RefusedConfabulated is not RefusedInternal,
    # D2 — the retry loop dies with instructions, the turn is not marked).
    getter, resolver, lookups = _relay_tripwire_setter()
    manager = BrowserSessionManager(resolver=resolver, ws_url_getter=getter)
    out = await BrowserNavigateTool(manager).run({"url": shape["url"]})
    assert out == f"browser_navigate error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)
    assert lookups == []
    assert manager.get() is None  # no session created


async def test_session_navigate_relay_url_refused_with_directive_no_ws():
    # The second arm, _browser_session.py:197 (defense in depth): the same
    # refusal at the session layer for any caller that skips the tool check.
    getter, resolver, lookups = _relay_tripwire_setter()
    session = BrowserSession(resolver=resolver, ws_url_getter=getter)
    out = await session.navigate(_RELAY_NAV_DIALS[0]["url"])
    assert out == f"browser_navigate error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)
    assert lookups == []


# --- approval preview: what a handle names (2026-11-01: a click approval said
# only "e94", which nobody can review) --------------------------------------


async def test_label_for_handle_names_the_snapshot_line_and_goes_stale():
    fake = FakeChrome()
    session, server = await _connect_session(fake)
    try:
        await session.navigate("https://example.com/book")
        assert session.label_for_handle("e2") == 'button "Reserve now"'
        assert session.label_for_handle("e9") is None  # never issued
        fake.ax_nodes = [
            {"role": {"value": "button"}, "name": {"value": "Only"}, "backendDOMNodeId": 999}
        ]
        await session.navigate("https://example.com/next")
        assert session.label_for_handle("e2") is None  # gone with the old page
        assert session.label_for_handle("e1") == 'button "Only"'
    finally:
        await _close(session, server)
