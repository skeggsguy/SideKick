#!/opt/homebrew/bin/python3
"""Suite `toggles` — BK-030 (goal 5) toggle-anchor and BK-031 (goal 6) nested
scrollers, on every device port.

Toggle-anchor (run_toggles): an UNPINNED reader taps a thinking header / a tool
pill and the tapped control holds its screen y (<= 1 px) on EVERY frame of the
animated open or close — sampled per rAF. Covers the clamp regime (a close near
the bottom, where the browser would clamp scrollTop every frame), the in-place
close (no slack needed — and none added), a pinned follower closing by keyboard
(just follows: pinned and at the bottom on every frame), a streaming strip
opened live and closed mid-stream, and reduced motion (static skeleton,
instant accordion). Each animated case must also span >= 5 in-between frames
(the accordion really animates).

Show all (run_showall, BK-031 / D3): 0 inner vertical scrollers in the
transcript (DOM check, both bodies open), a wheel over an open drawer /
thinking body reaches the log's bottom, and "Show all" / "Show less" hold the
reader's anchor — expand: the preview holds still; collapse: the tapped button
holds (<= 1 px per frame).

Red discipline without touching the served tree:
  --red=slack   pins #chat-log's padding-bottom from the probe page (follow.js's
                slack then reserves nothing): the near-bottom closes must FAIL
  --red=keepat  serves a copy of follow.js whose keepAt() just runs its
                mutation (no hold): every "Show less" hold must FAIL

Run through run_probe.py (`--only=toggles [--red=...]`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/toggles.py [--engines=webkit] [--webui=DIR] [--red=slack|keepat]
"""
from __future__ import annotations

import json
import sys

import common

SAMPLE = """([sel, idx]) => { const log = document.querySelector('#chat-log');
  const el = document.querySelectorAll(sel)[idx], acc = el.closest('.think, .pill-row').querySelector('.acc-in');
  const S = window.__ta = { rows: [], on: true };
  const tick = () => { if (!S.on) return; const r = el.getBoundingClientRect();
    S.rows.push({ y: r.top, h: acc.getBoundingClientRect().height, top: log.scrollTop, pinned: window.__probe.metrics().pinned,
      pill: !document.querySelector('#chat-jump').hidden, gap: log.scrollHeight - log.clientHeight - log.scrollTop,
      slack: parseFloat(getComputedStyle(log).getPropertyValue('--slack')) || 0 });
    requestAnimationFrame(tick); };
  tick(); }"""
# Would closing this control's accordion clamp scrollTop if nothing reserved it? (top > maxTop - h)
CLAMPS = """([sel, idx]) => { const log = document.querySelector('#chat-log'), el = document.querySelectorAll(sel)[idx];
  const h = el.closest('.think, .pill-row').querySelector('.acc-in').getBoundingClientRect().height;
  return { h: Math.round(h), wouldClamp: log.scrollTop > log.scrollHeight - log.clientHeight - h + 1 }; }"""


def sampled(page, sel, idx, act, ms=520):
    page.evaluate(SAMPLE, [sel, idx])
    page.wait_for_timeout(50)
    act()
    page.wait_for_timeout(ms)
    rows = page.evaluate("() => { window.__ta.on = false; return window.__ta.rows; }")
    y0, hs = rows[0]["y"], [r["h"] for r in rows]
    lo, hi = min(hs), max(hs)
    return {"frames": len(rows), "max_dy": round(max(abs(r["y"] - y0) for r in rows), 2),
            "animated_frames": sum(lo + 1 < h < hi - 1 for h in hs), "h": [round(lo), round(hi)],
            "end": {k: (round(v, 1) if isinstance(v, float) else v) for k, v in rows[-1].items()},
            "always_pinned": all(r["pinned"] for r in rows), "max_gap": round(max(r["gap"] for r in rows), 1)}


def tap(page, sel, idx, left=False):
    """A real touch tap — at the control's centre, or (left) over a header's glyph/label: once unpinned,
    "↓ Latest" floats over the bottom edge's centre and a tap there is the reader's tap on IT."""
    b = page.locator(sel).nth(idx).bounding_box()
    x = b["x"] + (24 if left else b["width"] / 2)
    return lambda: page.touchscreen.tap(x, b["y"] + min(b["height"] / 2, 20))


