#!/opt/homebrew/bin/python3
"""Suite `reopen` — BK-024: the SENDER leaves its own chat mid-reply and comes
back while the run is still going (scroll_follow's mock_fetch.js `reopen-mid`
script: 25 words over ~6.5 s, the run's ring answers /messages' live join).
WebKit, iPhone + iPad ports.

  join        the reopened chat shows the reply so far
  follows     the reply keeps growing on screen after the reopen (BK-024: it
              froze — the for-good latch dropped every later frame)
  row         the activity row runs while it streams (BK-041)
  end         once the run ends the whole answer is on screen exactly once,
              the row is gone and the composer reads Send

Run through run_probe.py (`--only=reopen`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/reopen.py [--webui=DIR]
"""
from __future__ import annotations

import json
import sys

import common
from common import send

ROWS = common.Rows()

JS = """async () => {
  const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
  const M = window.__mock, sid = 'sid-reopen-mid', other = 'rm-other', out = {};
  M.store[other] = { messages: [{ role: 'user', text: 'hi' }, { role: 'assistant', text: 'OTHER CHAT',
    tool_calls: [], tool_results: [], content_blocks: [{ type: 'text', text: 'OTHER CHAT' }] }], title: 'Other' };
  const log = () => document.querySelector('#chat-log').innerText;
  const words = () => (log().match(/rmword\\d+/g) || []).length;
  const snap = () => ({ words: words(), row: !document.querySelector('#chat-status').hidden,
    label: document.querySelector('#chat-status-label').textContent,
    mode: document.querySelector('#chat-send').dataset.mode });
  await sleep(1500);                                   // ~5 words in
  document.dispatchEvent(new CustomEvent('open-session', { detail: other }));
  await sleep(800);
  out.away = { other: log().includes('OTHER CHAT'), words: words() };
  document.dispatchEvent(new CustomEvent('open-session', { detail: sid }));
  await sleep(700);
  out.reopen = snap();
  await sleep(1800);
  out.later = snap();
  const t0 = performance.now();
  while (M.doneAt == null && performance.now() - t0 < 15000) await sleep(50);
  out.done_seen = M.doneAt != null;
  await sleep(400);                                    // the paced reveal's last words
  out.at_done = words();                               // streamed, before any end-of-run heal
  await sleep(2500);                                   // any end-of-run heal lands
  out.end = snap();
  out.end.final = (log().match(/rmfinal\\./g) || []).length;
  out.end.word0 = (log().match(/rmword0 /g) || []).length;
  out.end.screen = (await import('/state.js')).ui.sessionId;
  return out; }"""


def check(tag, name, ok, detail):
    ROWS.check(f"[{tag}] reopen: {name}", ok, detail)


def scenarios(b, port):
    for _engine, dev, vp, _cpu in [d for d in common.DEVICES if d[0] == "webkit"]:
        tag = f"webkit/{dev}"
        page, errs = common.boot(b, port, vp, None)
        send(page, "reopen-mid")
        o = page.evaluate(JS)
        page.close()
        print(f"--- {tag}:", json.dumps(o, ensure_ascii=False))
        r, l, e = o["reopen"], o["later"], o["end"]
        check(tag, "no page errors", not errs, errs[:3])
        check(tag, "away: the other chat is on screen, none of the run's words", o["away"]["other"]
              and o["away"]["words"] == 0, o["away"])
        check(tag, "join: the reopened chat shows the reply so far", r["words"] >= 3, r)
        check(tag, "follows: the reply keeps streaming after the reopen (>= 20 of 24 words as the run ends)",
              l["words"] > r["words"] and o["at_done"] >= 20, [r["words"], l["words"], o["at_done"]])
        check(tag, "row: the activity row runs while it streams", r["row"] and l["row"], [r, l])
        check(tag, "stop: the composer reads Stop mid-run", r["mode"] == "stop" and l["mode"] == "stop", [r["mode"], l["mode"]])
        check(tag, "end: the whole answer is on screen exactly once",
              o["done_seen"] and e["final"] == 1 and e["word0"] == 1 and e["words"] == 24, e)
        check(tag, "end: the row is gone and the composer reads Send",
              not e["row"] and e["mode"] == "send", e)


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
                ROWS.check("reopen: scenario ran", False, repr(exc)[:300])
            b.close()
    finally:
        h.shutdown()
    return ROWS.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
