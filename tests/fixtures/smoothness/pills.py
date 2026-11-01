#!/opt/homebrew/bin/python3
"""Suite `pills` — BK-035 (plan step 3b): a tool pill opens or closes ONLY when
the user taps it. WebKit iPhone port.

A background run on the on-screen chat (the bgrun path) streams a row of tool
calls; taps are real WebKit touchscreen taps. Then the session re-renders
twice: the step-2 resume heal (hidden -> persisted -> visible) and a chat
switch away and back.
  live:   a failed result leaves the drawer closed; tap opens, tap closes; a
          second failure in the row neither opens the drawer nor steals the
          pill the user has open; a pill the user opened while it ran
          refreshes with its (failed) result and stays open
  replay: every re-render paints the failed pills closed; open-close-rerender
          stays closed

Run through run_probe.py (`--only=pills`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/pills.py [--webui=DIR]
"""
from __future__ import annotations

import json
import sys

import common

SID, OTHER = "pill-chat", "other-chat"
SETUP = """([sid, other]) => {
  const M = window.__mock;
  M.store[other] = { messages: [{ role: 'user', text: 'hi' }, { role: 'assistant', text: 'OTHER CHAT', tool_calls: [],
    tool_results: [], content_blocks: [{ type: 'text', text: 'OTHER CHAT' }] }], title: 'Other' };
  M.store[sid] = { messages: [{ role: 'user', text: 'Check the sites' }], title: 'Pill Job' };
  window.__pp = {
    d: (ev) => window.__sk.handleRunEvent(Object.assign({ session_id: sid }, ev)), // background: no run_id, no seq
    vis: (v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
                  Object.defineProperty(document, 'hidden', { configurable: true, get: () => v === 'hidden' });
                  document.dispatchEvent(new Event('visibilitychange')); },
    fetches: () => M.calls.filter((c) => c === '/api/sessions/' + sid + '/messages').length,
  }; }"""
# The drawer state of every pill row on screen, plus each pill's face.
STATE = """() => [...document.querySelectorAll('#chat-log .pill-row')].map((row) => {
  const d = row.querySelector('.pill-detail');
  return { open: !d.hidden, errorOpen: d.classList.contains('error-open'), text: d.hidden ? '' : d.textContent,
    pills: [...row.querySelectorAll('.pill')].map((p) => ({ name: p.querySelector('.pill-name').textContent,
      cls: ['running', 'done', 'error'].find((c) => p.classList.contains(c)),
      status: p.querySelector('.pill-status').textContent, expanded: p.getAttribute('aria-expanded') })) }; })"""
CALLS = [("A", "web_search", False, "3 results"), ("B", "web_fetch", True, "boom B: 403"),
         ("C", "web_fetch", True, "boom C: 500"), ("D", "web_fetch", True, "boom D: timeout"),
         ("E", "browser_fetch", True, "boom E: refused")]

ROWS = common.Rows()


def check(name, ok, detail):
    ROWS.check("[webkit/iphone] pills: " + name, ok, detail)


