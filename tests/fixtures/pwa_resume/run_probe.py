#!/usr/bin/env python3
"""Headless repro probe for the iOS-PWA resume flash (plan pwa-resume-reconcile, Step 1).

Recipe: learnings.md 2026-10-16 — symlink the real webui files into a scratch
dir, serve with http.server (ES module imports need http://, not file://), run
the CACHED chrome-headless-shell with --dump-dom. One correction found today:
--dump-dom dumps at the LOAD event, so a --timeout alone misses anything the
page writes asynchronously — --virtual-time-budget fast-forwards the page's
timers and then dumps (verified against a 800ms timer page).

The page served is a copy of the REAL index.html (so every DOM lookup the
webui modules do at import time finds its element) with the app.js entry tag
replaced by mock_fetch.js + probe.js. probe.js drives the production recovery
path and writes its assertions into <pre id="probe-results">.

THE HEAL CONTRACT (pwa-accurate-resume Step 6): a resume asks
GET /api/chat/{sid}/live AT THE CLIENT'S OWN WATERMARK (webui/replay.js);
active:true gap-fills the ring tail UNPACED into the existing DOM, active:false
falls back to the persisted refetch. Re-syncing from the STORE mid-call stays
forbidden (it lacks the in-flight answer and would wipe the on-screen head).
The older wizard-era yardsticks below stay green as the regression guard.

GREEN (pwa-accurate-resume Step 6): the four live-resume assertions scenario L
(probe_live.js) added — `resume-mid-thinking`, `no-duplicate-words`,
`partial-head-survives`, `cold-open-live` — went green with the client resync
and now guard it, together with two Step-6 regression guards (`reopen-dedupe`,
`seq-restart-new-run-paints`) and six run-identity assertions
(`second-device-run`, `second-device-short-run`, `empty-tail-join-provisional`,
`terminal-join-attribution`, `late-old-run-title`, `missing-route` — the
identity rule: a slot vouches for one run; an unverifiable /live batch, or an
empty one whose seq restarted below the watermark, cold-joins).
From compaction-checkpoint-bookmark Step 4 also scenario M (probe_compaction.js):
`compaction-live`, `compaction-gapfill`, `compaction-join` + the three orphan
assertions (the live "Compacting…" phase and the seam through every painter),
and from Step 5 the `compaction-reload` assertions (the seam anchored from
/messages `compactions` across a refresh, the mid-turn compactions-vs-join-tail
dedupe, and the flush of trailing seams above the live tail).

README.md carries what each pins. TODAY's correct state is exit 0; any FAIL
line is a regression.

Engines (BK-019 Step 5 matrix): chromium (the chrome-headless-shell
--virtual-time-budget recipe above) and WEBKIT (the shipping engine for the
iPad/iPhone PWAs). No flag = BOTH when Playwright is importable (the system
python3, per README); under the uv venv (no Playwright) chromium alone with a
loud NOTE. `--engines=chromium,webkit` / `--engines=webkit` pins the list.
WebKit has no virtual-time knob, so that mode drives the SAME probe page
through Playwright's cached WebKit in REAL time and waits for the results
block (fixture sleeps carry 350–700 ms of slack over the 150 ms mock latency;
rAF actually ticks there, which only makes the paced reveal paint the way
finalizeAgentBubble() already forces). A run is only green when every engine
produced the matrix's assertion SET (unique names, floor
MIN_UNIQUE_ASSERTIONS; two engines must agree) — a results block with zero
rows is exit 2, never a pass.

Exit codes:
  0  every assertion PASS (the state since Step 6 landed)
  1  probe ran, assertions failed — any failure is a regression; the older
     yardsticks must stay green: blank-flash = Step 2's atomic swap, mid-word
     + heal = the seq-less accepted artefact + run_done refetch,
     no-premature-resync = no renderSession and no /messages fetch before
     run_done (a /live gap-fill fetch is Step 6's non-lossy path, not a
     /messages re-sync, and does not violate it)
  2  harness broke: jsc parse-check failed, no cached browser for the
     requested engine, no results block from an engine that ran, an engine
     produced fewer unique assertions than the matrix floor (a scenario family
     silently skipped), or the engines' assertion SETS disagree
"""
from __future__ import annotations

