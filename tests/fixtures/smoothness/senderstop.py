#!/opt/homebrew/bin/python3
"""Suite `senderstop` — BK-036 (plan step 2c): the SENDER's own Stop must stay while the server's /live is active.

One browser context = the iPhone that sent the turn, on stopanywhere_server.py (the real create_app
+ a fake agent, ephemeral port, never agentd's 8787). The "tool" run is shaped like the property
chat's browser_delegate stretch: ~1.5 s of text, a tool_call, 14 s of SILENCE (pings only), the
tool_result, ~3 s more text. Every scenario faults the sender mid-tool-call, then samples the
composer every 200 ms for 6 s while /live says active (any non-Stop sample is a FAIL), then
checks the run still ends cleanly on the page (final text, Send).

Transport faults are injected by wrapping window.fetch for POST /api/chat (init script): the app's
reader gets a proxied body, and __probeCut(how) aborts the REAL request (so the server sees the
disconnect and detaches, as on a phone) and then either CLOSES the proxied body (how="eof": the
reader sees done:true, no terminal event — a clean end, as a proxy or a lenient HTTP stack can
deliver) or ERRORS it (how="error": the reader throws, as "Load failed" does).

Scenarios (candidate letters = the plan's step-2c list):
  eof           (c) the chat stream ends CLEANLY mid-tool-call, page visible
  eof-hidden    (c) the same while hidden (in another app approving), then visible again
  error         (c) the stream THROWS mid-tool-call (Load failed / the 45 s stall abort)
  error-hidden  (c) the same while hidden, then visible
  eof-stop      (c) after the clean end, the sender's Stop still ends the run
  push-approvals (a) an approval push tap on the open page: sw.js posts {url: "/#approvals"}
  push-same     (a) a push / approvals-history "Open chat" naming the SAME chat (openSession of the
                live session while the stream is attached), then hidden + a stream error + visible
  reload        (b) the page is killed and relaunched mid-tool-call (cold restore of the chat)
Exit 1 on any failed check.
Run through run_probe.py (`--only=senderstop`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/senderstop.py [--engines=webkit] [--webui=DIR] [--scenario=NAME[,NAME]]"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

import common

ROWS = common.Rows()
BASE = ""
PwTimeout = TimeoutError  # rebound to Playwright's TimeoutError in main()


def check(name, ok, detail=None):
    ROWS.check(name, ok, detail)


def live(sid):  # GET only, and only against the probe server
    with urllib.request.urlopen(f"{BASE}api/chat/{sid}/live", timeout=5) as r:
        return json.load(r).get("active")

# The transport fault. Only the TRANSPORT is wrapped — app.js's own reader, catch and stall
# detector run unmodified on the proxied body.
CUT_JS = r"""(() => {
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (!(init && init.method === "POST" && url.pathname === "/api/chat")) return real(input, init);
    const inner = new AbortController();
    if (init.signal) init.signal.addEventListener("abort", () => inner.abort(init.signal.reason));
    const resp = await real(input, { ...init, signal: inner.signal });
    if (!resp.ok || !resp.body) return resp;
    const src = resp.body.getReader();
    let ctl, cut = false;
    const body = new ReadableStream({
      start(c) { ctl = c; },
      async pull(c) {
        try { const { done, value } = await src.read(); if (cut) return; done ? c.close() : c.enqueue(value); }
        catch (e) { if (!cut) c.error(e); }
      },
      cancel() { inner.abort(); },
    });
    window.__probeCut = (how) => {
      cut = true;
      if (how === "eof") ctl.close(); else ctl.error(new TypeError("Load failed"));
      inner.abort(); // the server sees the disconnect and DETACHES (events reroute to /api/events)
    };
    return new Response(body, { status: resp.status, headers: resp.headers });
  };
})();"""

MODE = "(document.querySelector('#chat-send').dataset.mode || 'send')"
def mode(p): return p.evaluate(MODE)
def wait_mode(p, m, ms):
    t0 = time.monotonic()
    try:
        p.wait_for_function(f"(m) => {MODE} === m", arg=m, timeout=ms, polling=50)
        return round((time.monotonic() - t0) * 1000)
    except PwTimeout:
        return None
def sid(p): return p.evaluate("import('/state.js').then((m) => m.ui.sessionId)")
def log(p): return p.evaluate("document.querySelector('#chat-log').innerText.replace(/\\s+/g, ' ')")
def vis(p, v):
    p.evaluate("""(v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => v === 'hidden' });
      document.dispatchEvent(new Event('visibilitychange')); }""", v)
def sw_message(p, url):  # what sw.js notificationclick posts to an already-open page
    p.evaluate("(url) => navigator.serviceWorker.dispatchEvent(new MessageEvent('message', { data: { url } }))", url)

def device(b):
    # service_workers="block": headless WebKit hangs page.evaluate once /sw.js controls the page
    # (learnings 2026-10-31); the worker's postMessage is simulated by sw_message() instead.
    ctx = b.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    ctx.add_init_script(CUT_JS)
    p = ctx.new_page(); errs = []
    p.on("pageerror", lambda e: errs.append(str(e)))
    p.on("console", lambda m: m.type == "error" and errs.append("console: " + m.text))
    p.goto(BASE); p.wait_for_selector("#chat-send"); p.wait_for_timeout(400)
    return ctx, p, errs

def start_tool_run(eng, name, A):
    A.fill("#chat-input", "a tool run"); A.click("#chat-send")
    check(f"[{eng}] {name}: A's own send shows Stop", wait_mode(A, "stop", 3000) is not None)
    A.wait_for_selector(".pill.running", timeout=8000)  # the long tool is in flight
    A.wait_for_timeout(700)
    s = sid(A)
    check(f"[{eng}] {name}: the run is live, mid-tool-call", bool(s) and live(s) is True)
    return s

def hold(eng, name, A, s, secs=6.0):
    """Sample the composer every 200 ms while /live is active: every sample must read Stop."""
    bad, n, t0 = [], 0, time.monotonic()
    while time.monotonic() - t0 < secs:
        m = mode(A)
        if live(s) is not True: break
        n += 1
        if m != "stop": bad.append(round(time.monotonic() - t0, 1))
        time.sleep(0.2)
    check(f"[{eng}] {name}: Stop held on the sender while /live is active", n >= 10 and not bad,
          {"samples": n, "send_at_s": bad[:8]})

def finish(eng, name, A, s, errs):
    t0 = time.monotonic()
    while live(s) is True and time.monotonic() - t0 < 30: time.sleep(0.2)
    ended = live(s) is False
    t = wait_mode(A, "send", 3000)  # after the server says ended (an EARLY Send is hold()'s to catch)
    check(f"[{eng}] {name}: the run's end hands the sender back to Send", ended and t is not None, {"ms_after_end": t})
    A.wait_for_timeout(800)
    check(f"[{eng}] {name}: the sender shows the final text", "END." in log(A), log(A)[-90:])
    check(f"[{eng}] {name}: no page errors", not errs, errs[:3])

def stop_it(eng, name, A, s, errs):
    A.click("#chat-send")
    t = wait_mode(A, "send", 6000)
    check(f"[{eng}] {name}: the sender's Stop ends the run -> Send", t is not None and live(s) is False, {"ms": t})
    check(f"[{eng}] {name}: no page errors", not errs, errs[:3])

def sc_cut(how, hidden, then):
    def run(eng, b):
        name = how + ("-hidden" if hidden else "") + ("-stop" if then == "stop" else "")
        c, A, errs = device(b)
        s = start_tool_run(eng, name, A)
        if hidden: vis(A, "hidden"); A.wait_for_timeout(300)
        A.evaluate("(how) => window.__probeCut(how)", how)
        if hidden: A.wait_for_timeout(2000); vis(A, "visible")
        A.wait_for_timeout(300)
        hold(eng, name, A, s)
        (stop_it if then == "stop" else finish)(eng, name, A, s, errs)
        c.close()
    return run

def sc_push_approvals(eng, b):
    name = "push-approvals"
    c, A, errs = device(b)
    s = start_tool_run(eng, name, A)
    sw_message(A, "/#approvals")                   # the approval push's url (gateway/agentd.py)
    A.wait_for_timeout(300)
    check(f"[{eng}] {name}: the chat stays on screen", sid(A) == s)
    hold(eng, name, A, s)
    finish(eng, name, A, s, errs)
    c.close()

def sc_push_same(eng, b):
    name = "push-same"
    c, A, errs = device(b)
    s = start_tool_run(eng, name, A)
    sw_message(A, f"/#session={s}")                # openSession(<the live chat>) while attached
    A.wait_for_timeout(1200)
    hold(eng, name + " (after the same-chat open)", A, s, 3.0)
    vis(A, "hidden"); A.wait_for_timeout(300)
    A.evaluate("() => window.__probeCut('error')")
    A.wait_for_timeout(1500); vis(A, "visible"); A.wait_for_timeout(300)
    hold(eng, name + " (after hidden + stream error)", A, s, 4.0)
    stop_it(eng, name, A, s, errs)
    c.close()

def sc_reload(eng, b):
    name = "reload"
    c, A, errs = device(b)
    s = start_tool_run(eng, name, A)
    A.reload(); A.wait_for_selector("#chat-send")  # iOS killed the PWA; relaunch restores the chat
    errs.clear()  # WebKit logs the torn-down page's in-flight fetches as "access control" errors
    t = wait_mode(A, "stop", 4000)
    check(f"[{eng}] {name}: the relaunched page shows Stop for its own run", t is not None and sid(A) == s, {"ms": t})
    hold(eng, name, A, s, 4.0)
    stop_it(eng, name, A, s, errs)
    c.close()

SCENARIOS = (("eof", sc_cut("eof", False, "finish")), ("eof-hidden", sc_cut("eof", True, "finish")),
             ("error", sc_cut("error", False, "finish")), ("error-hidden", sc_cut("error", True, "finish")),
             ("eof-stop", sc_cut("eof", False, "stop")), ("push-approvals", sc_push_approvals),
             ("push-same", sc_push_same), ("reload", sc_reload))


def main(argv: list[str]) -> int:
    global BASE, PwTimeout
    args = common.suite_args(argv, lambda ap: ap.add_argument("--scenario"))
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright
    common.hang_alarm(900)
    srv, BASE, errlog = common.spawn_server("stopanywhere_server.py", args.webui, "senderstop_server.stderr.txt")
    try:
        with sync_playwright() as pw:
            for eng in args.engines:
                b = getattr(pw, eng).launch()
                for name, fn in SCENARIOS:
                    if args.scenario and name not in args.scenario.split(","):
                        continue
                    print(f"== {eng} {name} ==", flush=True)
                    try:
                        fn(eng, b)
                    except Exception as e:  # a crashed scenario is a failed one, loudly
                        check(f"[{eng}] {name}: scenario ran", False, repr(e)[:300])
                b.close()
    finally:
        common.stop_server(srv, errlog)
    return ROWS.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
