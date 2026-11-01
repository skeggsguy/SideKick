#!/opt/homebrew/bin/python3
"""Suite `bgrun` — BK-027 (goal 2): a Fire-now background run watched live,
app hidden mid-run, app shown again. WebKit iPad port.
  A  finished while hidden  -> final answer on screen <= 2 s after `visible`,
     no chat switch, exactly one /messages fetch
  B  still running at resume -> re-rendered at resume (mark kept), healed
     again at run_done
  C  screen on another chat  -> resume fetches nothing of the background run

The "<= 2 s" is the plan's hard target (goal 2), polled every 25 ms; on this
host it heals far inside it (MEASURE line), so the bound has headroom.

Run through run_probe.py (`--only=bgrun`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/bgrun.py [--webui=DIR]
"""
from __future__ import annotations

import json
import sys

import common

ROWS = common.Rows()


def check(name, ok, detail):
    ROWS.check("[webkit/ipad] bgrun: " + name, ok, detail)


JS = """async (scenario) => {
  const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
  const { ui } = await import('/state.js');
  const M = window.__mock, sid = 'bg-prop', other = 'other-chat', out = { scenario };
  M.store[other] = { messages: [{ role: 'user', text: 'hi' }, { role: 'assistant', text: 'OTHER CHAT', tool_calls: [], tool_results: [], content_blocks: [{ type: 'text', text: 'OTHER CHAT' }] }], title: 'Other' };
  M.store[sid] = { messages: [{ role: 'user', text: 'Run the weekly digest' }], title: 'Weekly Digest Job' };
  const vis = (v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
                       Object.defineProperty(document, 'hidden', { configurable: true, get: () => v === 'hidden' });
                       document.dispatchEvent(new Event('visibilitychange')); };
  const text = () => document.querySelector('#chat-log').innerText.replace(/\\s+/g, ' ').slice(-160);
  const d = (ev) => window.__sk.handleRunEvent(Object.assign({ session_id: sid }, ev)); // background events: no run_id, no seq
  const fetches = () => M.calls.filter((c) => c === '/api/sessions/' + sid + '/messages').length;
  const waitFor = async (needle, ms) => { const t0 = performance.now();
    while (performance.now() - t0 < ms) { if (text().includes(needle)) return Math.round(performance.now() - t0); await sleep(25); }
    return null; };
  ui.expecting = true;                       // the Schedules "Fire now" path
  d({ type: 'run_start', title: 'Weekly Digest Job' });
  await sleep(600);
  d({ type: 'thinking_delta', text: 'Checking the feeds…' });
  d({ type: 'tool_call', tool: 'web_search', args: { q: 'weekly digest' } });
  await sleep(300);
  out.on_screen = ui.sessionId; out.mid_run = text();
  if (scenario === 'C') { document.dispatchEvent(new CustomEvent('open-session', { detail: other })); await sleep(800); }
  vis('hidden');                              // iPad sleeps: socket closed, run_done never arrives
  if (scenario !== 'B') M._persistAnswer(sid, [{ type: 'delta', text: 'FINAL: 1 new item + key corrections' }], 'Weekly Digest Job');
  await sleep(300);
  const before = fetches();
  vis('visible');                             // morning: app foregrounded
  out.final_ms = scenario === 'A' ? await waitFor('FINAL:', 2000) : null;
  await sleep(scenario === 'A' ? 300 : 1500);
  out.resume_fetches = fetches() - before;
  out.after_foreground = text();
  out.mark_at_resume = (ui.bgLive || {})[sid] ? { ...ui.bgLive[sid] } : null;
  if (scenario === 'B') {                     // the run keeps going after the resume render, then finishes
    d({ type: 'delta', text: 'FINAL: streamed live after resume' });
    await sleep(800);
    out.live_tail = text();
    M._persistAnswer(sid, [{ type: 'delta', text: 'FINAL: 1 new item + key corrections' }], 'Weekly Digest Job');
    const b2 = fetches();
    d({ type: 'run_done', text: 'FINAL' });
    out.heal_ms = await waitFor('key corrections', 2000);
    await sleep(300);
    out.run_done_fetches = fetches() - b2;
    out.after_run_done = text();
  }
  out.bg_mark = (ui.bgLive || {})[sid] || null;
  out.screen = ui.sessionId;
  return out; }"""


def scenarios(b, port):
    for sc in ("A", "B", "C"):
        page, errs = common.boot(b, port, common.IPAD, None)
        o = page.evaluate(JS, sc); page.close()
        print(f"--- scenario {sc}:", json.dumps(o, ensure_ascii=False))
        check(f"{sc}: no page errors", not errs, errs[:3])
        if sc == "A":
            ROWS.measure("[webkit/ipad] bgrun: A resume-to-final ms", o["final_ms"])
            check("A: final answer on screen <= 2 s after visible, no chat switch",
                  o["final_ms"] is not None and o["final_ms"] <= 2000 and o["screen"] == "bg-prop", o["final_ms"])
            check("A: exactly ONE /messages fetch on resume", o["resume_fetches"] == 1, o["resume_fetches"])
        if sc == "B":
            check("B: the resume re-renders the still-running chat once", o["resume_fetches"] == 1, o["resume_fetches"])
            check("B: the mark stays past the resume (run not done), carrying the hole; live events still paint",
                  o["mark_at_resume"] == {"gap": True} and "FINAL: streamed live" in o["live_tail"],
                  [o["mark_at_resume"], o["live_tail"][-40:]])
            check("B: run_done clears the mark", o["bg_mark"] is None, o["bg_mark"])
            check("B: run_done heals from the store (one fetch, persisted answer on screen <= 2 s)",
                  o["run_done_fetches"] == 1 and o["heal_ms"] is not None and "streamed live" not in o["after_run_done"],
                  [o["run_done_fetches"], o["heal_ms"], o["after_run_done"][-60:]])
        if sc == "C":
            check("C: screen on another chat -> no fetch of the background run on resume",
                  o["resume_fetches"] == 0 and o["screen"] == "other-chat", [o["resume_fetches"], o["screen"]])
            check("C: the mark stays for that run (opened later, its run_done heals)", o["bg_mark"] == {"gap": True}, o["bg_mark"])


def main(argv: list[str]) -> int:
    args = common.suite_args(argv)
    from playwright.sync_api import sync_playwright
    common.hang_alarm(300)
    common.build_site(args.webui)
    h, port = common.serve_site()
    try:
        with sync_playwright() as pw:
            b = pw.webkit.launch()
            try:
                scenarios(b, port)
            except Exception as exc:  # a crashed scenario is a failed one, loudly
                check("scenario ran", False, repr(exc)[:300])
            b.close()
    finally:
        h.shutdown()
    return ROWS.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
