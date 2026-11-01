#!/opt/homebrew/bin/python3
"""Suite `orphan` — BK-032 (plan step 7): one /api/events stream per page across sleep/wake.

The REAL web UI on orphan_server.py (the real create_app, ephemeral port, never
8787). The server-side stream count is `broadcaster.subscriber_count`
(/probe/subs) — one page, so it is the page's count, the number agentd.log's
`[events] page … while N live` lines report. Every `window.EventSource` the app
builds goes through a thin init-script wrapper over the engine's REAL
EventSource: it records each instance's `n`, and counts which instances deliver
a published `probe_ping` mark into the app's onmessage.

Rows (each run on every engine):
  cycles      hidden/visible x6 — the app's only close/reopen path
  sleep       a sleep = every stream cut server-side (/probe/drop) + hidden,
              then visible; also a cut while hidden and a cut while visible (an
              SSE error: the engine's own 3 s auto-reconnect)
  triggers    several resume triggers at once: visibilitychange x2 + pageshow
              (persisted) + focus + online, and a hidden/visible burst
  resurrect   THE iPad's shape (agentd.log, page 51db055b: n=1 re-opens 2.9 s
              after n=2, then n=6/n=7/n=1 together ~3 s after each new n). The
              rows above cannot make desktop WebKit do it — a close()d instance
              stays closed there (the step-7 scratch experiment: close in onerror, in the
              reconnect wait, before headers, offline: 0 reconnects) and
              Playwright WebKit never BFCaches (pageshow persisted=false). So
              this row injects the observed engine behaviour: a close() made
              while the page is suspending (the hidden handler during a sleep)
              comes back 3 s after wake — same object, same URL, same handlers —
              as a real EventSource on a real socket. What it tests is the APP's
              answer to an instance that returns after close().
Pass = after every cycle settles, exactly 1 live stream server-side, and a
published mark reaches the app exactly once, from the current instance.
Exit 1 on any failed check.
Run through run_probe.py (`--only=orphan`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/orphan.py [--engines=webkit] [--webui=DIR] [--row=ROW]
(--webui serves another copy of the web UI: the red-before run on an older tree.)"""
from __future__ import annotations

import json
import sys
import urllib.request

import common

ROWS_OUT = common.Rows()
BASE = ""


def check(name, ok, detail=None):
    ROWS_OUT.check(name, ok, detail)


def get(path):  # GETs only, and only against the probe server
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.load(r)
def subs(): return get("probe/subs")["subs"]

WRAP = r"""(() => {
  const Real = window.EventSource;
  const P = window.__probe = { suspended: false, model: false, all: [], got: {}, resurrected: [] };
  class Probed {
    constructor(url) {
      this.url = url; this.onopen = this.onmessage = this.onerror = null;
      this.n = Number((/[?&]n=(\d+)/.exec(url) || [])[1]);
      this._zombie = false; P.all.push(this); this._wire();
    }
    _wire() {
      const r = this._inner = new Real(this.url);
      r.onopen = (e) => this.onopen && this.onopen(e);
      r.onerror = (e) => this.onerror && this.onerror(e);
      r.onmessage = (e) => {
        let m = null; try { m = JSON.parse(e.data).mark || null; } catch {}
        if (m) (P.got[m] = P.got[m] || []).push({ n: this.n, open: r.readyState === 1 });
        if (this.onmessage) this.onmessage(e);
      };
    }
    get readyState() { return this._inner.readyState; }
    close() { this._inner.close(); if (P.model && P.suspended) this._zombie = true; }
  }
  Probed.CONNECTING = 0; Probed.OPEN = 1; Probed.CLOSED = 2;
  window.EventSource = Probed;
  // The resurrection (resurrect row only): every instance close()d while
  // suspended reconnects 3 s after wake (EventSource's default retry) — the
  // same object with the app's handlers, on a fresh real EventSource.
  P.wake = () => {
    P.suspended = false;
    for (const s of P.all) if (s._zombie) {
      s._zombie = false;
      setTimeout(() => { P.resurrected.push(s.n); s._wire(); }, 3000);
    }
  };
  // Instances whose real socket is not CLOSED (client-side view).
  P.live = () => P.all.filter((s) => s._inner.readyState !== 2).map((s) => s.n);
})();"""

def vis(p, v):
    p.evaluate("""(v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => v === 'hidden' });
      document.dispatchEvent(new Event('visibilitychange')); }""", v)

def page(b):
    # service_workers="block": once /sw.js controls the page, headless WebKit
    # never answers page.evaluate (learnings 2026-10-31). The worker passes
    # /api/* through anyway, so it is not under test here.
    ctx = b.new_context(viewport={"width": 820, "height": 1180}, service_workers="block")
    ctx.add_init_script(WRAP)
    p = ctx.new_page(); errs = []
    p.on("pageerror", lambda e: errs.append(str(e)))
    p.goto(BASE); p.wait_for_selector("#chat-send"); p.wait_for_timeout(800)
    return ctx, p, errs