GAP = """() => { const l = document.querySelector('#chat-log'); return Math.round(l.scrollHeight - l.clientHeight - l.scrollTop); }"""


def to_bottom(page):
    """Real wheel down to the very bottom (a downward user scroll: maybeRepin re-pins)."""
    b = page.locator("#chat-log").bounding_box()
    # Aim at the log's own 14 px padding. Before step 6 (BK-031) an open .pill-detail / .think-body was an
    # inner scroller (overscroll-behavior: contain) that ate the wheel — the log stuck 86 px short with the
    # drawer at scrollTop 121/121. That trap is gone (run_showall's wheel-over-* rows); the aim stays put.
    page.mouse.move(b["x"] + 6, b["y"] + b["height"] / 2)
    for _ in range(6):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(150)
        if page.evaluate(GAP) <= 1:
            break
    page.wait_for_timeout(300)
    assert page.evaluate(GAP) <= 1, f"to_bottom: never reached the bottom (gap {page.evaluate(GAP)})"


def run_toggles(boot_page, verdict, streaming_key="thinker"):
    out = {}
    PILL, HEAD = "#chat-log .pill", "#chat-log .think-head"
    page, errs = boot_page()
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["toggler", "sid-toggler"])
    page.fill("#chat-input", "go"); page.click("#chat-send")
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=60000)
    page.wait_for_timeout(1500)
    last_head = page.locator(HEAD).count() - 1
    anim = lambda r: r["animated_frames"] >= 5
    # 1. opens grow BELOW the tapped control: nothing may move it (and no slack is ever added).
    r = out["pill_open"] = sampled(page, PILL, 0, tap(page, PILL, 0))
    verdict(out, "pill-open-holds", r["max_dy"] <= 1 and anim(r) and not r["end"]["pinned"]
            and r["end"]["slack"] == 0, r)
    out["pill_close_inplace_regime"] = c = page.evaluate(CLAMPS, [PILL, 0])
    r = out["pill_close_inplace"] = sampled(page, PILL, 0, tap(page, PILL, 0))
    verdict(out, "pill-close-in-place-holds-no-slack", not c["wouldClamp"] and r["max_dy"] <= 1 and anim(r)
            and r["end"]["slack"] == 0, [c, r])
    r = out["think_open"] = sampled(page, HEAD, last_head, tap(page, HEAD, last_head))
    verdict(out, "think-open-holds", r["max_dy"] <= 1 and anim(r) and r["end"]["slack"] == 0, r)
    # 2. close in place (the body still has room below it): holds, and needs no slack.
    out["think_close_inplace_regime"] = c = page.evaluate(CLAMPS, [HEAD, last_head])
    r = out["think_close_inplace"] = sampled(page, HEAD, last_head, tap(page, HEAD, last_head))
    verdict(out, "think-close-in-place-holds-no-slack", not c["wouldClamp"] and r["max_dy"] <= 1 and anim(r)
            and r["end"]["slack"] == 0, [c, r])
    # 3. the clamp regime: re-open, ride to the very bottom (re-pins), close by tap (the press unpins).
    tap(page, HEAD, last_head)(); page.wait_for_timeout(400)
    to_bottom(page)
    out["think_close_regime"] = c = page.evaluate(CLAMPS, [HEAD, last_head])
    r = out["think_close_clamp"] = sampled(page, HEAD, last_head, tap(page, HEAD, last_head))
    verdict(out, "think-close-near-bottom-holds", c["wouldClamp"] and r["max_dy"] <= 1 and anim(r)
            and r["end"]["slack"] > 0, [c, r])
    tap(page, PILL, 0)(); page.wait_for_timeout(400)
    to_bottom(page)
    out["pill_close_regime"] = c = page.evaluate(CLAMPS, [PILL, 0])
    r = out["pill_close_clamp"] = sampled(page, PILL, 0, tap(page, PILL, 0))
    verdict(out, "pill-close-near-bottom-holds", c["wouldClamp"] and r["max_dy"] <= 1 and anim(r)
            and r["end"]["slack"] > 0, [c, r])
    jb = page.locator("#chat-jump").bounding_box()
    page.touchscreen.tap(jb["x"] + jb["width"] / 2, jb["y"] + jb["height"] / 2); page.wait_for_timeout(800)
    out["after_latest"] = st = page.evaluate("""() => { const l = document.querySelector('#chat-log');
      return { pinned: window.__probe.metrics().pinned, gap: Math.round(l.scrollHeight - l.clientHeight - l.scrollTop),
               slack: parseFloat(getComputedStyle(l).getPropertyValue('--slack')) || 0 }; }""")
    verdict(out, "latest-repins-drops-slack", st["pinned"] and st["gap"] <= 2 and st["slack"] == 0, st)
    # A pinned follower (idle, keyboard — no press, so no unpin): open, ride to the bottom, close.
    page.locator(PILL).nth(0).dispatch_event("click"); page.wait_for_timeout(400)
    to_bottom(page)
    page.evaluate("() => document.querySelectorAll('#chat-log .pill')[0].focus({ preventScroll: true })")
    out["pinned_close_regime"] = c = page.evaluate(CLAMPS, [PILL, 0])
    r = out["pinned_close"] = sampled(page, PILL, 0, lambda: page.keyboard.press("Enter"))
    verdict(out, "pinned-follower-close-just-follows", c["wouldClamp"] and r["always_pinned"] and r["max_gap"] <= 1
            and r["end"]["slack"] == 0 and not r["end"]["pill"] and anim(r), [c, r])
    out["pageerrors"] = errs[:3]
    page.close()
    # Streaming: open the live strip (it grows while open), then close it mid-stream.
    page, errs = boot_page()
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [streaming_key, "sid-thinker"])
    page.fill("#chat-input", "go"); page.click("#chat-send")
    page.wait_for_function("() => { const h = document.querySelector('.think.streaming .think-head'); if (!h) return false;"
                           " const r = h.getBoundingClientRect(), l = document.querySelector('#chat-log').getBoundingClientRect();"
                           " return r.top > l.top && r.bottom < l.bottom && l.height < document.querySelector('#chat-log').scrollHeight; }",
                           timeout=30000)
    page.wait_for_timeout(300)
    # Centre tap on purpose: the strip is the log's LAST row, the press shows "↓ Latest" right under the
    # finger, and the ghost click must still reach the header (follow.js pressBtn forward).
    r = out["stream_open"] = sampled(page, HEAD, 0, tap(page, HEAD, 0), ms=1200)
    live = page.evaluate("() => { const t = document.querySelector('.think'); return { streaming: t.classList.contains('streaming'),"
                         " bodyChars: t.querySelector('.think-body').textContent.length }; }")
    page.wait_for_timeout(400)
    grew = page.evaluate("() => document.querySelector('.think .think-body').textContent.length")
    verdict(out, "stream-open-holds-and-stays-live", r["max_dy"] <= 1 and anim(r) and live["streaming"]
            and grew > live["bodyChars"] > 0, [r, live, grew])
    out["stream_close_regime"] = c = page.evaluate(CLAMPS, [HEAD, 0])
    r = out["stream_close"] = sampled(page, HEAD, 0, tap(page, HEAD, 0, left=True))
    verdict(out, "stream-close-holds", r["max_dy"] <= 1 and anim(r), [c, r])
    out["pageerrors"] += errs[:3]
    page.close()
    # Reduced motion: the skeleton is static, the accordion snaps (no in-between frame), and the
    # near-bottom close still holds the header (an instant shrink, reserved the same way).
    page, errs = boot_page()
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [streaming_key, "sid-thinker-rm"])
    page.fill("#chat-input", "go"); page.click("#chat-send")
    page.wait_for_selector(".think.streaming .think-skel > span", timeout=30000)
    out["rm_skeleton"] = sk = page.evaluate("""() => { const s = getComputedStyle(document.querySelector('.think-skel > span'));
      return { animation: s.animationName, dur: getComputedStyle(document.querySelector('#chat-log')).getPropertyValue('--acc-dur').trim() }; }""")
    out["pageerrors"] += errs[:3]
    page.close()
    page, errs = boot_page()  # the toggler shape: its strip ends near the bottom, where a close can clamp
    page.emulate_media(reduced_motion="reduce")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["toggler", "sid-toggler-rm"])
    page.fill("#chat-input", "go"); page.click("#chat-send")
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=60000)
    page.wait_for_timeout(1500)
    last = page.locator(HEAD).count() - 1
    r = out["rm_open"] = sampled(page, HEAD, last, tap(page, HEAD, last), ms=300)
    to_bottom(page)
    out["rm_close_regime"] = c = page.evaluate(CLAMPS, [HEAD, last])
    r2 = out["rm_close"] = sampled(page, HEAD, last, tap(page, HEAD, last), ms=300)
    verdict(out, "reduced-motion-static-and-instant", sk["animation"] == "none" and sk["dur"] == "0s"
            and r["animated_frames"] == 0 and r["h"][1] > 40 and r["max_dy"] <= 1
            and c["wouldClamp"] and r2["animated_frames"] == 0 and r2["max_dy"] <= 1, [sk, r, c, r2])
    out["pageerrors"] += errs[:3]
    page.close()
    out["showall"] = run_showall(boot_page, verdict)
    out["pageerrors"] += out["showall"].pop("pageerrors")
    verdict(out, "no-page-errors", not out["pageerrors"], out["pageerrors"])
    return out


