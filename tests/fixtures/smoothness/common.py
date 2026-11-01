"""Shared plumbing for the smoothness probe suites (tests/fixtures/smoothness/).

Import-safe: nothing here launches a browser or needs Playwright at import time
(tests/test_smoothness_probe.py imports run_probe/metrics under the uv venv,
which deliberately has no Playwright).

Two kinds of suite share it:
  * static-site suites (stream, taps, toggles, pills, bgrun) — the real webui
    dir mirrored with symlinks into WORK/site, the app.js entry tag swapped for
    scroll_follow's mock_fetch.js + probe_scroll.js (the mocked /api/* SSE),
    served over http:// by an in-process http.server;
  * real-gateway suites (stopanywhere, senderstop, orphan) — the REAL
    create_app on an ephemeral 127.0.0.1 port, spawned under `uv run python`
    (they need the harness package), never agentd's 8787.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # tests/fixtures/smoothness
REPO = Path(__file__).resolve().parents[3]
WEBUI = REPO / "src" / "harness" / "gateway" / "webui"
SCROLL_FOLLOW = REPO / "tests" / "fixtures" / "scroll_follow"
WORK = REPO / "scratch" / "smoothness_fixture"          # gitignored; rebuilt each run
SITE = WORK / "site"

# (engine, device, viewport, CPU throttle) — WebKit is the shipping engine of
# the iPad/iPhone PWAs; chromium at 4x CPU is the "slow phone" control.
DEVICES = [
    ("webkit", "iphone", {"width": 390, "height": 844}, None),
    ("webkit", "ipad", {"width": 820, "height": 1180}, None),
    ("chromium", "iphone-4xcpu", {"width": 390, "height": 844}, 4),
]
IPHONE = {"width": 390, "height": 844}
IPAD = {"width": 820, "height": 1180}

# Make document.visibilityState report `v` and fire visibilitychange — the
# app's sleep/wake path (a real hide never happens headlessly).
VIS_JS = """(v) => { Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => v });
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => v === 'hidden' });
  document.dispatchEvent(new Event('visibilitychange')); }"""


def scroll_follow_runner():
    """scroll_follow's run_probe.py, loaded under its own name: its APP_ENTRY_RE
    and PROBE_ENTRY are the single source of the entry swap (we serve ITS
    mock_fetch.js + probe_scroll.js). Loaded by path, not `import run_probe` —
    this directory has a run_probe.py of its own."""
    spec = importlib.util.spec_from_file_location(
        "scroll_follow_run_probe", SCROLL_FOLLOW / "run_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def suite_args(argv: list[str], extra=None) -> argparse.Namespace:
    """Every suite takes the same flags (run_probe.py passes them through)."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", default="webkit,chromium",
                    help="comma list of webkit/chromium")
    ap.add_argument("--webui", help="serve this web UI dir instead of the tree's (red runs)")
    if extra:
        extra(ap)
    args = ap.parse_args(argv)
    args.engines = [e for e in args.engines.split(",") if e]
    if not args.engines or not set(args.engines) <= {"webkit", "chromium"}:
        ap.error("--engines wants webkit and/or chromium")
    args.webui = Path(args.webui).resolve() if args.webui else WEBUI
    if not (args.webui / "index.html").is_file():
        ap.error(f"--webui {args.webui}: no index.html there")
    return args


def devices(engines: list[str]):
    return [d for d in DEVICES if d[0] in engines]


def hang_alarm(secs: int) -> None:
    """Raise (not the default kill) on a hang so each suite's finally still runs
    — stops its server, closes the browser — and the hang reads as a FAIL."""
    def _hung(*_):
        raise TimeoutError(f"probe hung: {secs} s alarm")
    signal.signal(signal.SIGALRM, _hung)
    signal.alarm(secs)


def build_site(webui: Path, patch_keepat: bool = False) -> None:
    """Mirror `webui` into SITE with symlinks; swap the app.js entry tag."""
    sf = scroll_follow_runner()
    if SITE.exists():
        shutil.rmtree(SITE)  # our own scratch mirror, rebuilt every run
    SITE.mkdir(parents=True)
    for entry in webui.iterdir():
        os.symlink(entry, SITE / entry.name)
    for f in ("mock_fetch.js", "probe_scroll.js"):
        os.symlink(SCROLL_FOLLOW / f, SITE / f)
    index = (webui / "index.html").read_text()
    m = sf.APP_ENTRY_RE.search(index)
    if m is None:
        print("harness error: no app.js entry tag in index.html", file=sys.stderr)
        sys.exit(2)
    (SITE / "probe.html").write_text(index[: m.start()] + sf.PROBE_ENTRY + index[m.end():])
    if patch_keepat:  # --red=keepat: a patched COPY in the probe site only — the tree is untouched
        src = (webui / "follow.js").read_text()
        hook = "export function keepAt(node, fn) {"
        if hook not in src:
            print("harness error: --red=keepat: follow.js has no keepAt() to disable", file=sys.stderr)
            sys.exit(2)
        (SITE / "follow.js").unlink()
        (SITE / "follow.js").write_text(src.replace(hook, hook + " return fn();"))


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


