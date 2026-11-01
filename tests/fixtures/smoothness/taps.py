#!/opt/homebrew/bin/python3
"""Suite `taps` — BK-029 (plan step 4, REVISED by the user 2026-10-31: any tap
or press in the chat stops following; "↓ Latest" resumes) and D1 (opening a
pill drawer or thinking body mid-stream pauses following), on an OVERFLOWING
log (`tapper2` / `thinker` scenarios, instrument.js). Ports: webkit/iphone
and chromium/iphone-4xcpu (the extra.py pair). Every row is a hard assert.

  text-{touch,mouse}-unpins-latest-resumes  a bare press on prose mid-run
      unpins at once, stays unpinned while the answer grows (every 16 ms
      sample), and one tap on "↓ Latest" re-pins and settles at the bottom
  tap-under-pill-stays-unpinned  the lift's click lands on a pill that just
      appeared under the finger — it must not re-pin
  latest-mid-glide-{touch,mouse}-lands  "↓ Latest" tapped while an (emulated)
      iOS momentum glide still carries the log upward lands at the bottom, pinned
  drag-unpins / real-cdp-drag-unpins  a touch drag (WebKit: dispatch_event
      touchstart/move/end — Playwright has no touch-drag API; chromium also a
      real CDP drag)
  d1-pill-open-pauses-{touch,mouse,keyboard} / d1-think-open-pauses-{touch,mouse}
  idle-open-unchanged  not streaming: opening stays pinned, no "↓ Latest"

Run through run_probe.py (`--only=taps`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/taps.py [--engines=webkit] [--webui=DIR]
"""
from __future__ import annotations

import json
import sys

import common
from common import send
from stream import TAP_STATE

ROWS = common.Rows()
PORT = 0
PREFIX = ""
# taps ports: the iPhone in both engines (the iPad's wider log changes nothing here).
PORTS = [d for d in common.DEVICES if d[1] in ("iphone", "iphone-4xcpu")]


def boot(b, vp, cpu):
    return common.boot(b, PORT, vp, cpu)


def verdict(r, name, ok, detail):
    ROWS.verdict(r, f"{PREFIX}{name}", ok, detail)


def text_point(page):
    """Centre of a visible prose line that is NOT a control (a bare tap lands on text)."""
    return page.evaluate("""() => { const l = document.querySelector('#chat-log').getBoundingClientRect();
      const ps = [...document.querySelectorAll('#chat-log .msg.agent .md p')].reverse();
      for (const p of ps) { const r = p.getBoundingClientRect();
        const y = r.top + 8; if (y < l.top + 20 || y > l.bottom - 20) continue;
        const x = l.left + l.width / 2, e = document.elementFromPoint(x, y);
        if (e && !e.closest('button, a, summary, [role=button]')) return { x, y }; }
      return null; }""")


def synth_touch(page, dy, hold_ms=60):
    """touchstart → touchmove(+dy) → touchend on #chat-log, dispatched (isTrusted false) — the
    one way to drive a touch DRAG in WebKit (Playwright has no drag API); it moves no pixels."""
    log = page.locator("#chat-log")
    box = log.bounding_box(); x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    pt = lambda yy: {"touches": [{"identifier": 7, "clientX": x, "clientY": yy}]}
    log.dispatch_event("touchstart", pt(y))
    if dy:
        for k in range(1, 4):
            log.dispatch_event("touchmove", pt(y + dy * k / 3))
    page.wait_for_timeout(hold_ms)
    log.dispatch_event("touchend", {"touches": []})


def in_view(page, sel):
    return page.evaluate("""(s) => { const e = [...document.querySelectorAll(s)].pop(); if (!e || e.hidden) return false;
      const r = e.getBoundingClientRect(), l = document.querySelector('#chat-log').getBoundingClientRect();
      return r.bottom > l.top + 4 && r.top < l.bottom - 4; }""", sel)


def mid_tool(page, scenario="tapper2"):
    send(page, scenario)
    page.wait_for_function("() => document.querySelector('.pill.running')", timeout=30000)
    page.wait_for_timeout(1200)


def answer_4s(page):
    page.wait_for_function("() => document.querySelectorAll('.msg.agent .md').length >= 2", timeout=20000)
    page.wait_for_timeout(4000)
    return page.evaluate(TAP_STATE)


WATCH = """() => { const W = window.__W = { unpinned: 0, pill: 0, n: 0 };
  W.id = setInterval(() => { W.n++; if (!window.__probe.metrics().pinned) W.unpinned++;
    if (!document.querySelector('#chat-jump').hidden) W.pill++; }, 16); }"""
