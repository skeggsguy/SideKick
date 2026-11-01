"""Manual OOPIF spike (Lane B Phase C prerequisite) — run with chrome-agent up.

Answers THE coordinate question for OOPIF interaction: are DOM.getContentQuads
results from an OOPIF child session frame-relative or viewport-relative? Also
verifies: auto-attach announces cross-origin iframes, DOM.getFrameOwner gives
the frame's offset in the parent, a main-session mouse click at translated
viewport coordinates hit-tests into the frame (browser-process routing), and
which session Input.insertText must target for an OOPIF field.

Usage: uv run python scripts/spike_oopif.py
Read-only public browsing (example.org, httpbingo.org); no login, no data
submitted. Logs every CDP method sent and asserts none touch the forbidden
Runtime domain.

FINDINGS (run 2026-07-18, Chrome via chrome-agent — recorded in
docs/plans/lane-b-interactive-browser.md, removed 2026-09-28; see git da386d5):
- attachedToTarget fires BEFORE the frame navigates; url is "" at attach and
  arrives via targetInfoChanged.
- Child-session DOM.getContentQuads are FRAME-relative → offset REQUIRED.
- DOM.getFrameOwner{frameId} on the parent + getContentQuads of the <iframe>
  element gives the exact offset (top-left corner).
- A mouse click dispatched on the MAIN session at child-coords + offset
  hit-tests into the OOPIF (frame navigated to iana.org) — input routing works.
- DOM.focus + Input.insertText on the CHILD session registers (AXValue
  confirms); no main-session fallback needed.
"""

from __future__ import annotations

import asyncio
import base64

from harness.tools._cdp import CDPClient
from harness.tools.browser_fetch import _CDP_PORT, _browser_ws_url

IFRAME_LEFT, IFRAME_TOP = 200, 150

PAGE = f"""<!doctype html><html><body style="margin:0">
<h1>oopif spike</h1>
<iframe id="a" src="https://example.org/" style="position:absolute;
 left:{IFRAME_LEFT}px; top:{IFRAME_TOP}px; width:600px; height:400px; border:0"></iframe>
<iframe id="b" src="https://httpbingo.org/forms/post" style="position:absolute;
 left:{IFRAME_LEFT}px; top:620px; width:800px; height:500px; border:0"></iframe>
</body></html>"""

DATA_URL = "data:text/html;base64," + base64.b64encode(PAGE.encode()).decode()

AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}


def _ax_value(v):
    return v.get("value") if isinstance(v, dict) else v


def _find_node(nodes, role_needle, name_needle=""):
    for n in nodes:
        if n.get("ignored"):
            continue
        role = str(_ax_value(n.get("role")) or "")
        name = str(_ax_value(n.get("name")) or "")
        if role == role_needle and name_needle.lower() in name.lower():
            if n.get("backendDOMNodeId") is not None:
                return n
    return None


def _quad_center(q):
    return (q[0] + q[2] + q[4] + q[6]) / 4, (q[1] + q[3] + q[5] + q[7]) / 4