marks = iter(range(1, 10**6))
def settle_and_count(eng, row, p, label):
    """One cycle's verdict: the server's live count, then one published mark."""
    p.wait_for_timeout(4500)  # past the 3 s retry/resurrection + slack
    s = subs(); m = f"{row}-{next(marks)}"
    get(f"probe/publish?mark={m}"); p.wait_for_timeout(600)
    got = p.evaluate("(m) => window.__probe.got[m] || []", m)
    held = p.evaluate("window.__probe.all.length ? Math.max(...window.__probe.all.map((s) => s.n)) : 0")
    ok = s == 1 and len(got) == 1 and got[0]["n"] == held
    check(f"[{eng}] {row}: {label} -> 1 live stream, mark delivered once by the current n", ok,
          {"subs": s, "mark_from_n": [g["n"] for g in got], "current_n": held,
           "client_live_n": p.evaluate("window.__probe.live()")})
    return s

def row_cycles(eng, b):
    ctx, p, errs = page(b)
    check(f"[{eng}] cycles: boot holds 1 stream", subs() == 1, subs())
    for i in range(6):
        vis(p, "hidden"); p.wait_for_timeout(300 if i % 2 else 1200); vis(p, "visible")
    settle_and_count(eng, "cycles", p, "6 hidden/visible cycles")
    check(f"[{eng}] cycles: no page errors", not errs, errs[:3]); ctx.close()

def row_sleep(eng, b):
    ctx, p, errs = page(b)
    for i in range(3):  # cut then hidden (the socket dies first), then wake
        get("probe/drop"); p.wait_for_timeout(100); vis(p, "hidden"); p.wait_for_timeout(1500); vis(p, "visible")
        settle_and_count(eng, "sleep", p, f"cut+hidden+visible #{i + 1}")
    vis(p, "hidden"); p.wait_for_timeout(200); get("probe/drop"); p.wait_for_timeout(4000); vis(p, "visible")
    settle_and_count(eng, "sleep", p, "cut while hidden, hidden past the 3 s retry")
    get("probe/drop")  # an SSE error while visible: the engine's own auto-reconnect
    settle_and_count(eng, "sleep", p, "cut while visible (native reconnect)")
    get("probe/drop"); p.wait_for_timeout(1000); vis(p, "hidden"); p.wait_for_timeout(300); vis(p, "visible")
    settle_and_count(eng, "sleep", p, "hidden+visible inside the reconnect wait")
    check(f"[{eng}] sleep: no page errors", not errs, errs[:3]); ctx.close()

def row_triggers(eng, b):
    ctx, p, errs = page(b)
    vis(p, "hidden"); p.wait_for_timeout(500)
    p.evaluate("""() => {
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
      document.dispatchEvent(new Event('visibilitychange'));
      window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }));
      window.dispatchEvent(new Event('focus'));
      window.dispatchEvent(new Event('online'));
      document.dispatchEvent(new Event('visibilitychange'));
    }""")
    settle_and_count(eng, "triggers", p, "visibilitychange x2 + pageshow(persisted) + focus + online")
    p.evaluate("""() => { const set = (v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
        document.dispatchEvent(new Event('visibilitychange')); };
      for (let i = 0; i < 5; i++) { set('hidden'); set('visible'); } }""")
    settle_and_count(eng, "triggers", p, "5 hidden/visible flips in one task")
    check(f"[{eng}] triggers: no page errors", not errs, errs[:3]); ctx.close()

def row_resurrect(eng, b):
    ctx, p, errs = page(b)
    p.evaluate("window.__probe.model = true")
    counts = []
    for i in range(4):  # a sleep: suspending, socket cut, hidden (the app closes), wake
        p.evaluate("window.__probe.suspended = true"); get("probe/drop"); p.wait_for_timeout(100)
        vis(p, "hidden"); p.wait_for_timeout(1200)
        vis(p, "visible"); p.evaluate("window.__probe.wake()")
        counts.append(settle_and_count(eng, "resurrect", p, f"sleep/wake #{i + 1}"))
    res = p.evaluate("window.__probe.resurrected")
    check(f"[{eng}] resurrect: the model really resurrected closed instances", len(res) >= 4, res)
    check(f"[{eng}] resurrect: no page errors", not errs, errs[:3])
    ROWS_OUT.measure(f"[{eng}] orphan: resurrect live streams per cycle", counts)
    ctx.close()

ROWS = {"cycles": row_cycles, "sleep": row_sleep, "triggers": row_triggers, "resurrect": row_resurrect}


def main(argv: list[str]) -> int:
    global BASE
    args = common.suite_args(argv, lambda ap: ap.add_argument("--row", choices=sorted(ROWS)))
    from playwright.sync_api import sync_playwright
    common.hang_alarm(600)
    srv, BASE, errlog = common.spawn_server("orphan_server.py", args.webui, "orphan_server.stderr.txt")
    try:
        with sync_playwright() as pw:
            for eng in args.engines:
                print(f"===== {eng} =====", flush=True)
                b = getattr(pw, eng).launch()
                for name, fn in ROWS.items():
                    if args.row and name != args.row:
                        continue
                    try:
                        fn(eng, b)
                    except Exception as e:  # a crashed row is a failed one, loudly
                        check(f"[{eng}] {name}: row ran", False, repr(e)[:300])
                b.close()
    finally:
        common.stop_server(srv, errlog)
    return ROWS_OUT.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