# Step 6 (goal 6, BK-031, D3). Every element inside #chat-log that a VERTICAL gesture could scroll: overflow-y
# auto|scroll and taller content than its box (`vertical`), plus any auto|scroll box with a max-height cap — a
# scroller-in-waiting even while its content is short (`capped`). Horizontal-only scrollers (wide code blocks,
# tables) stay allowed and are listed for the record: `overflow-x: auto` computes overflow-y to auto too, which
# is why `vertical` also requires real vertical overflow.
SCROLLERS = """() => { const all = [...document.querySelectorAll('#chat-log *')];
  const name = (e) => e.tagName.toLowerCase() + [...e.classList].map((c) => '.' + c).join('');
  const sc = (v) => v === 'auto' || v === 'scroll';
  const vertical = all.filter((e) => sc(getComputedStyle(e).overflowY) && e.scrollHeight > e.clientHeight + 1).map(name);
  const capped = all.filter((e) => { const s = getComputedStyle(e); return sc(s.overflowY) && s.maxHeight !== 'none'; }).map(name);
  const horizontal = all.filter((e) => sc(getComputedStyle(e).overflowX) && e.scrollWidth > e.clientWidth + 1).map(name);
  return { vertical, capped, horizontal: [...new Set(horizontal)], checked: all.length }; }"""