async def main() -> None:
    sent: list[str] = []
    frames: dict[str, dict] = {}  # sessionId -> targetInfo
    info_changes: list[dict] = []

    ws_url = await _browser_ws_url(_CDP_PORT)
    cdp = await CDPClient.connect(ws_url)

    real_send = cdp.send

    async def send(method, params=None, session_id=None, timeout=30.0):
        sent.append(method)
        return await real_send(method, params, session_id, timeout)

    cdp.send = send  # type: ignore[method-assign]

    def on_event(msg):
        if msg.get("method") == "Target.attachedToTarget":
            p = msg["params"]
            if p["targetInfo"].get("type") == "iframe":
                frames[p["sessionId"]] = p["targetInfo"]
        elif msg.get("method") == "Target.targetInfoChanged":
            info_changes.append(msg["params"]["targetInfo"])

    cdp.add_listener(on_event)

    target_id = None
    try:
        created = await send("Target.createTarget", {"url": "about:blank"})
        target_id = created["targetId"]
        sid = (await send("Target.attachToTarget", {"targetId": target_id, "flatten": True}))[
            "sessionId"
        ]
        await send("Page.enable", {}, sid)
        await send("DOM.enable", {}, sid)
        await send("Target.setAutoAttach", AUTO_ATTACH, sid)

        print("== navigate to data: page with two cross-origin iframes")
        await send("Page.navigate", {"url": DATA_URL}, sid)
        for _ in range(30):
            if len(frames) >= 2:
                break
            await asyncio.sleep(0.5)
        # FINDING: attachedToTarget fires BEFORE the frame navigates — url is
        # usually "" at attach and arrives via targetInfoChanged. Resolve the
        # live url per target before matching.
        await asyncio.sleep(3)
        for fsid, info in frames.items():
            for change in info_changes:
                if change.get("targetId") == info["targetId"] and change.get("url"):
                    info["url"] = change["url"]
        print(f"   attached iframe targets: {len(frames)}")
        for fsid, info in frames.items():
            print(f"   - session={fsid} targetId={info['targetId']} url={info['url']}")
        if not frames:
            print("!! no OOPIF attached — auto-attach broken or site isolation off")
            return

        # ---- frame A (example.org): THE coordinate datum + click routing ----
        a_sid = next(
            (s for s, i in frames.items() if "example.org" in i["url"]), None
        )
        if a_sid is None:
            print("!! example.org frame not attached")
            return
        a_tid = frames[a_sid]["targetId"]
        await send("DOM.enable", {}, a_sid)
        await asyncio.sleep(2)  # let the frame paint
        ax = await send("Accessibility.getFullAXTree", {}, a_sid)
        link = _find_node(ax.get("nodes", []), "link", "learn more")
        if link is None:
            print("!! example.org link not found in child AX tree — dump:")
            for n in ax.get("nodes", [])[:40]:
                if n.get("ignored"):
                    continue
                print(
                    f"   role={_ax_value(n.get('role'))!r} "
                    f"name={_ax_value(n.get('name'))!r} "
                    f"backend={n.get('backendDOMNodeId')}"
                )
            print(f"   (total nodes: {len(ax.get('nodes', []))})")
            return
        backend = link["backendDOMNodeId"]
        quads = (
            await send("DOM.getContentQuads", {"backendNodeId": backend}, a_sid)
        )["quads"]
        lx, ly = _quad_center(quads[0])
        print(f"== child-session getContentQuads center: ({lx:.0f}, {ly:.0f})")
        frame_relative = lx < IFRAME_LEFT
        print(
            "   VERDICT: coordinates are "
            + ("FRAME-relative (offset needed)" if frame_relative else "VIEWPORT-relative")
        )

        owner = await send("DOM.getFrameOwner", {"frameId": a_tid}, sid)
        oquads = (
            await send(
                "DOM.getContentQuads", {"backendNodeId": owner["backendNodeId"]}, sid
            )
        )["quads"]
        ox, oy = oquads[0][0], oquads[0][1]
        print(f"== getFrameOwner offset (parent quads top-left): ({ox:.0f}, {oy:.0f})"
              f"  [expected ≈ ({IFRAME_LEFT}, {IFRAME_TOP})]")

        cx, cy = (lx + ox, ly + oy) if frame_relative else (lx, ly)
        print(f"== clicking on MAIN session at viewport ({cx:.0f}, {cy:.0f})")
        await send(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy}, sid
        )
        for etype in ("mousePressed", "mouseReleased"):
            await send(
                "Input.dispatchMouseEvent",
                {"type": etype, "x": cx, "y": cy, "button": "left", "clickCount": 1},
                sid,
            )
        await asyncio.sleep(3)
        navigated = [i for i in info_changes if "iana.org" in i.get("url", "")]
        print(
            "   click hit-test into frame: "
            + ("YES — frame navigated to iana.org" if navigated else
               "NO navigation observed (check manually)")
        )

        # ---- frame B (httpbin form): which session takes insertText ----
        b_sid = next((s for s, i in frames.items() if "httpbingo" in i["url"]), None)
        if b_sid is None:
            print("!! httpbin frame not attached — skipping type test")
            return
        await send("DOM.enable", {}, b_sid)
        box = None
        for _ in range(10):  # httpbin can be slow; poll for the form
            await asyncio.sleep(2)
            ax_b = await send("Accessibility.getFullAXTree", {}, b_sid)
            box = _find_node(ax_b.get("nodes", []), "searchbox") or _find_node(
                ax_b.get("nodes", []), "textbox"
            )
            if box is not None:
                break
        if box is None:
            print("!! no searchbox/textbox in httpbin frame AX tree — dump:")
            for n in ax_b.get("nodes", [])[:40]:
                if n.get("ignored"):
                    continue
                print(
                    f"   role={_ax_value(n.get('role'))!r} "
                    f"name={_ax_value(n.get('name'))!r} "
                    f"backend={n.get('backendDOMNodeId')}"
                )
            return
        bb = box["backendDOMNodeId"]

        async def value_of() -> str:
            res = await send(
                "Accessibility.getPartialAXTree",
                {"backendNodeId": bb, "fetchRelatives": False},
                b_sid,
            )
            nodes = res.get("nodes") or [{}]
            return str(_ax_value(nodes[0].get("value")) or "")

        await send("DOM.focus", {"backendNodeId": bb}, b_sid)
        await send("Input.insertText", {"text": "spike"}, b_sid)
        await asyncio.sleep(1)
        v = await value_of()
        print(f"== insertText on CHILD session → AXValue={v!r}")
        if not v:
            await send("DOM.focus", {"backendNodeId": bb}, b_sid)
            await send("Input.insertText", {"text": "spike"}, sid)
            await asyncio.sleep(1)
            v = await value_of()
            print(f"== insertText on MAIN session → AXValue={v!r}")
        print(
            "   VERDICT: insertText targets the "
            + ("CHILD session" if v == "spike" else "??? (neither registered)")
        )
    finally:
        runtime = [m for m in sent if m.startswith("Runtime.")]
        print(f"== methods sent: {len(sent)}; Runtime.* count: {len(runtime)} "
              + ("(OK)" if not runtime else "(VIOLATION!)"))
        if target_id:
            try:
                await real_send("Target.closeTarget", {"targetId": target_id})
            except Exception as exc:  # noqa: BLE001 — teardown is best-effort
                print(f"   closeTarget failed: {exc}")
        await cdp.close()


if __name__ == "__main__":
    asyncio.run(main())