import glob
import html
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # tests/fixtures/pwa_resume
REPO = Path(__file__).resolve().parents[3]
WEBUI = REPO / "src" / "harness" / "gateway" / "webui"
WORK = REPO / "scratch" / "pwa_resume_probe"            # gitignored; rebuilt each run
SITE = WORK / "site"
JSC = Path(
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
)
# The app.js ENTRY TAG only — matched version-tolerantly, because the webui
# convention (skills/webui-change) bumps its `?v=` on EVERY webui asset edit:
# Steps 2/3 edit transcript.js/events.js, so a pinned `?v=45` here would make
# the probe exit 2 ("harness error") on exactly the runs it exists to measure.
# A genuine shape change (entry renamed, src dropped) still fails loudly below.
APP_ENTRY_RE = re.compile(r'<script[^>]*\bsrc="/app\.js(?:\?v=\d+)?"[^>]*>\s*</script>')
PROBE_ENTRY = (
    '<script src="/mock_fetch.js"></script>\n'
    '  <script type="module" src="/probe.js"></script>'
)


def build_site() -> None:
    """Mirror the webui dir with symlinks; swap the entry tag in index.html."""
    if SITE.exists():
        shutil.rmtree(SITE)  # our own scratch mirror, rebuilt every run
    SITE.mkdir(parents=True)
    for entry in WEBUI.iterdir():
        os.symlink(entry, SITE / entry.name)
    for fixture in ("probe.js", "probe_coldstart.js", "probe_live.js",
                    "probe_compaction.js", "mock_fetch.js"):
        os.symlink(HERE / fixture, SITE / fixture)
    index = (WEBUI / "index.html").read_text()
    match = APP_ENTRY_RE.search(index)
    if match is None:
        lines = [l.strip() for l in index.splitlines() if "app.js" in l]
        print("harness error: no app.js entry tag in index.html "
              f"(expected a module <script src=\"/app.js[?v=N]\">); found: "
              f"{lines or '(none)'}", file=sys.stderr)
        sys.exit(2)
    print(f"entry tag matched: {match.group(0)!r}")
    (SITE / "probe.html").write_text(
        index[: match.start()] + PROBE_ENTRY + index[match.end():]
    )


def jsc_check() -> None:
    """Parse-check the fixture JS before spending a browser launch on it."""
    out = subprocess.run(
        [str(JSC), "tests/fixtures/pwa_resume/jsc_check.js"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    ).stdout
    print(out.strip())
    if "JSC-RESULT: PASS" not in out:
        print("harness error: jsc parse-check failed (see lines above)", file=sys.stderr)
        sys.exit(2)


def chrome_binary() -> str:
    builds = sorted(
        glob.glob(os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium_headless_shell-*/"
            "chrome-headless-shell-mac-arm64/chrome-headless-shell"
        ))
    )
    if not builds:
        print("harness error: no cached chrome-headless-shell "
              "(see learnings.md 2026-10-16 for the recipe)", file=sys.stderr)
        sys.exit(2)
    return builds[-1]


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


# The Step-5 acceptance is a TWO-engine matrix ("both probes exit 0 on both
# engines"); a runner that can exit 0 while an engine contributed ZERO rows is
# the worst failure this fixture can ship, so the floor is structural. 107 is
# today's assertion set (all PASS, identical names on chromium and WebKit).
# May only RISE: a deliberate merge/replacement edits the figure in the same
# change (same discipline as tests/test_jsunit.py's check floor).
MIN_UNIQUE_ASSERTIONS = 107


