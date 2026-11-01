#!/opt/homebrew/bin/python3
"""Suite `stream` — the smooth-stream render checks, on every device port.

Streams a realistic mixed-markdown answer (instrument.js `smooth`: heading,
list, an 18-line code block, a 12-row table, prose; lumpy ~24-char chunks with
stalls) through the REAL webui over scroll_follow's mock SSE, sampling layout
once per rAF. Hard rows (metrics.py): block retypes 0, raw markdown markers 0
(BK-028), thinking-strip collapse 0 px (BK-030), selection survives (BK-028),
tap highlight transparent on every transcript tappable (BK-026), a code
block's sideways scroll survives (BK-028), and the 16k-char `long` answer's
DOM churn stays under the goal line. Frame/parse/sanitize timings are MEASURE
lines only (the timing rule, README).

Run through run_probe.py (`--only=stream`); standalone:
  /opt/homebrew/bin/python3 tests/fixtures/smoothness/stream.py [--engines=webkit] [--webui=DIR]
"""
from __future__ import annotations

import sys

import common
import metrics

TAP_STATE = """() => { const l = document.querySelector('#chat-log');
  return { pinned: window.__probe.metrics().pinned, jumpPill: !document.querySelector('#chat-jump').hidden,
           gap: Math.round(l.scrollHeight - l.clientHeight - l.scrollTop) }; }"""

# Every tappable in the transcript, not just the two named controls. The fixture streams no links or
# photos, so a static sample of the markup renderMd (a, details/summary) and thumbs.js (.attach-open)
# emit is appended after the run closes, swept, and removed.
TAP_SWEEP = """() => { const log = document.querySelector('#chat-log');
  const sample = document.createElement('div');
  sample.innerHTML = '<div class="msg agent"><div class="md"><p><a href="#x">link</a></p>' +
    '<details><summary>more</summary>body</details></div></div>' +
    '<div class="msg user"><div class="attach-row"><a class="attach-open" href="#y">p</a></div></div>';
  log.appendChild(sample);
  const els = [...log.querySelectorAll('a[href], button, summary, [role=button], [tabindex]')];
  const name = (e) => e.tagName.toLowerCase() + [...e.classList].map((c) => '.' + c).join('');
  const bad = els.filter((e) => getComputedStyle(e).webkitTapHighlightColor !== 'rgba(0, 0, 0, 0)')
    .map((e) => name(e) + ': ' + getComputedStyle(e).webkitTapHighlightColor);
  sample.remove();
  return { checked: els.length, kinds: [...new Set(els.map(name))].sort(), default_highlight: [...new Set(bad)].sort() }; }"""

SELECTION = """async () => {
  const mds = document.querySelectorAll('.msg.agent .md'); const md = mds[mds.length - 1];
  const r = document.createRange(); r.selectNodeContents(md.firstElementChild || md);
  const s = getSelection(); s.removeAllRanges(); s.addRange(r); const before = s.toString().length;
  await new Promise((ok) => setTimeout(ok, 400));
  return { before, after: getSelection().toString().length, anchorConnected: !!(s.anchorNode && s.anchorNode.isConnected) }; }"""

NAMED_HIGHLIGHT = """() => ['.think-head', '.pill'].map((s) => {
  const e = document.querySelector(s); return e ? s + ': ' + getComputedStyle(e).webkitTapHighlightColor : s + ': n/a'; })"""


def stop_sampler(page) -> dict:
    return metrics.summarize(page.evaluate("() => { window.__S.on = false; return window.__S; }"))


def run_smooth(page, rows, key):
    common.send(page, "smooth")
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=120000)
    page.wait_for_timeout(1500)
    s = stop_sampler(page)
    for name, ok, detail in metrics.stream_checks(s):
        rows.check(f"[{key}] stream: {name}", ok, detail)
    rows.measure(f"[{key}] smooth", {k: s[k] for k in metrics.TIMING_KEYS})


def run_tapper(page, rows, key):
    """The `tapper` answer (text, a slow tool call, a long answer): selection
    survival mid-stream, then the tap-highlight sweep once the run closed."""
    common.send(page, "tapper")
    page.wait_for_function("() => document.querySelectorAll('.msg.agent .md').length >= 2", timeout=30000)
    page.wait_for_timeout(5000)
    c = metrics.selection_check(page.evaluate(SELECTION))
    rows.check(f"[{key}] stream: {c[0]}", c[1], c[2])
    named = page.evaluate(NAMED_HIGHLIGHT)
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=120000)
    c = metrics.highlight_check(named, page.evaluate(TAP_SWEEP))
    rows.check(f"[{key}] stream: {c[0]}", c[1], c[2])


def run_scroll_left(page, rows, key):
    """A reader swipes the code block 40 px sideways mid-stream; the stream and
    the finalize (renderMd(raw)) must leave it there, on the same node."""
    common.send(page, "smooth")
    page.wait_for_function("() => { const w = document.querySelector('.md pre');"
                           " return w && w.scrollWidth > w.clientWidth + 2; }", timeout=60000)
    h = page.evaluate("""async () => {
      const q = () => document.querySelector('.md pre'); window.__pre0 = q(); q().scrollLeft = 40; const set = q().scrollLeft;
      await new Promise((ok) => setTimeout(ok, 250));
      return { set, mid_after250ms: q().scrollLeft, mid_sameNode: q() === window.__pre0 }; }""")
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=120000)
    page.wait_for_timeout(1500)
    h.update(page.evaluate("""() => { const q = document.querySelector('.md pre');
      return { post_complete: q.scrollLeft, post_sameNode: q === window.__pre0 }; }"""))
    c = metrics.scroll_left_check(h)
    rows.check(f"[{key}] stream: {c[0]}", c[1], c[2])


def run_long(page, rows, key):
    common.send(page, "long")
    page.wait_for_function("() => window.__mock.runClosed === true", timeout=240000)
    page.wait_for_timeout(1500)
    s = stop_sampler(page)
    c = metrics.long_churn_check(s)
    rows.check(f"[{key}] stream: {c[0]}", c[1], c[2])
    rows.measure(f"[{key}] long", {k: s[k] for k in metrics.TIMING_KEYS})


def main(argv: list[str]) -> int:
    args = common.suite_args(argv)
    from playwright.sync_api import sync_playwright
    common.hang_alarm(1200)
    common.build_site(args.webui)
    httpd, port = common.serve_site()
    rows = common.Rows()
    try:
        with sync_playwright() as pw:
            for engine, name, vp, cpu in common.devices(args.engines):
                key = f"{engine}/{name}"
                print(f"== {key} ==", flush=True)
                b = common.launch(pw, engine)
                errs: list[str] = []
                # scrollLeft needs a code block wider than its box: on the iPad's 820 px log the 18-line
                # block fits, so (as extra.py did) it runs on the iPhone-width ports only.
                fns = [run_smooth, run_tapper] + ([run_scroll_left] if vp["width"] < 600 else []) + [run_long]
                for fn in fns:
                    page, e = common.boot(b, port, vp, cpu)
                    try:
                        fn(page, rows, key)
                    except Exception as exc:  # a crashed scenario is a failed one, loudly
                        rows.check(f"[{key}] stream: {fn.__name__} ran", False, repr(exc)[:300])
                    errs += e
                    page.close()
                rows.check(f"[{key}] stream: no page errors", not errs, errs[:3])
                b.close()
    finally:
        httpd.shutdown()
    return rows.finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
