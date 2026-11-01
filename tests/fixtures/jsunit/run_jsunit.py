#!/usr/bin/env python3
"""Offline JS unit tier (plan bk-019-run-lifecycle Step 2; BK-020 minimal).

Runs the REAL webui modules' pure logic under the cached chrome-headless-shell
— there is no node on this host (.claude/gotchas.md) and the modules are
browser ES modules. The recipe is copied from the proven
tests/fixtures/pwa_resume/run_probe.py: symlink the real webui files into a
scratch dir, serve with http.server (ES modules reject file://), run
chrome-headless-shell --dump-dom, parse the results block out of the dump.

Differences from the probe fixture, deliberate:
  * NO DOM fixture (suite.html is a bare page with a <pre>) — this tier only
    ever drive DOM-free modules (replay.js and runstate.js, which design §2.6
    keeps renderer-free precisely so). A module that
    touches the page at import time belongs in the probe tier.
  * --virtual-time-budget is KEPT: rAF callbacks never fire under it
    (gotchas), and rAF-free is this tier's admission rule, so it cannot bite
    — it is what lets async (promise-driven) tests finish before the
    dump. The plan's "no --virtual-time-budget for rAF code" excludes rAF
    code from THIS tier; it is not a ban on the flag for rAF-free code.
  * main() RETURNS an exit code and raises HarnessError instead of sys.exit,
    so tests/test_jsunit.py can call it in-process; standalone use wraps it.

Exit codes (pwa_resume precedent):
  0  every check PASS
  1  a check FAILED (or the page threw before reporting) — the tier is red
  2  harness broke: jsc parse-check failed, no cached chrome, or the suite
     never reported (no usable results block)
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

HERE = Path(__file__).resolve().parent                  # tests/fixtures/jsunit
REPO = Path(__file__).resolve().parents[3]
WEBUI = REPO / "src" / "harness" / "gateway" / "webui"
WORK = REPO / "scratch" / "jsunit_probe"                # gitignored; rebuilt each run
SITE = WORK / "site"
JSC = Path(
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
)
BEGIN = "JSUNIT-RESULTS-BEGIN"
END = "JSUNIT-RESULTS-END"


class HarnessError(RuntimeError):
    """The tier itself broke — distinct from a red assertion."""


def build_site() -> None:
    """Mirror the webui dir with symlinks; add the fixture's runner + tests."""
    if SITE.exists():
        shutil.rmtree(SITE)  # our own scratch mirror, rebuilt every run
    SITE.mkdir(parents=True)
    for entry in WEBUI.iterdir():
        os.symlink(entry, SITE / entry.name)
    for fixture in sorted(HERE.glob("*.test.js")):  # the glob is the contract
        os.symlink(fixture, SITE / fixture.name)
    for fixture in ("runner.js", "suite.js", "suite.html"):
        os.symlink(HERE / fixture, SITE / fixture)


def jsc_check() -> str:
    """Parse-check the suite's JS before spending a browser launch on it."""
    if not JSC.exists():
        raise HarnessError(f"no jsc helper at {JSC}")
    try:
        proc = subprocess.run(
            [str(JSC), "tests/fixtures/jsunit/jsc_check.js"],
            cwd=REPO, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise HarnessError("jsc parse-check timed out")
    out = proc.stdout
    if "JSC-RESULT: PASS" not in out:  # trust the PRINTED line, not the exit code
        raise HarnessError("jsc parse-check failed:\n" + out.strip())
    return out.strip()


def chrome_binary() -> str:
    builds = sorted(
        glob.glob(os.path.expanduser(
            "~/Library/Caches/ms-playwright/chromium_headless_shell-*/"
            "chrome-headless-shell-mac-arm64/chrome-headless-shell"
        ))
    )
    if not builds:
        raise HarnessError(
            "no cached chrome-headless-shell "
            "(see learnings.md 2026-10-16 for the recipe)"
        )
    return builds[-1]


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


def serve_and_dump() -> str:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(_Quiet, directory=str(SITE)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    cmd = [
        chrome_binary(),
        "--headless", "--no-sandbox", "--no-first-run",
        f"--user-data-dir={WORK / 'udd'}",
        # Fast-forwards timers, then dumps: the sync replay tests need no
        # budget, but runstate's promise-driven transitions (T27
        # resyncVerdict) need it to finish before --dump-dom fires.
        "--virtual-time-budget=4000",
        "--dump-dom",
        f"http://127.0.0.1:{port}/suite.html",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise HarnessError("chrome-headless-shell timed out")
    finally:
        httpd.shutdown()
    return proc.stdout


def parse_results(dump: str) -> list[str]:
    match = re.search(re.escape(BEGIN) + r"([\s\S]*?)" + re.escape(END), dump)
    if not match:
        (WORK / "dump-fail.html").write_text(dump)
        raise HarnessError(
            "no results block in the DOM dump (suite page never reported; "
            f"dump saved to {WORK / 'dump-fail.html'})"
        )
    body = html.unescape(match.group(1)).strip()
    if "(running)" in body:
        raise HarnessError(
            "the suite never reported (results block still at '(running)') — "
            "suite.js died before runAll() without tripping the page handler"
        )
    return [line for line in body.splitlines() if line.strip()]


def main() -> int:
    try:
        build_site()
        print(jsc_check())
        body = parse_results(serve_and_dump())
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        return 2
    for line in body:
        print(line)
    fails = [line for line in body if line.startswith("FAIL")]
    print(f"\n{len(fails)} failing check(s)")
    verdict = next((l for l in body if l.startswith("JSUNIT-RESULT:")), "")
    if fails or not verdict.startswith("JSUNIT-RESULT: PASS"):
        # The verdict line must EXIST and say PASS — a suite that died
        # mid-report leaves lines but no verdict; that is red, not green.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