# A body's clip state: the box, its "Show all" button, whether more is hidden below the cap.
CLIP = """(sel) => { const b = document.querySelector(sel); if (!b) return null;
  const btn = b.parentElement.querySelector(':scope > .show-all');
  return { clipped: b.scrollHeight > b.clientHeight + 1, h: Math.round(b.clientHeight), full: b.scrollHeight,
           fade: b.classList.contains('clipped'), expanded: b.classList.contains('expanded'),
           btn: btn ? { shown: !btn.hidden, text: btn.textContent, aria: btn.getAttribute('aria-expanded') } : null }; }"""
THINK_BOX, DRAWER_BOX = ".think .think-text", ".pill-detail-body"
THINK_BTN, DRAWER_BTN = ".think .show-all", ".pill-row .show-all"


def wheel_over(page, sel):
    """Centre `sel` in the log, put the mouse ON it and wheel down: the log must reach its bottom. Before step 6
    an open .pill-detail / .think-body was an inner scroller with overscroll-behavior: contain — the wheel
    scrolled THAT box to its end and never chained (the log stuck 86 px short, 2026-10-31)."""
    page.evaluate("(s) => document.querySelector(s).scrollIntoView({ block: 'center' })", sel)
    page.wait_for_timeout(300)
    b = page.locator(sel).first.bounding_box()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + min(b["height"] / 2, 120))
    for _ in range(8):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(150)
        if page.evaluate(GAP) <= 1:
            break
    page.wait_for_timeout(300)
    return page.evaluate(GAP)


def centre(page, sel):
    page.evaluate("(s) => document.querySelector(s).scrollIntoView({ block: 'center' })", sel)
    page.wait_for_timeout(300)


