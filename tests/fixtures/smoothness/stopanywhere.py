#!/opt/homebrew/bin/python3
"""Suite `stopanywhere` — BK-034 (plan step 2b): Stop on every device, against the REAL gateway.

Two browser contexts = two devices (separate storage, separate /api/events
sockets) on stopanywhere_server.py — the real create_app with a slow fake
agent on an ephemeral port (never agentd's 8787). Scenarios:
  stop      A sends; B opens the chat mid-run -> Stop; B's view keeps growing
            (A is ATTACHED: nothing reaches /api/events); B switches away ->
            Send, back -> Stop; B's Stop ends the run -> BOTH Send, A shows
            "Stopped", no reload on either page.
  finish    A sends a short run; B observes -> Stop; the run ends normally ->
            both Send, B shows the final text, no reload.
  gone      A's page closes mid-run (the server detaches: events reroute to
            /api/events); B's Stop ends it -> B Send.
  hidden    B hidden mid-run then shown: still Stop, caught up; B's Stop ends
            it -> both Send.
Exit 1 on any failed check.
Run through run_probe.py (`--only=stopanywhere`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/stopanywhere.py [--engines=webkit] [--webui=DIR] [--scenario=NAME]
(--webui serves another copy of the web UI: the red-before run on an older tree.)"""
from __future__ import annotations

import json
import os
import re
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
def last_w(p):
    # MAX, not last: the paced reveal can be mid-word ("w1" of "w12") at the tail
    ws = re.findall(r"\bw(\d+)\b", log(p)); return max(map(int, ws)) if ws else -1
def open_chat(p, s): p.evaluate("(s) => document.dispatchEvent(new CustomEvent('open-session', { detail: s }))", s)
def vis(p, v):
    p.evaluate("""(v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => v === 'hidden' });
      document.dispatchEvent(new Event('visibilitychange')); }""", v)
def not_reloaded(p): return p.evaluate("window.__probeMark === 1")

def device(b):
    # service_workers="block": the app registers /sw.js, and once a worker
    # controls the page headless WebKit never answers page.evaluate (it hangs,
    # no error). The worker is not under test here.
    ctx = b.new_context(viewport={"width": 390, "height": 844}, service_workers="block")
    p = ctx.new_page(); errs = []
    p.on("pageerror", lambda e: errs.append(str(e)))
    p.on("console", lambda m: m.type == "error" and errs.append("console: " + m.text))
    p.goto(BASE); p.wait_for_selector("#chat-send"); p.wait_for_timeout(400)
    p.evaluate("window.__probeMark = 1")
    return ctx, p, errs

def send(p, text):
    p.fill("#chat-input", text); p.click("#chat-send")

def start_run(eng, A, text):
    send(A, text)
    check(f"[{eng}] A's own send shows Stop", wait_mode(A, "stop", 3000) is not None)
    for _ in range(40):
        s = sid(A)
        if s and live(s): return s
        time.sleep(0.1)
    check(f"[{eng}] run is live on the server", False); return sid(A)

def sc_stop(eng, b):
    cA, A, eA = device(b); cB, B, eB = device(b)
    send(B, "fast warmup")                         # B's own other chat, to switch to
    wait_mode(B, "stop", 3000); check(f"[{eng}] stop: B's warmup run finishes", wait_mode(B, "send", 10000) is not None)
    other = sid(B)
    s = start_run(eng, A, "a long one")
    time.sleep(1.0)
    open_chat(B, s)
    t = wait_mode(B, "stop", 3000)
    check(f"[{eng}] stop: B opens the live chat mid-run -> Stop", t is not None, {"ms": t})
    if os.environ.get("PROBE_TRACE"):              # what B's log shows, twice a second
        for _ in range(14):
            print("  trace B:", log(B)[-110:], flush=True); time.sleep(0.5)
    B.wait_for_timeout(1000)                       # the join's paced reveal settles
    w0 = last_w(B); time.sleep(4.5); w1 = last_w(B)
    # ~45 ticks stream meanwhile; the 2 s watch delivers them in lumps
    check(f"[{eng}] stop: B's view keeps growing while A is attached (observe watch)", w0 >= 0 and w1 >= w0 + 20, {"before": w0, "after": w1})
    open_chat(B, other)
    t = wait_mode(B, "send", 2000)
    check(f"[{eng}] stop: B switches to another chat -> Send", t is not None and sid(B) == other, {"ms": t})
    open_chat(B, s)
    t = wait_mode(B, "stop", 3000)
    check(f"[{eng}] stop: B switches back mid-run -> Stop", t is not None, {"ms": t})
    B.click("#chat-send")
    tb = wait_mode(B, "send", 5000); ta = wait_mode(A, "send", 5000)
    check(f"[{eng}] stop: B's Stop -> B back to Send", tb is not None, {"ms": tb})
    check(f"[{eng}] stop: B's Stop -> A (the sender) back to Send", ta is not None, {"ms": ta})
    check(f"[{eng}] stop: the run really ended on the server", live(s) is False)
    check(f"[{eng}] stop: A shows Stopped", "Stopped" in log(A), log(A)[-120:])
    time.sleep(2.5)                                # a watch tick after the end must not flip anything back
    check(f"[{eng}] stop: both still Send a tick later", mode(A) == "send" and mode(B) == "send", [mode(A), mode(B)])
    check(f"[{eng}] stop: B kept the streamed partial", last_w(B) >= w1, last_w(B))
    check(f"[{eng}] stop: no reload on either page", not_reloaded(A) and not_reloaded(B))
    check(f"[{eng}] stop: no page errors", not eA and not eB, (eA + eB)[:3])
    cA.close(); cB.close()