def tap(page, name):
    """A real touch tap on the pill whose name is `name` (one row; names are unique per probe)."""
    box = page.locator(f"#chat-log .pill:has(.pill-name:text-is('{name}'))").bounding_box()
    page.touchscreen.tap(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_timeout(250)


def call(page, key):
    k, tool, _, _ = next(c for c in CALLS if c[0] == key)
    page.evaluate("([t, k]) => window.__pp.d({ type: 'tool_call', tool: t, args: { q: k } })", [f"{tool}_{k}", k])
    page.wait_for_timeout(150)


def result(page, key):
    _, _, err, text = next(c for c in CALLS if c[0] == key)
    page.evaluate("([e, t]) => window.__pp.d({ type: 'tool_result', is_error: e, digest: t, chars: t.length })", [err, text])
    page.wait_for_timeout(150)


def opened(st):
    """(row index, pill names with aria-expanded=true) for every row whose drawer is open."""
    return [(i, [p["name"] for p in r["pills"] if p["expanded"] == "true"]) for i, r in enumerate(st) if r["open"]]


def persisted_blocks():
    blocks = [{"type": "tool_use", "id": f"t{k}", "name": f"{tool}_{k}", "input": {"q": k}} for k, tool, _, _ in CALLS]
    results = [{"tool_call_id": f"t{k}", "is_error": err, "content": text} for k, _, err, text in CALLS]
    return [{"role": "assistant", "text": "", "tool_calls": [], "tool_results": results, "content_blocks": blocks},
            {"role": "assistant", "text": "Two sites failed.", "tool_calls": [], "tool_results": [],
             "content_blocks": [{"type": "text", "text": "Two sites failed."}]}]


def replay_checks(page, label):
    st = page.evaluate(STATE)
    errs = sum(p["cls"] == "error" for r in st for p in r["pills"])
    check(f"{label}: the replay painted the 4 failed pills, red and '✗ failed'",
          errs == 4 and all(p["status"] == "✗ failed" for r in st for p in r["pills"] if p["cls"] == "error"), errs)
    sr = page.evaluate("() => document.querySelector('#sr-status').textContent")
    check(f"{label}: replay announces nothing (history isn't news)", sr == "", sr)
    check(f"{label}: every drawer closed, no pill expanded", opened(st) == [] and not any(
        p["expanded"] == "true" for r in st for p in r["pills"]), opened(st))


def scenario(b, port):
    page, errs = common.boot(b, port, common.IPHONE, None)
    page.evaluate(SETUP, [SID, OTHER])
    page.evaluate("async () => { const { ui } = await import('/state.js'); ui.expecting = true; }")  # Fire-now path
    page.evaluate("() => window.__pp.d({ type: 'run_start', title: 'Pill Job' })")
    page.wait_for_timeout(600)
    call(page, "A"); result(page, "A")
    call(page, "B"); result(page, "B")
    st = page.evaluate(STATE)
    b_pill = st[-1]["pills"][-1]
    check("live: a failed result is loud (red, '✗ failed', announced)",
          b_pill["cls"] == "error" and b_pill["status"] == "✗ failed"
          and page.evaluate("() => document.querySelector('#sr-status').textContent") == "Tool web_fetch_B failed",
          b_pill)
    check("live: a failed result leaves the drawer closed", opened(st) == [], opened(st))
    tap(page, "web_fetch_B")
    st = page.evaluate(STATE)
    check("live: tap opens the failed pill's drawer, error-styled, with its result",
          opened(st) == [(0, ["web_fetch_B"])] and st[0]["errorOpen"] and "boom B" in st[0]["text"], opened(st))
    tap(page, "web_fetch_B")
    st = page.evaluate(STATE)
    check("live: a second tap closes it", opened(st) == [], opened(st))
    call(page, "C"); result(page, "C")
    st = page.evaluate(STATE)
    check("live: a second failure in the row leaves the closed drawer closed", opened(st) == [], opened(st))
    tap(page, "web_search_A")
    call(page, "D"); result(page, "D")
    st = page.evaluate(STATE)
    check("live: a failure does not steal the pill the user has open",
          opened(st) == [(0, ["web_search_A"])] and "3 results" in st[0]["text"] and "boom D" not in st[0]["text"]
          and not st[0]["errorOpen"], [opened(st), st[0]["text"][-60:]])
    call(page, "E")
    tap(page, "browser_fetch_E")  # the user opens it while it runs
    result(page, "E")
    st = page.evaluate(STATE)
    check("live: a pill opened while running stays open and refreshes with its failed result",
          opened(st) == [(0, ["browser_fetch_E"])] and "boom E" in st[0]["text"] and st[0]["errorOpen"],
          [opened(st), st[0]["text"][-60:]])
    tap(page, "browser_fetch_E")
    check("live: closed again by the user", opened(page.evaluate(STATE)) == [], "")
    # Resume heal (step 2): the app sleeps mid-run, the run finishes, the app comes back.
    page.evaluate("() => window.__pp.vis('hidden')")
    page.evaluate("(rows) => { const r = window.__mock.store['%s']; r.messages = r.messages.concat(rows); }" % SID,
                  persisted_blocks())
    page.wait_for_timeout(300)
    before = page.evaluate("() => window.__pp.fetches()")
    page.evaluate("() => { document.querySelector('#sr-status').textContent = ''; }")
    page.evaluate("() => window.__pp.vis('visible')")
    page.wait_for_function("() => document.querySelector('#chat-log').innerText.includes('Two sites failed.')",
                           timeout=5000)
    page.wait_for_timeout(300)
    check("heal: the resume re-rendered the chat from the store",
          page.evaluate("() => window.__pp.fetches()") - before == 1, "")
    replay_checks(page, "heal")
    # The user opens and closes a failed pill, then switches away and back.
    tap(page, "web_fetch_C")
    check("heal: tap opens a replayed failed pill", opened(page.evaluate(STATE)) == [(0, ["web_fetch_C"])], "")
    tap(page, "web_fetch_C")
    for leg in ("away", "back"):
        page.evaluate("(id) => document.dispatchEvent(new CustomEvent('open-session', { detail: id }))",
                      OTHER if leg == "away" else SID)
        page.wait_for_function("(t) => document.querySelector('#chat-log').innerText.includes(t)",
                               arg="OTHER CHAT" if leg == "away" else "Two sites failed.", timeout=5000)
        page.wait_for_timeout(300)
        if leg == "away":
            page.evaluate("() => { document.querySelector('#sr-status').textContent = ''; }")
    replay_checks(page, "switch back")
    check("no page errors", not errs, errs[:3])
    page.close()


def main(argv: list[str]) -> int:
    args = common.suite_args(argv)
    from playwright.sync_api import sync_playwright
    common.hang_alarm(600)
    common.build_site(args.webui)
    h, port = common.serve_site()
    try:
        with sync_playwright() as pw:
            b = pw.webkit.launch()
            try:
                scenario(b, port)
            except Exception as exc:  # a crashed scenario is a failed one, loudly
                check("scenario ran", False, repr(exc)[:300])
            b.close()
    finally:
        h.shutdown()
    return ROWS.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