def guarded(out, verdict, name, fn):
    """A check whose page state may not exist on a red tree (no .show-all before step 6): record FAIL, go on."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — a probe records the failure as a FAIL row, never hides it
        verdict(out, name, False, f"{type(e).__name__}: {str(e)[:200]}")


def run_showall(boot_page, verdict):
    out = {}
    page, errs = boot_page()
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["longbody", "sid-longbody"])
    page.fill("#chat-input", "go"); page.click("#chat-send")

    def mid_stream_keyboard():
        # D1: the reader opens the live strip by keyboard (pauses), resumes with "↓ Latest", then once the live
        # body outgrows its cap, "Show all" by keyboard pauses following again — same rule as any open.
        page.wait_for_selector(".think.streaming .think-head", timeout=30000)
        page.evaluate("() => document.querySelector('.think.streaming .think-head').focus({ preventScroll: true })")
        page.keyboard.press("Enter"); page.wait_for_timeout(200)
        page.locator("#chat-jump").click(); page.wait_for_timeout(200)
        page.wait_for_function("(s) => { const b = document.querySelector(s); return b && !b.hidden; }",
                               arg=THINK_BTN, timeout=15000)
        before = page.evaluate("() => ({ pinned: window.__probe.metrics().pinned, streaming: !!document.querySelector('.think.streaming') })")
        page.evaluate("(s) => document.querySelector(s).focus({ preventScroll: true })", THINK_BTN)
        page.keyboard.press("Enter"); page.wait_for_timeout(200)
        after = page.evaluate(TAP_ST)
        out["kbd_mid_stream"] = d = {"before": before, "after": after, "clip": page.evaluate(CLIP, THINK_BOX)}
        verdict(out, "show-all-keyboard-mid-stream-pauses", before["pinned"] and before["streaming"]
                and not after["pinned"] and after["pill"] and d["clip"]["expanded"] and not d["clip"]["clipped"], d)
    guarded(out, verdict, "show-all-keyboard-mid-stream-pauses", mid_stream_keyboard)
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=60000)
    page.wait_for_timeout(1500)
    PILL, HEAD = "#chat-log .pill", "#chat-log .think-head"

    def offered():
        # Close + reopen the strip (an open always starts clipped), open the LONG pill's drawer.
        if page.evaluate("() => document.querySelector('.think').classList.contains('open')"):
            centre(page, HEAD); tap(page, HEAD, 0, left=True)(); page.wait_for_timeout(400)
        centre(page, HEAD); tap(page, HEAD, 0, left=True)(); page.wait_for_timeout(400)
        centre(page, PILL); tap(page, PILL, 0)(); page.wait_for_timeout(400)
        t, d = page.evaluate(CLIP, THINK_BOX), page.evaluate(CLIP, DRAWER_BOX)
        out["offered"] = [t, d]
        ok = lambda c: c and c["clipped"] and c["fade"] and not c["expanded"] and c["btn"] == {
            "shown": True, "text": "Show all", "aria": "false"}
        verdict(out, "show-all-offered-when-taller", ok(t) and ok(d), [t, d])
    guarded(out, verdict, "show-all-offered-when-taller", offered)
    out["scrollers"] = s = page.evaluate(SCROLLERS)
    both_open = page.evaluate("() => !document.querySelector('.think-body').hidden && !document.querySelector('.pill-detail').hidden")
    verdict(out, "no-inner-vertical-scrollers", both_open and not s["vertical"] and not s["capped"], [both_open, s])
    for name, sel in (("drawer", ".pill-detail"), ("thinking", ".think-body")):
        gap = out[f"wheel_{name}"] = wheel_over(page, sel)
        verdict(out, f"wheel-over-open-{name}-reaches-log-bottom", gap <= 1, {"gap": gap})

    def anchors(label, box, btn):
        # Expand in place: the preview holds still (the rest flows in below it, the button moves to the end).
        centre(page, btn)
        r = out[f"{label}_expand"] = sampled(page, box, 0, tap(page, btn, 0, left=True))
        c = page.evaluate(CLIP, box)
        verdict(out, f"{label}-show-all-holds-preview", r["max_dy"] <= 1 and not r["end"]["pinned"] and r["end"]["slack"] == 0
                and c["expanded"] and not c["clipped"] and not c["fade"] and c["btn"]["text"] == "Show less"
                and c["btn"]["aria"] == "true", [r, c])
        # Collapse from the END of the expanded body: "Show less" stays under the finger, no slack needed.
        centre(page, btn)
        r = out[f"{label}_collapse"] = sampled(page, btn, 0, tap(page, btn, 0, left=True))
        c = page.evaluate(CLIP, box)
        verdict(out, f"{label}-show-less-holds-button", r["max_dy"] <= 1 and r["end"]["slack"] == 0 and c["clipped"]
                and c["btn"]["text"] == "Show all", [r, c])
    guarded(out, verdict, "thinking-show-all-holds-preview", lambda: anchors("thinking", THINK_BOX, THINK_BTN))
    guarded(out, verdict, "drawer-show-all-holds-preview", lambda: anchors("drawer", DRAWER_BOX, DRAWER_BTN))

    def near_bottom():
        # The drawer is the log's last rows: expand, ride to the very bottom (re-pins), "Show less" there by tap
        # (the press unpins). Holding the button scrolls up by the shrink — never a clamp, so no slack. NOT a
        # keepAt discriminator (--red=keepat leaves it green: at gap 0 the browser's clamp moves the view by
        # exactly the shrink); it pins that nothing else — slack, a re-pin — disturbs the bottom case.
        centre(page, DRAWER_BTN); tap(page, DRAWER_BTN, 0, left=True)(); page.wait_for_timeout(300)
        to_bottom(page)
        r = out["drawer_collapse_bottom"] = sampled(page, DRAWER_BTN, 0, tap(page, DRAWER_BTN, 0, left=True))
        verdict(out, "drawer-show-less-at-bottom-holds-button", r["max_dy"] <= 1 and r["end"]["slack"] == 0
                and r["end"]["gap"] <= 1 and not r["end"]["pinned"], r)
    guarded(out, verdict, "drawer-show-less-at-bottom-holds-button", near_bottom)

    def swap_short_and_back():
        # Expand the long drawer, swap to the SHORT pill (no button, no clip), swap back: starts clipped again.
        centre(page, DRAWER_BTN); tap(page, DRAWER_BTN, 0, left=True)(); page.wait_for_timeout(300)
        centre(page, PILL)
        sw = out["swap_anchor"] = sampled(page, PILL, 1, tap(page, PILL, 1))  # cleric: expanded long -> short, tapped pill holds
        page.wait_for_timeout(400)
        verdict(out, "swap-expanded-to-short-holds-pill", sw["max_dy"] <= 1, sw)
        short = page.evaluate(CLIP, DRAWER_BOX)
        centre(page, PILL); tap(page, PILL, 0)(); page.wait_for_timeout(400)
        back = page.evaluate(CLIP, DRAWER_BOX)
        out["swap"] = [short, back]
        verdict(out, "show-all-hidden-when-short-and-reset-on-swap", not short["clipped"] and not short["fade"]
                and short["btn"] and not short["btn"]["shown"] and back["clipped"] and not back["expanded"]
                and back["btn"]["shown"] and back["btn"]["text"] == "Show all", [short, back])
    guarded(out, verdict, "show-all-hidden-when-short-and-reset-on-swap", swap_short_and_back)
    out["pageerrors"] = errs[:3]
    page.close()
    return out


TAP_ST = """() => ({ pinned: window.__probe.metrics().pinned, pill: !document.querySelector('#chat-jump').hidden })"""


def main(argv: list[str]) -> int:
    args = common.suite_args(argv, lambda ap: ap.add_argument("--red", choices=("slack", "keepat")))
    from playwright.sync_api import sync_playwright
    common.hang_alarm(1500)
    common.build_site(args.webui, patch_keepat=args.red == "keepat")
    httpd, port = common.serve_site()
    rows = common.Rows()
    css = "#chat-log { padding-bottom: 14px !important; }" if args.red == "slack" else None
    try:
        with sync_playwright() as pw:
            for engine, name, vp, cpu in common.devices(args.engines):
                prefix = f"[{engine}/{name}] toggles: "
                print(f"== {engine}/{name} ==", flush=True)
                b = common.launch(pw, engine)

                def verdict(r, n, ok, detail, prefix=prefix):
                    rows.verdict(r, prefix + n, ok, detail)
                try:
                    out = run_toggles(lambda: common.boot(b, port, vp, cpu, css), verdict)
                    print(f"{engine}/{name}", json.dumps(out)[:1500], flush=True)
                except Exception as exc:  # a crashed scenario is a failed one, loudly
                    rows.check(f"{prefix}scenario ran", False, repr(exc)[:300])
                b.close()
    finally:
        httpd.shutdown()
    return rows.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