def sc_finish(eng, b):
    cA, A, eA = device(b); cB, B, eB = device(b)
    s = start_run(eng, A, "fast answer please")
    time.sleep(0.8)
    open_chat(B, s)
    t = wait_mode(B, "stop", 3000)
    check(f"[{eng}] finish: B observes -> Stop", t is not None, {"ms": t})
    tb = wait_mode(B, "send", 10000); ta = wait_mode(A, "send", 10000)
    check(f"[{eng}] finish: normal end -> B Send", tb is not None, {"ms": tb})
    check(f"[{eng}] finish: normal end -> A Send", ta is not None, {"ms": ta})
    B.wait_for_timeout(600)
    check(f"[{eng}] finish: B shows the final text", "END." in log(B), log(B)[-80:])
    check(f"[{eng}] finish: no reload; no page errors", not_reloaded(B) and not eA and not eB, (eA + eB)[:3])
    cA.close(); cB.close()

def sc_gone(eng, b):
    cA, A, eA = device(b); cB, B, eB = device(b)
    s = start_run(eng, A, "a long one")
    time.sleep(0.8)
    open_chat(B, s)
    check(f"[{eng}] gone: B observes -> Stop", wait_mode(B, "stop", 3000) is not None)
    cA.close()                                     # sender gone: the server detaches, events reroute
    time.sleep(1.5)
    check(f"[{eng}] gone: run survives the sender", live(s) is True)
    B.click("#chat-send")
    t = wait_mode(B, "send", 5000)
    check(f"[{eng}] gone: B's Stop -> B Send", t is not None, {"ms": t})
    check(f"[{eng}] gone: the run really ended", live(s) is False)
    check(f"[{eng}] gone: no reload; no page errors", not_reloaded(B) and not eB, eB[:3])
    cB.close()

def sc_hidden(eng, b):
    cA, A, eA = device(b); cB, B, eB = device(b)
    s = start_run(eng, A, "a long one")
    time.sleep(0.8)
    open_chat(B, s)
    check(f"[{eng}] hidden: B observes -> Stop", wait_mode(B, "stop", 3000) is not None)
    B.wait_for_timeout(500); w0 = last_w(B)
    vis(B, "hidden"); time.sleep(4.0); vis(B, "visible")
    B.wait_for_timeout(300)
    check(f"[{eng}] hidden: still Stop after resume", mode(B) == "stop", mode(B))
    B.wait_for_timeout(2700)
    check(f"[{eng}] hidden: caught up after resume", last_w(B) >= w0 + 30, {"before": w0, "after": last_w(B)})
    B.click("#chat-send")
    tb = wait_mode(B, "send", 5000); ta = wait_mode(A, "send", 5000)
    check(f"[{eng}] hidden: B's Stop -> both Send", tb is not None and ta is not None, {"B": tb, "A": ta})
    check(f"[{eng}] hidden: no reload; no page errors", not_reloaded(B) and not eA and not eB, (eA + eB)[:3])
    cA.close(); cB.close()

SCENARIOS = (("stop", sc_stop), ("finish", sc_finish), ("gone", sc_gone), ("hidden", sc_hidden))


def main(argv: list[str]) -> int:
    global BASE, PwTimeout
    args = common.suite_args(argv, lambda ap: ap.add_argument("--scenario"))
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright
    common.hang_alarm(600)
    srv, BASE, errlog = common.spawn_server("stopanywhere_server.py", args.webui, "stopanywhere_server.stderr.txt")
    try:
        with sync_playwright() as pw:
            for eng in args.engines:
                b = getattr(pw, eng).launch()
                for name, fn in SCENARIOS:
                    if args.scenario and name != args.scenario:
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
