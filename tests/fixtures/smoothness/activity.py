#!/opt/homebrew/bin/python3
"""Suite `activity` — BK-041: the activity row says what the run is doing,
on the real webui against the mocked /api/* (scroll_follow's mock_fetch.js).
WebKit, iPad + iPhone ports.

  reopen-bg     a cron run 95 s in, mid web search: opening its chat shows the
                row AT ONCE with "Searching the web…" and a timer >= 1:35 (G4)
  reopen-chat   a chat run (another device's, or this page's latched one)
                composing a web_search call: "Preparing web search…", timer
                >= 1:35 (G4: /messages `live` carries started_ms + phase)
  approval      a pending approval for the on-screen chat → "Waiting for your
                approval…" within one poll; cleared → the tool's label is back (G1)
  no-working    no label anywhere reads the old catch-all "Working…" (G3)
  cron-prompt   a cron run's stored prompt ("[Scheduled run · …]") renders as
                a user turn, with or without a leading memory block (2026-11-01)

Run through run_probe.py (`--only=activity`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/activity.py [--webui=DIR]
"""
from __future__ import annotations

import json
import sys

import common

ROWS = common.Rows()

JS = """async (scenario) => {
  const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
  const M = window.__mock, sid = 'act-' + scenario, out = { scenario };
  const started_ms = Date.now() - 95000;
  const row = () => { const r = document.querySelector('#chat-status');
    return { hidden: r.hidden, label: document.querySelector('#chat-status-label').textContent,
             timer: document.querySelector('#chat-status-timer').textContent }; };
  const secs = (t) => { const m = /^(\\d+):(\\d\\d)$/.exec(t || ''); return m ? 60 * +m[1] + +m[2] : -1; };
  const rec = { messages: [{ role: 'user', text: 'Find me a recipe' }], title: 'Activity ' + scenario };
  if (scenario === 'cron-prompt') {
    const head = '[Scheduled run · Saturday 2026-11-01 08:00 UTC]\\n\\n';
    for (const [k, text] of [['plain', head + 'Check the feeds'],
                             ['memory', '<memory>\\nfact\\n</memory>\\n' + head + 'Check the feeds']]) {
      M.store[sid + k] = { messages: [{ role: 'user', text }], title: 'Cron ' + k };
      document.dispatchEvent(new CustomEvent('open-session', { detail: sid + k }));
      await sleep(500);
      out[k] = [...document.querySelectorAll('#chat-log .msg.user')].map((e) => e.textContent);
    }
    return out;
  }
  if (scenario === 'reopen-chat') {
    rec.live = { since: 0, seq: 0, oldest_seq: null, gap: false, events: [],
                 started_ms, phase: { type: 'tool_start', tool: 'web_search' } };
    rec.bg_live = null;
  } else {
    rec.bg_live = { started_ms, phase: { type: 'tool_call', tool: 'web_search' } };
  }
  M.store[sid] = rec;
  document.dispatchEvent(new CustomEvent('open-session', { detail: sid }));
  await sleep(700);  // inside the observe watch's first 2 s tick (the mock has no ring)
  out.joined = row(); out.joined_secs = secs(out.joined.timer);
  if (scenario === 'approval') {
    const { loadApprovals } = await import('/approvals.js');
    M.approvals = [{ id: 'ap-1', session_id: sid, tool: 'web_fetch', classification: 'READ_ONLY',
                     args: { url: 'https://example.com' }, created_at: new Date().toISOString() }];
    await loadApprovals();
    out.pending = row();
    M.approvals = [];
    await loadApprovals();
    out.cleared = row();
  }
  return out; }"""


def check(port, name, ok, detail):
    ROWS.check(f"[{port}] activity: {name}", ok, detail)


def scenarios(b, port):
    for _engine, dev, vp, _cpu in [d for d in common.DEVICES if d[0] == "webkit"]:
        tag = f"webkit/{dev}"
        for sc in ("reopen-bg", "reopen-chat", "approval", "cron-prompt"):
            page, errs = common.boot(b, port, vp, None)
            o = page.evaluate(JS, sc)
            page.close()
            print(f"--- {tag} {sc}:", json.dumps(o, ensure_ascii=False))
            check(tag, f"{sc}: no page errors", not errs, errs[:3])
            if sc == "cron-prompt":
                for k in ("plain", "memory"):
                    shown = o[k]
                    check(tag, f"cron-prompt ({k}): the scheduled prompt shows as a user turn, memory stripped",
                          len(shown) == 1 and "Check the feeds" in shown[0]
                          and shown[0].startswith("[Scheduled run")
                          and "<memory>" not in shown[0], shown)
                continue
            j = o["joined"]
            want = "Preparing web search…" if sc == "reopen-chat" else "Searching the web…"
            check(tag, f"{sc}: the row shows on open, at the run's phase",
                  not j["hidden"] and j["label"] == want, j)
            check(tag, f"{sc}: the timer counts from the run's real start (>= 1:35)",
                  o["joined_secs"] >= 95, j["timer"])
            labels = [j["label"]]
            if sc == "approval":
                p, c = o["pending"], o["cleared"]
                labels += [p["label"], c["label"]]
                check(tag, "approval: pending → 'Waiting for your approval…'",
                      not p["hidden"] and p["label"] == "Waiting for your approval…", p)
                check(tag, "approval: cleared → the tool's label is back",
                      not c["hidden"] and c["label"] == "Searching the web…", c)
                check(tag, "approval: the timer kept counting through it", secs(c["timer"]) >= 95, c["timer"])
            check(tag, f"{sc}: no 'Working…' label", "Working…" not in labels, labels)


def secs(t: str) -> int:
    m, _, s = (t or "").partition(":")
    return 60 * int(m) + int(s) if m.isdigit() and s.isdigit() else -1


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
                ROWS.check("activity: scenario ran", False, repr(exc)[:300])
            b.close()
    finally:
        h.shutdown()
    return ROWS.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