UNWATCH = "() => { clearInterval(window.__W.id); return window.__W; }"

# An iOS momentum glide upward (c3): owns the position each frame, ignores other writes, ends
# at the top, after ~3 s, or the moment #chat-log's overflow-y is hidden (WebKit's cancel).
GLIDE = """() => { const log = document.querySelector('#chat-log');
  const G = window.__glide = { frames: 0, stopped: null }; let y = log.scrollTop, v = 40;
  const t0 = performance.now();
  const step = () => {
    if (getComputedStyle(log).overflowY === 'hidden') { G.stopped = 'overflow'; return; }
    v *= 0.985; y = Math.max(0, y - v); log.scrollTop = y; G.frames++;
    if (y <= 0 || performance.now() - t0 > 3000) { G.stopped = 'end'; return; }
    requestAnimationFrame(step); };
  requestAnimationFrame(step); }"""


def settled(page):
    """The run closed and the spring had 1.5 s: a follower sits AT the bottom (gap 0)."""
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=120000)
    page.wait_for_timeout(1500)
    return page.evaluate(TAP_STATE)


def taps(b, vp, cpu, engine):
    r = {}
    st = lambda: page.evaluate(TAP_STATE)
    # (0) CONTROL: a never-touched follower at the same instant — streaming, the spring trails
    # the paced reveal by tens of px, so "gap ≈ 0" mid-answer means "≈ this", and 0 once settled.
    page, _ = boot(b, vp, cpu); mid_tool(page)
    ctl = answer_4s(page); ctl_end = settled(page)
    r["control"] = {"4s_into_answer": ctl, "settled": ctl_end}
    page.close()
    near = lambda g: g <= ctl["gap"] + 60   # a follower's trail, not a reader's 1,500 px
    tap_at = lambda page, x, y: page.touchscreen.tap(x, y)
    def latest(page, how="touch"):
        jb = page.locator("#chat-jump").bounding_box()
        if jb is None: return None
        x, y = jb["x"] + jb["width"] / 2, jb["y"] + jb["height"] / 2
        tap_at(page, x, y) if how == "touch" else page.mouse.click(x, y)
        page.wait_for_timeout(1200); return st()
    # (a) BK-029 REVISED (user, 2026-10-31: any tap stops following): a bare press on TEXT
    # mid-run unpins at once and stays unpinned while the answer grows; "↓ Latest" resumes.
    for how in ("touch", "mouse"):
        page, _ = boot(b, vp, cpu); mid_tool(page)
        pt = text_point(page); before = st()
        tap_at(page, pt["x"], pt["y"]) if how == "touch" else page.mouse.click(pt["x"], pt["y"])
        page.wait_for_timeout(200)
        after = st(); page.evaluate(WATCH); later = answer_4s(page); w = page.evaluate(UNWATCH)
        resumed = latest(page, how); end = settled(page)
        r[f"text_{how}"] = {"before": before, "after": after, "4s_into_answer": later,
                            "samples_pinned": [w["n"] - w["unpinned"], w["n"]],
                            "after_latest_tap": resumed, "settled": end}
        verdict(r, f"text-{how}-unpins-latest-resumes", before["pinned"] and not after["pinned"]
                and after["jumpPill"] and not later["pinned"] and later["jumpPill"]
                and later["gap"] > ctl["gap"] + 300 and w["unpinned"] == w["n"]
                and resumed is not None and resumed["pinned"] and near(resumed["gap"])
                and end["pinned"] and end["gap"] <= 2, r[f"text_{how}"])
        page.close()
    # (b) a tap exactly where the pill then appears: that tap's click (hit-tested at the lift)
    # must not land on the new pill and re-pin.
    page, _ = boot(b, vp, cpu); mid_tool(page)
    spot = page.evaluate("""() => { const l = document.querySelector('#chat-log').getBoundingClientRect();
      return { x: l.left + l.width / 2, y: l.bottom - 34 }; }""")
    tap_at(page, spot["x"], spot["y"]); page.wait_for_timeout(200); after = st()
    page.wait_for_timeout(800); later = st()
    r["under_pill_tap"] = {"spot": spot, "after": after, "1s_later": later}
    verdict(r, "tap-under-pill-stays-unpinned", not after["pinned"] and after["jumpPill"]
            and not later["pinned"] and later["jumpPill"], r["under_pill_tap"])
    page.close()
    # (c) drags unpin (synthetic: any touchstart does; chromium also a real CDP drag below).
    page, _ = boot(b, vp, cpu); mid_tool(page)
    synth_touch(page, 12); page.wait_for_timeout(100); drag = st()
    r["drag"] = {"12px": drag}
    verdict(r, "drag-unpins", not drag["pinned"] and drag["jumpPill"], r["drag"])
    # (c2) BK-039: iOS sends NO click for a tap that stops a momentum glide — only touchstart/end.
    # A still touch on "↓ Latest" must re-pin from those alone; a touch that slides off must not.
    jump = page.locator("#chat-jump"); jb = jump.bounding_box()
    jx, jy = jb["x"] + jb["width"] / 2, jb["y"] + jb["height"] / 2
    pt = lambda dx: {"touches": [{"identifier": 9, "clientX": jx + dx, "clientY": jy}],
                     "changedTouches": [{"identifier": 9, "clientX": jx + dx, "clientY": jy}]}
    jump.dispatch_event("touchstart", pt(0)); jump.dispatch_event("touchend", pt(30))
    page.wait_for_timeout(200); slid = st()
    jump.dispatch_event("touchstart", pt(0)); jump.dispatch_event("touchend", pt(2))
    page.wait_for_timeout(1200); glide = st()
    r["glide_tap"] = {"slid_off": slid, "still_touch": glide}
    verdict(r, "latest-touch-without-click-repins", not slid["pinned"] and slid["jumpPill"]
            and glide["pinned"] and not glide["jumpPill"] and near(glide["gap"]), r["glide_tap"])
    page.close()
    # (c3) 2026-11-01 (Tom, iPhone): "↓ Latest" tapped WHILE the log is still gliding upward.
    # The pill sits outside #chat-log, so the tap does not stop the log's momentum, and the glide
    # keeps scrolling up over our writes (its upward moves then read as the user's: unpinned again).
    # GLIDE emulates that: a per-frame upward trajectory that ignores our writes and halts only
    # when the log stops being a scroller (overflow-y hidden — how WebKit cancels a glide).
    for how in ("touch", "mouse"):
        page, _ = boot(b, vp, cpu); mid_tool(page); answer_4s(page)
        synth_touch(page, 12); page.evaluate(GLIDE); page.wait_for_timeout(250)
        mid = st(); landed = latest(page, how); g = page.evaluate("() => window.__glide")
        end = settled(page)
        r[f"glide_{how}"] = {"gliding": mid, "after_latest_tap": landed, "glide": g, "settled": end}
        verdict(r, f"latest-mid-glide-{how}-lands", not mid["pinned"] and mid["jumpPill"]
                and landed is not None and landed["pinned"] and not landed["jumpPill"]
                and near(landed["gap"]) and end["pinned"] and end["gap"] <= 2, r[f"glide_{how}"])
        page.close()
    if engine == "chromium":
        page, _ = boot(b, vp, cpu); mid_tool(page)
        cdp = page.context.new_cdp_session(page)
        box = page.locator("#chat-log").bounding_box(); x, y = box["x"] + box["width"] / 2, box["y"] + 200
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for k in range(1, 7):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y + 5 * k}]})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
        page.wait_for_timeout(200); real = st()
        r["cdp_drag_30px"] = real
        verdict(r, "real-cdp-drag-unpins", not real["pinned"] and real["jumpPill"] and real["gap"] >= 10, real)
        page.close()
    # (d) D1: opening the running pill's drawer pauses following — touch, mouse and keyboard.
    for how in ("touch", "mouse", "keyboard"):
        page, _ = boot(b, vp, cpu); mid_tool(page)
        pill = page.locator(".pill.running"); bx = pill.bounding_box()
        cx, cy = bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2
        if how == "touch": page.touchscreen.tap(cx, cy)
        elif how == "mouse": page.mouse.click(cx, cy)
        else: pill.focus(); page.keyboard.press("Enter")
        page.wait_for_timeout(200)
        after = st(); opened = page.evaluate("() => !document.querySelector('.pill-detail').hidden")
        later = answer_4s(page); drawer_seen = in_view(page, ".pill-detail")
        jb = page.locator("#chat-jump").bounding_box()  # None: no "↓ Latest" to tap (HEAD's mouse path)
        if jb and how == "touch": page.touchscreen.tap(jb["x"] + jb["width"] / 2, jb["y"] + jb["height"] / 2)
        elif jb: page.mouse.click(jb["x"] + jb["width"] / 2, jb["y"] + jb["height"] / 2)
        page.wait_for_timeout(1200); resumed = st(); end = settled(page)
        r[f"d1_pill_{how}"] = {"opened": opened, "after": after, "4s_into_answer": later,
                               "drawer_still_in_view": drawer_seen, "after_latest_tap": resumed, "settled": end}
        verdict(r, f"d1-pill-open-pauses-{how}", opened and not after["pinned"] and after["jumpPill"]
                and not later["pinned"] and later["jumpPill"] and drawer_seen
                and resumed["pinned"] and not resumed["jumpPill"] and near(resumed["gap"])
                and end["pinned"] and end["gap"] <= 2, r[f"d1_pill_{how}"])
        page.close()
    # (e) D1 on the thinking strip mid-thinking — touch and mouse.
    for how in ("touch", "mouse"):
        page, _ = boot(b, vp, cpu); send(page, "thinker")
        page.wait_for_function("() => { const h = document.querySelector('.think.streaming .think-head'); if (!h) return false;"
                               " const r = h.getBoundingClientRect(), l = document.querySelector('#chat-log').getBoundingClientRect();"
                               " return r.top > l.top && r.bottom < l.bottom && l.height < document.querySelector('#chat-log').scrollHeight; }",
                               timeout=30000)
        page.wait_for_timeout(300)
        hb = page.locator(".think.streaming .think-head").bounding_box(); before = st()
        cx, cy = hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2
        if how == "touch": page.touchscreen.tap(cx, cy)
        else: page.mouse.click(cx, cy)
        page.wait_for_timeout(200)
        after = st(); opened = page.evaluate("() => document.querySelector('.think').classList.contains('open')")
        # close it again: following must NOT resume on a close
        # Over the label, not the centre: D2 (BK-030) made the streaming strip the log's last row, and
        # the now-shown "↓ Latest" floats over the bottom edge's centre — a centre tap is a tap on IT.
        hb2 = page.locator(".think .think-head").last.bounding_box()
        if hb2 and hb2["y"] > 0:
            if how == "touch": page.touchscreen.tap(hb2["x"] + 24, hb2["y"] + hb2["height"] / 2)
            else: page.mouse.click(hb2["x"] + 24, hb2["y"] + hb2["height"] / 2)
        page.wait_for_timeout(200); closed = st()
        r[f"d1_think_{how}"] = {"before": before, "opened": opened, "after": after, "after_close": closed}
        verdict(r, f"d1-think-open-pauses-{how}", before["pinned"] and opened and not after["pinned"]
                and after["jumpPill"] and not closed["pinned"] and closed["jumpPill"], r[f"d1_think_{how}"])
        page.close()
    # (f) not streaming: opening behaves as before D1 (stays pinned, no pill).
    page, _ = boot(b, vp, cpu); send(page, "tapper2")
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=120000)
    page.wait_for_timeout(1500)
    # The pill sits a long answer above the bottom; scrolling to it would unpin, so click it in
    # place — the same click handler touch, mouse and keyboard reach.
    before = st(); page.locator(".pill").last.dispatch_event("click"); page.wait_for_timeout(300)
    after = st(); opened = page.evaluate("() => !document.querySelector('.pill-detail').hidden")
    r["idle_open"] = {"before": before, "opened": opened, "after": after}
    verdict(r, "idle-open-unchanged", opened and before["pinned"] and after["pinned"] and not after["jumpPill"], r["idle_open"])
    page.close()
    return r


def main(argv: list[str]) -> int:
    global PORT, PREFIX
    args = common.suite_args(argv)
    from playwright.sync_api import sync_playwright
    common.hang_alarm(1500)
    common.build_site(args.webui)
    httpd, PORT = common.serve_site()
    try:
        with sync_playwright() as pw:
            for engine, name, vp, cpu in [d for d in PORTS if d[0] in args.engines]:
                PREFIX = f"[{engine}/{name}] taps: "
                print(f"== {engine}/{name} ==", flush=True)
                b = common.launch(pw, engine)
                try:
                    r = taps(b, vp, cpu, engine)
                    print(f"{engine}/{name}", json.dumps(r)[:2000], flush=True)
                except Exception as exc:  # a crashed scenario is a failed one, loudly
                    ROWS.check(f"{PREFIX}scenario ran", False, repr(exc)[:300])
                b.close()
    finally:
        httpd.shutdown()
    return ROWS.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