def serve_site() -> tuple[ThreadingHTTPServer, int]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(SITE)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def chrome_path() -> str:
    builds = sorted(glob.glob(os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium_headless_shell-*/"
        "chrome-headless-shell-mac-arm64/chrome-headless-shell")))
    if not builds:
        print("harness error: no cached chrome-headless-shell", file=sys.stderr)
        sys.exit(2)
    return builds[-1]


def launch(pw, engine: str):
    return pw.webkit.launch() if engine == "webkit" else pw.chromium.launch(executable_path=chrome_path())


def boot(browser, port: int, vp: dict, cpu: int | None, css: str | None = None):
    """A fresh probe page with the instrument injected. Touch-enabled, so
    page.touchscreen.tap works (a WebKit touch DRAG needs dispatch_event)."""
    page = browser.new_page(viewport=vp, has_touch=True)
    errs: list[str] = []
    page.on("pageerror", lambda e: errs.append(str(e)))
    page.goto(f"http://127.0.0.1:{port}/probe.html", wait_until="load")
    page.wait_for_function("() => window.__probe && window.__probe.ready === true", timeout=20000)
    if cpu:
        page.context.new_cdp_session(page).send("Emulation.setCPUThrottlingRate", {"rate": cpu})
    if css:
        page.add_style_tag(content=css)
    page.evaluate((HERE / "instrument.js").read_text())
    return page, errs


def send(page, scenario: str) -> None:
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [scenario, f"sid-{scenario}"])
    page.fill("#chat-input", "go")
    page.click("#chat-send")


class Rows:
    """The suites' one output contract (read by run_probe.py): each check is a
    `PASS <name> | <json>` / `FAIL <name> | <json>` row, each reported number a
    `MEASURE <key> = <json>` line, and the suite ends with exactly one
    `PROBE-RESULT: PASS|FAIL (N failing)` line."""

    def __init__(self) -> None:
        self.fails: list[str] = []

    def check(self, name: str, ok: bool, detail=None) -> bool:
        print(f"{'PASS' if ok else 'FAIL'} {name} | {json.dumps(detail, ensure_ascii=False, default=str)[:600]}",
              flush=True)
        if not ok:
            self.fails.append(name)
        return ok

    def verdict(self, r: dict, name: str, ok: bool, detail) -> None:
        """toggles/taps shape: also record the row into the result dict `r`."""
        r.setdefault("checks", {})[name] = "PASS" if ok else "FAIL"
        self.check(name, ok, detail)

    @staticmethod
    def measure(key: str, value) -> None:
        print(f"MEASURE {key} = {json.dumps(value, default=str)}", flush=True)

    def finish(self) -> int:
        print(f"PROBE-RESULT: {'FAIL' if self.fails else 'PASS'} ({len(self.fails)} failing)", flush=True)
        return 1 if self.fails else 0


def spawn_server(script: str, webui: Path, stderr_name: str):
    """Start a real-gateway probe server (`uv run python <script>`); returns
    (proc, base_url, stderr_file). The server prints `PORT <n>` once listening.
    PROBE_WEBUI makes it serve another web UI copy (the red runs)."""
    env = dict(os.environ)
    if webui != WEBUI:
        env["PROBE_WEBUI"] = str(webui)
    WORK.mkdir(parents=True, exist_ok=True)
    errlog = open(WORK / stderr_name, "w")
    proc = subprocess.Popen(["uv", "run", "python", str(HERE / script)], cwd=REPO, env=env,
                            stdout=subprocess.PIPE, stderr=errlog, stdin=subprocess.DEVNULL, text=True)
    line = proc.stdout.readline()
    if not line.startswith("PORT "):
        proc.kill()
        errlog.close()
        print(f"harness error: {script} did not start: {line!r} (stderr: {WORK / stderr_name})",
              file=sys.stderr)
        sys.exit(2)
    port = int(line.split()[1])
    if port == 8787:  # never the live agentd's port
        proc.kill()
        sys.exit("harness error: probe server bound 8787")
    return proc, f"http://127.0.0.1:{port}/", errlog


def stop_server(proc, errlog) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    errlog.close()