def default_engines() -> list[str]:
    """Both engines when Playwright is importable — the README's canonical
    `python3 run_probe.py` — and chromium alone under the uv venv (which
    deliberately has no Playwright), with a LOUD note: a one-engine run must
    never pass for the matrix."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("NOTE: engines: chromium only — Playwright is not importable "
              "here (the uv venv deliberately lacks it). The Step-5 matrix "
              "needs the system python3, or --engines=chromium,webkit.")
        return ["chromium"]
    return ["chromium", "webkit"]


def parse_engines(argv: list[str], default: list[str] | None = None) -> list[str]:
    """`--engines=chromium,webkit` (scroll_follow's flag shape). With no flag,
    `default` (or the environment's default) applies — never a silent single
    engine without shrinking the matrix's meaning."""
    engines = list(default) if default is not None else default_engines()
    for a in argv:
        k, _, v = a.partition("=")
        if k != "--engines":
            print(f"usage error: unknown flag {a!r} (see module docstring)",
                  file=sys.stderr)
            sys.exit(2)
        want = [x for x in v.split(",") if x]
        if not want or not set(want) <= {"chromium", "webkit"}:
            print("usage error: --engines wants chromium and/or webkit",
                  file=sys.stderr)
            sys.exit(2)
        engines = list(dict.fromkeys(want))
    return engines


def assertion_names(body: str) -> list[str]:
    """The PASS/FAIL rows' assertion names (text before a ` | detail` suffix).
    Unique-row counts only — a doubled print must not inflate the matrix."""
    return [ln[5:].split(" | ")[0] for ln in body.splitlines()
            if ln.startswith(("PASS ", "FAIL "))]


def coverage_problem(engine: str, body: str) -> str | None:
    """A results block is NOT proof the scenarios ran: the page's finally prints
    PROBE-RESULT: PASS with zero rows when main() returns early. Floor on
    unique names, so 'engine silently contributed nothing' is exit 2."""
    names = set(assertion_names(body))
    if len(names) < MIN_UNIQUE_ASSERTIONS:
        return (f"{engine} produced {len(names)} unique assertion(s), floor "
                f"{MIN_UNIQUE_ASSERTIONS} — a scenario family did not run; a "
                "zero-row results block must never read as green")
    return None


def engine_set_problem(bodies: list[tuple[str, str]]) -> str | None:
    """Cross-engine identity: a family skipped only on one engine shows up as a
    SET difference even when the totals happen to match."""
    if len(bodies) < 2:
        return None
    base_engine = bodies[0][0]
    base = set(assertion_names(bodies[0][1]))
    for engine, body in bodies[1:]:
        names = set(assertion_names(body))
        if names != base:
            missing = sorted(base - names)
            extra = sorted(names - base)
            return (f"{engine}'s assertion set differs from {base_engine}'s "
                    f"(missing={missing[:4]}, extra={extra[:4]}) — an engine "
                    "ran a different scenario set")
    return None


def run_chrome(port: int) -> str:
    cmd = [
        chrome_binary(),
        "--headless", "--no-sandbox", "--no-first-run",
        f"--user-data-dir={WORK / 'udd'}",
        # 6000 was enough until scenario I grew the three-state boots (+~0.7s of
        # virtual timers); 9000 kept headroom; Step 5's scenario L adds four more
        # suspend/resume cycles with fetch windows (+~2.2s) — 15000 keeps the
        # headroom. Virtual time fast-forwards; the wall clock only cares that
        # the budget ENDS.
        "--virtual-time-budget=15000",
        "--dump-dom",
        f"http://127.0.0.1:{port}/probe.html",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        print("harness error: chrome-headless-shell timed out", file=sys.stderr)
        sys.exit(2)
    if "PROBE-RESULTS-BEGIN" not in proc.stdout:
        (WORK / "dump-fail.html").write_text(proc.stdout)
        tail = proc.stderr.strip().splitlines()[-3:]
        print("chrome stderr tail:", *tail, sep="\n  ")
        print(f"(DOM dump saved to {WORK / 'dump-fail.html'})")
    return proc.stdout


def run_webkit(port: int) -> str:
    """WebKit pass (BK-019 Step 5): the same probe page, Playwright's cached
    WebKit, REAL time — there is no virtual-time knob, and the fixture sleeps
    are already sized for real timers (350–700 ms settle windows over the
    150 ms mock latency). Waits for the results block instead of dumping at a
    budget end."""
    try:  # lazy: the uv venv has no Playwright, and importing this module
        from playwright.sync_api import Error as PlaywrightError  # must stay
        from playwright.sync_api import sync_playwright           # side-effect-free
    except ImportError as exc:
        print("harness error: --engines=webkit needs the system python3 "
              "(it has Playwright; the uv venv deliberately does not): "
              f"{exc}", file=sys.stderr)
        sys.exit(2)
    if not glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/webkit-*/")):
        print("harness error: no cached WebKit under ~/Library/Caches/ms-playwright/"
              "webkit-* — install with: python3 -m playwright install webkit",
              file=sys.stderr)
        sys.exit(2)
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            page = browser.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/probe.html", wait_until="load")
            try:
                page.wait_for_function(
                    "() => { const pre = document.querySelector('#probe-results'); "
                    "return !!pre && pre.textContent.includes('PROBE-RESULTS-END'); }",
                    timeout=180_000,
                )
            except PlaywrightError as exc:
                # The probe never reached its finally — dump what there IS.
                partial = page.evaluate(
                    "() => { const p = document.querySelector('#probe-results');"
                    " return p ? p.textContent : ''; }")
                (WORK / "dump-fail-webkit.html").write_text(page.content())
                print("harness error: WebKit run did not reach its results "
                      f"block: {exc}", file=sys.stderr)
                print("  pageerrors:", *errors[-5:], sep="\n  ")
                print(f"  partial results tail: {partial[-1500:]}", file=sys.stderr)
                print(f"  (page dump saved to {WORK / 'dump-fail-webkit.html'})")
                sys.exit(2)
            return page.evaluate(
                "() => document.querySelector('#probe-results').textContent")
        finally:
            browser.close()


def main() -> None:
    engines = parse_engines(sys.argv[1:])
    print(f"engines: {', '.join(engines)}")
    build_site()
    jsc_check()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(_Quiet, directory=str(SITE)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    bodies: list[tuple[str, str]] = []
    try:
        for engine in engines:
            raw = run_webkit(port) if engine == "webkit" else run_chrome(port)
            match = re.search(r"PROBE-RESULTS-BEGIN([\s\S]*?)PROBE-RESULTS-END", raw)
            if not match:
                print(f"harness error: no results block from {engine} (probe did "
                      "not reach its finally — see probe.html)", file=sys.stderr)
                sys.exit(2)
            bodies.append((engine, html.unescape(match.group(1)).strip()))
    finally:
        httpd.shutdown()
    failed = 0
    problems: list[str] = []
    for engine, body in bodies:
        print(f"\n===== {engine} =====")
        print(body)
        fails = [line for line in body.splitlines() if line.startswith("FAIL")]
        print(f"{engine}: {len(fails)} failing assertion(s)")
        # The per-engine PROBE-RESULT line is the authority on assertion-level
        # failure; the COVERAGE floor is separate — a results block that printed
        # PASS with zero rows (main() returned early) is a harness break, not a
        # green engine.
        if fails or "PROBE-RESULT: PASS" not in body:
            failed += 1
        problem = coverage_problem(engine, body)
        if problem:
            print(f"harness error: {problem}", file=sys.stderr)
            problems.append(problem)
    if len(bodies) > 1:
        problem = engine_set_problem(bodies)
        if problem:
            print(f"harness error: {problem}", file=sys.stderr)
            problems.append(problem)
    print(f"\n{failed} failing engine(s)")
    if problems:
        sys.exit(2)
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
