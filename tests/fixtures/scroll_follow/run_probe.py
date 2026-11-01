#!/usr/bin/env python3
"""BK-017 scroll-follow repro probe (plan bk-017-scroll-follow, Step 1).

Reproduces "the chat snaps to the bottom when a streamed answer finishes"
headlessly, over the REAL webui modules and the REAL app.js boot:

  * the site is the real webui dir mirrored with symlinks (pwa_resume
    precedent), the app.js entry tag swapped for mock_fetch.js +
    probe_scroll.js, served over http:// (ES modules reject file://);
  * the user path is real: Playwright types into #chat-input, clicks Send,
    performs a real mouse-wheel gesture over #chat-log — app.js's submit
    path runs against a MOCKED /api/chat SSE (no daemon, no token, no
    model; the mock models the store's timing: the assistant row lands when
    the call returns);
  * REAL TIME only: the reveal pacing, the spring, and the paced-reveal lag
    are all rAF-driven, and rAF never fires under --virtual-time-budget
    (learnings 2026-10-16). Playwright drives the CACHED
    chrome-headless-shell via executable_path; networkidle never fires on
    this app, so waits are load + explicit conditions.

Engines: chromium (cached headless shell) always; WEBKIT — the shipping
engine for the reporter's iPad/iPhone PWAs — via Playwright's cached build
whenever `python3 -m playwright install webkit` has been run; a missing
WebKit is a recorded SKIP, never a FAIL.

Scenarios (one page PER ENGINE, sequential; each prints a TELEMETRY block):
  trigger       thinking + long GFM-table answer; modest wheel-up mid-stream;
                completion over live SSE. For the view this parks (hundreds of
                px up, growing content) chromium stays put: no scroll call
                while unpinned, and the done frame GROWS (+77 px of paced
                reveal) so there is no clamp. That is what makes the two
                repros below the named mechanisms rather than a generic
                completion scroll — it is NOT a ruling-out of the shrink-clamp
                candidate (b), which the shrink-* scenarios construct below.
  trigger-list  the bullet-list variant (the board names lists too).
  control       same length/pacing, plain prose, no thinking strip — flat.
  keyboard      table answer; scroll up with the KEYBOARD (focus a think-head
                 + PageUp: follow.js used to unpin on wheel/touch ONLY).
                 Candidate (c). Post-fix: the scroll handler's direction +
                 write-attribution rule unpins, the spring goes silent, and
                 completion leaves the reader up (pre-fix: stale-pinned spring
                 yanked to the bottom — the numbers are in probe-before.log).
  heal          reopen the finished trigger session, wheel up, arm
                 ui.resync + deliver run_done through the events.js sink.
                 Candidate (a). Post-fix: renderSession(preserveRead) restores
                 the read position inside its atomic swap (restoreRead write,
                 pill shown, pinned false) instead of the unconditional jump.
                 GUARD guard-heal-following: the same heal with NO gesture — the
                 follower must still land at the healed bottom (preserveRead is
                 not "always preserve", wizard Q3.6).
  shrink-close  candidate (b), the gateway's NORMAL strip close: a second
                 thinking strip collapses on its thinking_done frame — a
                 pure ~47.5 px content shrink with no other DOM change in
                 that frame. The view is parked UNPINNED a few px above the
                 bottom with fine real-wheel steps (no JS write) during a
                 static window, then the close lands. Clamp signature
                 asserted: the 8 ms sampler sees scrollTop move to the new
                 maxTop with ZERO scrollTop-setter writes in the window.
  shrink-delta  the second burst closed by the FIRST TEXT DELTA of the next
                 segment instead (paintStream's dropped-thinking_done
                 guard) — the shrink lands in the same frame the next
                 segment starts growing.
  shrink-turn   the strip is still open when turn_complete arrives:
                 closeSegments() collapses it AT the completion boundary.
                 All three assert the done frame itself stays flat — the
                 clamp lives at the segment boundary, not in the done path.
  status-clamp  no thinking strip: turn_complete hides the IN-FLOW #chat-status
                 row, so #chat-log grows ~28 px taller and maxTop drops by that
                 much — a VIEWPORT height change (clientHeight), which a
                 content-shrink-only compensation cannot see. Post-fix the hide
                 carries the keepRead compensation write and the parked gap
                 survives instead of the view clamping to the new bottom.
  stop-stopped  wheel up mid-run, then the run ends on `stopped` (app.js's
  stop-error  `stopped`/`error` handlers → addSystemMsg/addErrorMsg). The
                 terminal line lands WITHOUT a jump for an up-scrolled reader
                 (the wizard's gating call; addUserMsg's send-jump stays
                 unconditional).
  status-sub    the #chat-status SUB-line (its own flex line inside the
                 in-flow row): shown by the 20 s slow-hint tick (the probe's
                 shiftClock jumps Date.now +21 s once the app has PROCESSED
                 the model_wait frame, so the tick lands in seconds, not 20
                 s), hidden by the next phase, then the whole row hides at
                 turn_complete — three clientHeight toggles mid-run. SHOW
                 and HIDE only move the viewport (the parked view must not
                 move and nothing may write); the ROW HIDE grows the
                 viewport onto the restored 4 px gap, so the clamp bites and
                 the keepRead write must carry the top down WITH maxTop.
  gate-followed the fake-scroller gate pin (criterion 3): seed 40 rows, then
  gate-unpinned drive the REAL paint paths (dispatchStream delta ticks + the
                 finalizeAgentBubble flush) with NO SSE — unfollowed the view
                 must not move at all (zero writes), followed it must track
                 the bottom and settle there. One predicate, both paths.
  follow-shrink  GUARD (green before AND after): the shrink-close shape with a
                 FOLLOWER — a mid-answer strip collapse must not read as user
                 intent, so a following view never unpins or shows the pill.
  follow        the table answer, NEVER scrolled — must settle at the bottom
                with the pill never shown (the following half of criterion 2b).
  short         short plain reply, shorter than the log — nothing to snap.

Exit codes (pwa_resume precedent):
  0  every check PASS on every engine that ran — the POST-FIX contract (the
      README's flip matrix): the heal re-renders IN PLACE, keyboard scroll-up
      unpins and completion leaves it up, every shrink/viewport height change
      carries the named compensation write and the parked read position
      survives, no post-collapse carry-repin on EITHER engine, stopped/error
      land flat for an up-scrolled reader, the sub-line toggles compensate,
      the one gate governs both paint paths — while the followed answer still
      settles at the bottom (guard-follow-pinned, live-path-flat*,
      control-plain-flat, *-done-flat, short-reply-fits unchanged) — plus the
      Step-3 acceptance matrix (criterion 2): six answer shapes x unfollowed/
      followed x desktop/iPhone ports x both engines, the criterion-4
      regression guards (send-jump, openSession-jump, picker-jump alongside
      guard-heal-following and the stop-* flat+landed pair), the two
      overflow-anchor residual measurements behind the keep/go call, and the
      BK-019 recover family: the CHAT transport's five runstate recovery
      paths (T15 socketDied incl. T16 catchingUp + the watchdog verdicts,
      T03 screenLeave, T12 sendRejected, T13 sendFailed, T14
      streamEndSilent) asserted on OBSERVABLE outcomes over an honest
      server-side run
  1  probe ran, some check FAILed
  2  harness broke: no playwright, jsc parse-check failed, no cached
     chrome-headless-shell, the app never booted, a scenario timed out, or a
     results block that could not have come from the full matrix (the
     per-engine assertion floor / cross-engine set-identity guards —
     pwa_resume's precedent, see MIN_UNIQUE_PER_ENGINE)

Flags (all optional; the human re-run path):
  --only=mechanisms,matrix,guards,anchors,recover   run only these families
  --engines=chromium,webkit                 run only these engines
  --ports=desktop,iphone                    run only these ports
  (used for red-discipline runs: --only=recover --engines=chromium
   --ports=desktop with a planted src sabotage; mechanisms and recover are
   desktop-port families)
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent                  # tests/fixtures/scroll_follow
REPO = Path(__file__).resolve().parents[3]
WEBUI = REPO / "src" / "harness" / "gateway" / "webui"
WORK = REPO / "scratch" / "scroll_follow_probe"         # gitignored; rebuilt each run
SITE = WORK / "site"
JSC = Path(
    "/System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc"
)
# The app.js ENTRY TAG only — version-tolerant for the same reason as
# tests/fixtures/pwa_resume/run_probe.py: the webui convention bumps `?v=` on
# every asset edit. Pinned by tests/test_scroll_follow_probe.py.
APP_ENTRY_RE = re.compile(r'<script[^>]*\bsrc="/app\.js(?:\?v=\d+)?"[^>]*>\s*</script>')
PROBE_ENTRY = (
    '<script src="/mock_fetch.js"></script>\n'
    '  <script type="module" src="/probe_scroll.js"></script>'
)
SETTLE_MS = 900          # after the done frame: spring/scroll settle window
# A modest scroll-up — the board's shape is "read the last rows while it
# finishes", tens of px, LESS than the completion shrink; a big scroll-up
# (beyond the shrink) legitimately stays put and is not the bug.
WHEEL_TRIGGER = -40      # px wheelled up mid-stream (trigger/control)
WHEEL_MIN_GAP = 30       # px above bottom the wheel must achieve (unpin proof)
WHEEL_HEAL = -600        # px scrolled up before the heal re-render
# Park for the shrink scenarios: fine REAL-wheel steps (both calibrated
# engines move ~|dy| px per wheel event; the probe still asserts the achieved
# gap, never a px promise). Upward steps never re-pin (follow.js's maybeRepin
# only reacts to downward movement), so a few px above the bottom is a stable
# UNPINNED state — exactly how a real reader stops after a small scroll-up.
WHEEL_FINE = -2
PARK_TARGET_GAP = 4      # px above the bottom the fine-wheel loop aims for
PARK_MAX_STEPS = 4
# D2 (BK-030): the streaming strip's live tail is gone, so a segment close no
# longer shrinks. The shrink-* parks still sit inside the distance the OLD
# tail collapse (CSS max 3.8em ~= 47.5 px) would have clamped — so a change
# that brings any segment-close shrink back reddens `*-flat` there.
PRE_D2_TAIL_PX = 47.5

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright
    IMPORT_ERR: Exception | None = None
except ImportError as exc:  # tests/test_scroll_follow_probe.py imports THIS
    # module with no interpreter-side playwright (uv venv) — the import must
    # stay side-effect-free; main() exits 2 with the recipe below.
    PlaywrightError = PWTimeout = sync_playwright = None  # type: ignore[assignment]
    IMPORT_ERR = exc


class HarnessError(RuntimeError):
    """The probe could not reach the state a check needs — exit 2, not FAIL."""


def build_site() -> None:
    """Mirror the webui dir with symlinks; swap the entry tag in index.html."""
    if SITE.exists():
        shutil.rmtree(SITE)  # our own scratch mirror, rebuilt every run
    SITE.mkdir(parents=True)
    for entry in WEBUI.iterdir():
        os.symlink(entry, SITE / entry.name)
    for fixture in ("mock_fetch.js", "probe_scroll.js"):
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
        [str(JSC), "tests/fixtures/scroll_follow/jsc_check.js"],
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


def webkit_ready() -> str | None:
    """None when Playwright's cached WebKit exists, else why it can't run.
    WebKit is the shipping engine for the reporter's iPad/iPhone PWAs, so the
    matrix runs it whenever `python3 -m playwright install webkit` has been
    run — a missing install is a recorded skip, not a failure (plan Step 1)."""
    if not glob.glob(os.path.expanduser("~/Library/Caches/ms-playwright/webkit-*/")):
        return ("no cached WebKit under ~/Library/Caches/ms-playwright/webkit-* — "
                "install with: python3 -m playwright install webkit")
    return None


def launch_engine(pw, engine: str):
    if engine == "chromium":
        return pw.chromium.launch(executable_path=chrome_binary(), args=["--no-sandbox"])
    if engine == "webkit":
        return pw.webkit.launch()  # Playwright's own cached build, no executable_path
    raise HarnessError(f"unknown engine {engine!r}")


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # silence per-request stderr noise
        pass


# ── scenario plumbing ────────────────────────────────────────────────────────

class Checks:
    def __init__(self):
        self.lines = []
        self.skips = []

    def check(self, name, ok, detail=""):
        detail = " | ".join(str(detail).split())  # never a raw newline in a line
        line = f"{'PASS' if ok else 'FAIL'} {name}" + (f" | {detail}" if detail else "")
        self.lines.append(line)
        print(line, flush=True)  # visible even if a later scenario wedges
        return ok

    def skip(self, name, reason=""):
        """A check/engine that COULD NOT run (missing WebKit install, an
        engine-specific stand-in). Printed and counted, never a FAIL."""
        reason = " | ".join(str(reason).split())
        line = f"SKIP {name}" + (f" | {reason}" if reason else "")
        self.skips.append(line)
        self.lines.append(line)
        print(line, flush=True)
        return False

    @property
    def fails(self):
        return [l for l in self.lines if l.startswith("FAIL")]


def metrics(page):
    return page.evaluate("() => window.__probe.metrics()")


def send_message(page, text):
    page.fill("#chat-input", text)
    page.click("#chat-send")


def wait_log_tall(page, min_extra=300, timeout=15000):
    page.wait_for_function(
        "(n) => { const l = document.querySelector('#chat-log'); "
        "return l.scrollHeight - l.clientHeight > n; }",
        arg=min_extra, timeout=timeout,
    )


def hover_log(page):
    box = page.locator("#chat-log").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


def wheel_up(page, dy, tries=3):
    """Wheel the log up by dy, re-aiming the pointer until the scroller really
    moved. Playwright dispatches a wheel at the current POINTER, and two runs
    left the log untouched while the gesture was meant to land (WebKit moved
    the page body 3 px; chromium did not unpin at all) — an input-delivery
    race, not a product result. Callers still assert the achieved gap; this
    only makes the gesture land."""
    top0 = page.evaluate("() => document.querySelector('#chat-log').scrollTop")
    page.evaluate("() => window.__probe.mark('wheelAt')")
    for _ in range(tries):
        page.mouse.wheel(0, dy)
        page.wait_for_timeout(50)
        top1 = page.evaluate("() => document.querySelector('#chat-log').scrollTop")
        # Direction-aware: a still-gliding spring writes DOWNWARD, so `!=`
        # would read the spring as a landed up-wheel.
        moved = top1 < top0 if dy < 0 else top1 > top0
        if moved:
            break
        hover_log(page)  # the wheel went elsewhere — re-aim at the scroller
        page.wait_for_timeout(20)
    page.evaluate("() => window.__probe.mark('wheelDone')")


def wait_done(page, timeout=30000, label="done event"):
    # 30 s: this wait covers the whole answer (the mock stamps doneAt at the
    # done FRAME), so a slow shape's tail rides here — the 20 s budget predates
    # the long matrix scripts (shape-mixed's list+table tail, 2026-10-29).
    try:
        page.wait_for_function("() => window.__mock.doneAt !== null", timeout=timeout)
    except PlaywrightError as exc:
        raise HarnessError(f"timed out waiting for {label}: {exc}") from None


# Per-script tall threshold (px ABOVE clientHeight the log must reach before
# the probe acts). 300 suits the long shapes; shape-code's fenced block is the
# one shape that does NOT overflow the desktop log by 300 (its <pre> stops at
# ~318 px on desktop — measured 2026-10-29), and a gesture the spring can
# still outrun is not the state (a) under test. Measured desktop overflows:
# shape-code ~318 (desktop) / ~1000 (iphone 390 px wrap).
TALL_H = {"shape-code": 200, "shape-mixed": 120}


def wait_log_tall_ctx(page, label, min_extra=None, timeout=25000):
    """`label` doubles as the script name: TALL_H keys off it."""
    n = TALL_H.get(label, 300 if min_extra is None else min_extra)
    try:
        wait_log_tall(page, min_extra=n, timeout=timeout)
    except PlaywrightError as exc:
        raise HarnessError(f"timed out waiting for tall log ({label}, +{n}px): {exc}") from None


def collect(page):
    return page.evaluate("() => window.__probe.collect()")


def around(tel, lo, hi):
    """Samples with doneAt+lo <= t <= doneAt+hi (telemetry table)."""
    d = tel["doneAt"]
    return [s for s in tel["samples"] if d + lo <= s["t"] <= d + hi]


def pre_post(tel, settle=SETTLE_MS):
    """State just before the done frame vs after the settle window.

    doneAt is stamped by the MOCK at the done frame's EMIT instant
    (mock_fetch.js), and the app's SSE reader processes that frame in a
    LATER task — so every sample with t <= doneAt is by construction before
    the done path's first effect, whatever writes sit between the sample and
    doneAt (those are the streaming spring, i.e. the pre-done state itself).
    An earlier revision demanded a write-free pre sample on a poll-granularity
    theory of doneAt; with the in-page emit stamp that rule only starves
    scenarios whose spring writes every frame (WebKit's keyboard run crashed
    with no clean sample within reach)."""
    d = tel["doneAt"]
    pre = None
    for s in tel["samples"]:
        if s["t"] <= d:
            pre = s
        else:
            break
    post = None
    for s in tel["samples"]:
        if d <= s["t"] <= d + settle:
            post = s
    if pre is None or post is None:
        raise RuntimeError("scenario sampled too few points around done")
    return pre, post


def writes_in(tel, lo, hi, settle=SETTLE_MS):
    d = tel["doneAt"] if lo is not None else None
    if d is None:
        raise RuntimeError("no doneAt")
    return [w for w in tel["writes"] if d + lo <= w["t"] <= d + hi]


def in_window(seq, center, lo, hi):
    """Telemetry entries (anything with a "t") inside center+lo .. center+hi."""
    return [x for x in seq if center + lo <= x["t"] <= center + hi]


def wait_log_static(page, quiet_ms=300, timeout=8000):
    """Wait until the paced reveal has drained — the log's scrollHeight AND
    the live bubble's rendered text length both stop changing. Height alone is
    not enough: a reveal painting more characters into a line that still fits
    leaves scrollHeight flat, then wraps 20+ px later (observed: the parked
    gap drifted 4 → 26 px before the completion frame). The status-clamp
    scenario's park must HOLD until the tagged frame."""
    last = None
    stable_since = 0.0
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        state = page.evaluate(
            "() => { const l = document.querySelector('#chat-log'); "
            "const mds = l.querySelectorAll('.msg.agent .md'); "
            "return [l.scrollHeight, mds.length ? mds[mds.length - 1].textContent.length : 0]; }"
        )
        now = time.monotonic()
        if state != last:
            last, stable_since = state, now
        elif (now - stable_since) * 1000 >= quiet_ms:
            return
        page.wait_for_timeout(50)
    raise HarnessError("the log never went static (reveal still painting)")


def shrink_measure(tel, shrink_at):
    """The clamp arithmetic, anchored on the shrink frame's EMIT instant:
    the last pre-shrink sample, the height floor in the shrink window, the
    first post-shrink sample that shows the drop, the setter writes and the
    (browser-dispatched) scroll events in the same window."""
    samples = tel["samples"]
    pre = None
    for s in samples:
        if s["t"] <= shrink_at:
            pre = s
    window = in_window(samples, shrink_at, -8, 250)
    h_min = min((s["h"] for s in window), default=(pre or {"h": 0})["h"])
    shrink_px = (pre["h"] - h_min) if pre else 0.0
    first_drop = None
    if pre is not None and shrink_px >= 4:
        for s in window:
            if s["t"] >= shrink_at and s["h"] <= pre["h"] - max(4, shrink_px / 2):
                first_drop = s
                break
    return {
        "pre": pre,
        "window": window,
        "shrinkPx": shrink_px,
        "firstDrop": first_drop,
        "topDrop": (pre["top"] - first_drop["top"]) if pre and first_drop else 0.0,
        "writes": in_window(tel["writes"], shrink_at, -8, 250),
        # Writes that could have MOVED the top by the time the drop sample
        # shows it: the clamp signature is about THIS window, not everything
        # that happens later (a repin after the clamp is a different act).
        "clampWrites": [w for w in tel["writes"]
                        if shrink_at - 8 <= w["t"] <= (first_drop or {"t": shrink_at + 250})["t"]],
        "scrolls": in_window(tel["scrolls"], shrink_at, -8, 250),
    }


def max_measure(tel, at, base):
    """The clamp arithmetic for a VIEWPORT (clientHeight) change, measured on
    maxTop (h - ch) instead of scrollHeight: the completion boundary also
    changes the viewport (#chat-status, an in-flow row, hides at turn_complete),
    so maxTop drops while scrollHeight stays — the same browser clamp, a
    different cause, and a scrollHeight-only window would miss it.

    `base` is the parked metrics: the view is static between the park and the
    frame, so the drop is found against THAT maxTop and attributed to the
    tagged frame by `atGap` (first sample after base whose maxTop fell by ≥12
    px, required to sit near the tag) — no dependence on which side of the
    tag's emit stamp the sampler's tick landed (WebKit run: the tick sat
    11 ms after it; a pre-sample rule then read the post-clamp state)."""
    samples = tel["samples"]
    pre_max = base["h"] - base["ch"]
    first = None
    for s in samples:
        if s["t"] > base["t"] and (s["h"] - s["ch"]) <= pre_max - 12:
            first = s
            break
    pre = None
    for s in samples:
        if first is None:
            break
        if s["t"] < first["t"]:
            pre = s
        else:
            break
    window = in_window(samples, at, -8, 250)
    max_min = min((s["h"] - s["ch"] for s in window), default=pre_max)
    return {
        "pre": pre,
        "window": window,
        "first": first,
        "atGap": (first["t"] - at) if first is not None else None,
        "maxDrop": (pre_max - (first["h"] - first["ch"])) if first is not None
                   else (pre_max - max_min),
        "hDrop": ((base["h"] - first["h"]) if first is not None
                  else (base["h"] - min((s["h"] for s in window), default=base["h"]))),
        "topDrop": (pre["top"] - first["top"]) if pre and first else 0.0,
        "writes": in_window(tel["writes"], at, -8, 250),
        "clampWrites": [w for w in tel["writes"]
                        if (pre["t"] if pre else at) <= w["t"]
                        <= (first["t"] if first else at + 250)],
        "scrolls": in_window(tel["scrolls"], at, -8, 250),
    }


def park_near_bottom(page):
    """Park the view UNPINNED a few px above the bottom with fine REAL-wheel
    steps only — no JS write, so the shrink window's zero-writer assertion
    stays clean (the park writes nothing the spy could log)."""
    hover_log(page)
    page.evaluate("() => window.__probe.mark('parkWheel')")
    gap = None
    for _ in range(PARK_MAX_STEPS):
        page.mouse.wheel(0, WHEEL_FINE)
        page.wait_for_timeout(60)
        m = metrics(page)
        gap = m["h"] - m["ch"] - m["top"]
        if gap >= PARK_TARGET_GAP:
            break
    return metrics(page)


def key_toggle_last(page, sel):
    """Keyboard-activate the LAST control matching `sel`, focused without a
    focus scroll (a programmatic move is not the reader's) — no press, so no
    press-unpin (BK-029)."""
    page.evaluate("(s) => [...document.querySelectorAll(s)].pop().focus({ preventScroll: true })", sel)
    page.keyboard.press("Enter")


def wheel_to_bottom(page):
    """Real wheel DOWN to the log's bottom, aimed at #chat-log's own padding.
    Before BK-031 an open .think-body / .pill-detail was an inner scroller
    (overscroll-behavior: contain) that ate the wheel; they flow at full
    height now ("Show all"), and the aim simply stays put."""
    box = page.locator("#chat-log").bounding_box()
    page.mouse.move(box["x"] + 6, box["y"] + box["height"] / 2)
    for _ in range(6):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(150)
        m = metrics(page)
        if m["h"] - m["ch"] - m["top"] <= 1:
            break
    page.wait_for_timeout(200)


def scenario_shrink(page, name, sid, park_tag="thinkB", static_first=False, park=True):
    """Run a shrink scenario: submit, wait for the park frame (the second
    thinking strip, or `park_tag`), park the unpinned view near the bottom
    while the log is STATIC, let the tagged shrink frame land, snap the
    clamped state, then ride out to done. Returns (telemetry, states)."""
    print(f"scenario {name}: start", flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [name, sid])
    send_message(page, f"Probe {name}: shrink shape")
    try:
        page.wait_for_function("(tag) => window.__mock.tagAt[tag] !== undefined",
                               arg=park_tag, timeout=25000)
    except PlaywrightError as exc:
        raise HarnessError(f"timed out waiting for the park frame {park_tag!r} "
                           f"({name}): {exc}") from None
    page.wait_for_timeout(500)  # tail paint + reveal drain + spring settle
    wait_log_tall_ctx(page, name)
    if static_first:
        wait_log_static(page)  # the park must HOLD until the tagged frame
        # ...and the spring must have finished gliding to the bottom: an
        # unpin mid-glide leaves the view short of the bottom, so the park
        # would start 20+ px up and the clamp would barely bite.
        try:
            page.wait_for_function(
                "() => { const l = document.querySelector('#chat-log'); "
                "return l.scrollTop >= l.scrollHeight - l.clientHeight - 2; }",
                timeout=5000)
        except PlaywrightError as exc:
            raise HarnessError(f"the spring never settled at the bottom "
                               f"({name}): {exc}") from None
    tail = page.evaluate("() => window.__probe.tailInfo()")
    if park:
        parked = park_near_bottom(page)
    else:
        # park=False: a FOLLOWER (no gesture) rides the same collapse. Its
        # spring must have SETTLED at the bottom first, so the shrink is the
        # only thing that moves the view in the measured window
        # (guard-follow-through-shrink: the clamp must not read as user intent).
        wait_log_static(page)
        page.wait_for_function(
            "() => { const l = document.querySelector('#chat-log'); "
            "return l.scrollTop >= l.scrollHeight - l.clientHeight - 2; }", timeout=5000)
        parked = metrics(page)
    try:
        page.wait_for_function("() => window.__mock.tagAt.shrink !== undefined", timeout=8000)
    except PlaywrightError as exc:
        raise HarnessError(f"timed out waiting for the shrink frame ({name}): {exc}") from None
    shrink_at = page.evaluate("() => window.__mock.tagAt.shrink")
    page.wait_for_timeout(220)  # the clamp frame + a couple of settles
    snapped = metrics(page)
    wait_done(page)
    page.wait_for_timeout(SETTLE_MS)
    return collect(page), {"tail": tail, "parked": parked,
                           "shrinkAt": shrink_at, "snapped": snapped}


def telemetry_table(tel, tag):
    print(f"TELEMETRY {tag}: doneAt={tel['doneAt']:.0f} "
          f"(t offset | scrollTop | scrollHeight | pinned | pill)")
    for s in around(tel, -800, 1000):
        if (s["t"] - tel["doneAt"]) % 40 < 9:  # ~one row per 40 ms
            print(f"  t={s['t'] - tel['doneAt']:+6.0f} top={s['top']:7.1f} "
                  f"h={s['h']:6} pinned={str(s['pinned']):5} pill={s['pill']}")
    for w in writes_in(tel, -800, 1000):
        print(f"  WRITE t={w['t'] - tel['doneAt']:+6.0f} {w['from']} -> {w['to']} | {w['stack']}")


def scenario_stream(page, name, sid, wheel_dy=None, before_done=None,
                   settle=SETTLE_MS):
    """Reset, arm a script, submit through the real form, optional real
    wheel-up mid-stream, wait for done + settle. `before_done(page)` runs
    after the log is tall (extra gestures). Returns (telemetry, states) where
    states = {tall: metrics-at-tall, wheel: metrics-after-the-wheel}."""
    print(f"scenario {name}: start", flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [name, sid])
    send_message(page, f"Probe {name}: compare the candidates")
    states = {}
    if wheel_dy is not None or before_done is not None:
        wait_log_tall_ctx(page, name)
        states["tall"] = metrics(page)
    if wheel_dy is not None:
        hover_log(page)
        wheel_up(page, wheel_dy)
        states["wheel"] = metrics(page)  # post-wheel state BEFORE anything else moves
    if before_done is not None:
        before_done(page)
    wait_done(page)
    page.wait_for_timeout(settle)
    return collect(page), states


def scenario_mixed_over_history(page, sid, engine, port, cdp):
    """Matrix state (a) for shape-mixed — the table block AT COMPLETION over a
    view that has scroll room. Paced reveal is OFF for this shape (the one
    probe-gated src switch, transcript.js tick), which is what makes the whole
    table land in the finalize render — but it also means the log renders
    NOTHING mid-stream: a fresh session has no scroll room to park in, and
    the tall-gate would only satisfy after done (measured: the wheel landed
    385 ms after the done frame, 2026-10-29). So this shape's state (a) runs
    over the HISTORY transcript, like the short shape's: real scroll room,
    real seam (the mock's ~1.4 s pause before the table frames), and the
    done-time render grows the view under a parked, unpinned reader."""
    print("scenario shape-mixed: start (over history, touch/wheel per port)",
          flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(id) => window.__probe.openSession(id)", sid)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)  # the open's jump settles before arming
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["shape-mixed", sid])
    base = page.evaluate("() => document.querySelectorAll('#chat-log .msg.agent').length")
    send_message(page, "Probe shape-mixed: table at completion, view parked")
    wait_park_ready(page, [base])  # NEW bubble streaming (history-safe gate)
    wait_log_static(page)        # and the DOM is at rest: the seam
    states = {"tall": metrics(page)}
    if port == "iphone":
        box, cx, cy = log_center(page)
        page.evaluate("() => window.__probe.mark('tapAt')")
        page.touchscreen.tap(cx, cy)
        page.wait_for_timeout(120)
        states["unpin"] = metrics(page)
        touch_drag = engine == "chromium" and cdp is not None
        scroll_up_gesture(page, engine, port, cdp, DRAG_DIST, min_gap=MIN_STATE_GAP)
        states["gesture"] = ("tap+cdp-drag" if touch_drag
                             else "tap+wheel-position(webkit-no-drag)")
    else:
        states["unpin"] = states["wheel"] = scroll_up_gesture(
            page, engine, port, cdp, abs(WHEEL_SHORT_A), min_gap=MIN_STATE_GAP)
        states["gesture"] = "wheel"
    states["moved"] = metrics(page)
    wait_done(page)
    page.wait_for_timeout(SETTLE_MS)
    return collect(page), states


# ── BK-017 Step-2 scenarios ──────────────────────────────────────────────────

WHEEL_STOP_UP = -120   # px scrolled up before a stopped/error terminal frame
GATE_SEED_ROWS = 40    # seeded transcript rows for the fake-scroller gate pin
GATE_PAINT = "\n".join(f"gate line {i}: filler filler filler filler"
                       for i in range(30))


def scenario_stop_up(page, name, sid):
    """BK-017 stop-up: wheel up mid-run; the run ENDS on the terminal frame
    (stopped → addSystemMsg("Stopped"), error → addErrorMsg — both used to end
    in an unconditional jumpToBottom). doneAt is stamped by the mock on the
    stopped/error EMIT (mock_fetch.js), so pre/post/done-flat semantics carry
    over from the done-frame scenarios unchanged."""
    print(f"scenario {name}: start", flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [name, sid])
    send_message(page, f"Probe {name}: terminal frame while scrolled up")
    # Wheel on the LAST PROSE DELTA (tag "preTerm"), not on log height: the
    # paced reveal keeps the log under any tall-threshold until the terminal
    # frame's own flush (measured: +328 px at stopped) — a height cue would
    # land the wheel after the frame under test.
    try:
        page.wait_for_function("() => window.__mock.tagAt.preTerm !== undefined",
                               timeout=25000)
    except PlaywrightError as exc:
        raise HarnessError(f"timed out waiting for the pre-terminal frame "
                           f"({name}): {exc}") from None
    page.wait_for_timeout(80)  # the delta's first reveal tick paints
    states = {"tall": metrics(page)}
    hover_log(page)
    wheel_up(page, WHEEL_STOP_UP)
    states["wheel"] = metrics(page)
    wait_done(page, label=f"terminal frame of {name}")
    page.wait_for_timeout(SETTLE_MS)
    return collect(page), states


def scenario_status_sub(page, name, sid):
    """BK-017 status-sub: park the UNPINNED view a few px above the bottom,
    then ride the three #chat-status clientHeight toggles —

      sub SHOW   the mock's model_wait frame arms the waiting phase; the
                 probe's shiftClock (called when the app has PROCESSED that
                 frame — the status label flip is show()'s synchronous
                 signature; stamping off tagAt's EMIT loses the race and the
                 hint never fires) turns status.js's 20 s slow-hint threshold
                 into the next 1 s tick. The toggle lands on that TICK, not
                 on the frame, so its instant comes from the probe's
                 MutationObserver on #chat-status-sub (window.__T.subs),
                 clientHeight included; it SHORTENS the viewport (maxTop
                 rises — no clamp possible), so the contract is: unmoved,
                 silent, gap grown by the viewport change;
      sub HIDE   thinking_done's phase("Deciding next step…") hides the sub with ZERO
                 content growth: viewport grows back, the widened gap
                 absorbs it — still unmoved, still silent;
      row HIDE   turn_complete (tag "shrink") hides the whole row onto the
                 RESTORED ~4 px gap — the clamp bites and the keepRead
                 compensation write must carry the top down WITH maxTop.

    Returns telemetry + {parked, shown, hiddenEv, tagAt}."""
    print(f"scenario {name}: start", flush=True)

    def wait_tag(tag, what):
        try:
            page.wait_for_function("(t) => window.__mock.tagAt[t] !== undefined",
                                   arg=tag, timeout=25000)
        except PlaywrightError as exc:
            raise HarnessError(f"timed out waiting for the {what} ({tag}, {name}): {exc}") from None

    page.evaluate("() => window.__probe.reset()")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [name, sid])
    send_message(page, f"Probe {name}: status sub-line toggles")
    wait_tag("armPark", "park frame")
    page.wait_for_timeout(500)  # tail paint + reveal drain + spring settle
    wait_log_tall_ctx(page, name)
    wait_log_static(page)  # the park must HOLD until the toggles start
    try:
        page.wait_for_function(
            "() => { const l = document.querySelector('#chat-log'); "
            "return l.scrollTop >= l.scrollHeight - l.clientHeight - 2; }",
            timeout=5000)
    except PlaywrightError as exc:
        raise HarnessError(f"the spring never settled at the bottom ({name}): {exc}") from None
    parked = park_near_bottom(page)
    wait_tag("subArm", "model_wait frame")
    # tagAt stamps the mock's EMIT; waitingSince arms when the APP processes
    # the frame (status.js show()). Shifting before that arms waitingSince
    # WITH the shifted clock — a constant shift then subtracts out and the
    # hint never fires; shifting seconds later lets unshifted ticks run first
    # (measured, red run). The label flip is that show()'s synchronous
    # signature: shift the moment it moves.
    try:
        page.wait_for_function(
            "() => document.querySelector('#chat-status-label').textContent"
            " === 'Waiting for model…'", timeout=6000)
    except PlaywrightError as exc:
        raise HarnessError(f"the app never processed the model_wait frame "
                           f"({name}): {exc}") from None
    page.evaluate("() => window.__probe.shiftClock()")  # before the next 1 s tick
    try:
        page.wait_for_function("() => window.__T.subs.some(e => !e.hidden)", timeout=15000)
    except PlaywrightError as exc:
        raise HarnessError(f"the slow-hint sub-line never showed ({name}): {exc}") from None
    shown = page.evaluate("() => window.__T.subs.filter(e => !e.hidden)[0]")
    wait_tag("subHide", "sub-hiding delta")
    hidden_ev = page.evaluate(
        "() => { const s = window.__T.subs; const i = s.findIndex(e => !e.hidden); "
        "return s.slice(i + 1).find(e => e.hidden) || null; }")
    if hidden_ev is None:
        raise HarnessError(f"the sub-line hide never reached the MutationObserver "
                           f"({name}) — nothing to measure")
    wait_tag("shrink", "turn_complete frame")
    wait_done(page)
    page.wait_for_timeout(SETTLE_MS)
    tel = collect(page)
    tag_at = page.evaluate("() => window.__mock.tagAt")
    return tel, {"parked": parked, "shown": shown, "hiddenEv": hidden_ev, "tagAt": tag_at}


def scenario_gate(page, followed):
    """Fake-scroller gate pin (criterion 3): NO SSE — drive the REAL paint
    paths (dispatchStream → renderDelta's paced ticks, then the
    finalizeAgentBubble flush) against a seeded transcript, and pin that BOTH
    scroll paths run through the ONE isFollowing() gate: an unfollowed paint
    moves the view ZERO px with ZERO writes; a followed one tracks the bottom
    and settles there."""
    name = "gate-followed" if followed else "gate-unpinned"
    print(f"scenario {name}: start", flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(n) => window.__probe.seedTranscript(n)", GATE_SEED_ROWS)
    page.wait_for_timeout(200)
    if not followed:
        hover_log(page)
        wheel_up(page, -200)
    base = metrics(page)
    following0 = page.evaluate("() => window.__probe.following()")
    page.evaluate("() => window.__probe.mark('gatePaint')")
    page.evaluate("(t) => window.__probe.paintDelta(t)", GATE_PAINT)
    wait_log_static(page)  # the paced reveal drained
    page.evaluate("() => window.__probe.flush()")
    page.wait_for_timeout(700)  # any flush-initiated glide lands
    after = metrics(page)
    paint_at = page.evaluate("() => window.__T.meta.gatePaint")
    writes = page.evaluate("(t0) => window.__T.writes.filter(w => w.t >= t0)", paint_at)
    return name, {"base": base, "after": after, "followingAtPaint": following0,
                  "writes": writes}


# ── BK-017 Step-3: acceptance matrix + regression guards + anchor residuals ──

# The ports (plan criterion 2/5): desktop webui is the mechanism suite's home
# and one matrix column; the iPhone-sized viewport with REAL touch is the
# shipping device (iPad + iPhone home-screen PWAs). is_mobile is a Chromium
# capability (device pixel/UA emulation); WebKit runs the same viewport with
# hasTouch only — stated here so the README can state it too.
PORTS = [
    {"name": "desktop", "viewport": {"width": 1000, "height": 680},
     "has_touch": False, "is_mobile": False},
    {"name": "iphone", "viewport": {"width": 390, "height": 844},
     "has_touch": True, "is_mobile": True},
]
# shape key -> mock_fetch.js scenario name. Pinned against the mock's scenario
# table by tests/test_scroll_follow_probe.py (a renamed script must fail the
# suite, not the mid-run browser session). `short` runs OVER a history
# session: a reply shorter than the log has no scroll room of its own, so its
# state-(a) is the reporter's real shape — reading an older answer while a
# short one finishes.
MATRIX_SHAPES = [
    ("table", "trigger"),          # GFM table (the board's trigger)
    ("list", "trigger-list"),      # bullet list
    ("prose", "control"),          # long plain prose
    ("code", "shape-code"),        # fenced code block (fence re-layout at done)
    ("mixed", "shape-mixed"),      # list + table + emoji (surrogate-pair guard)
    ("short", "short-plain"),      # short plain reply over history
]
HIST_SID = "probe-mx-history"      # the session every port's matrix builds first
RC_HIST_SID = "probe-rc-history"   # recover's OWN switch-target history session:
                                   # the matrix asserts HIST_SID is absent when it
                                   # starts (fresh-page guard) — recover seeds this
                                   # id instead, so the families never collide
WHEEL_A = -120                     # px wheelled up for a matrix-a run
WHEEL_SHORT_A = -400               # px up over the history transcript
DRAG_DIST = 250                    # px of real finger travel (touch unpin proof)
                                   # — keep it INSIDE the scroller: a 400 px CDP drag
                                   # delivered 0-11 px on a long transcript (250 → 430)
MIN_STATE_GAP = 30                 # px above the bottom the unpin must achieve

FAMILIES = ("mechanisms", "matrix", "guards", "anchors", "recover")

# ── BK-019 Step 5b: the recover family (CHAT-transport recovery paths) ───────
# The mock scripts this family drives (pinned against the mock's scenario
# table by tests/test_scroll_follow_probe.py, same discipline as
# MATRIX_SHAPES). t15-die serves both T15 scenarios: it dies mid-stream AND
# finishes server-side (serverDone), so the watchdog's honest 10 s tick finds
# an ACTIVE run (gap-fill) and its next finds it gone (persisted heal).
RECOVER_SCRIPTS = ("t15-die", "t14-silent", "t03-switch", "t12-busy", "t13-netfail")
WARN_LABEL = "Reconnecting · still working"  # runstate T15's statusWarn label
CATCH_LABEL = "Catching up…"                  # runstate T16's one-time phase

_MISSING = object()  # "no arg" sentinel for poll()


def poll(page, expr, arg=_MISSING, ms=8000, step=100):
    """Bounded wait that RETURNS a bool. Every recover step uses this instead
    of wait_for_function: a condition that never lands must become a NAMED
    FAIL row the family reports, never an exit-2 harness break — the mutation
    proofs unplug transitions precisely so these conditions stop landing,
    and they have to produce named FAILs, not diagnostics."""
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        ok = page.evaluate(expr, arg) if arg is not _MISSING else page.evaluate(expr)
        if ok:
            return True
        page.wait_for_timeout(step)
    return False


RECOVER_JS = {
    # The live bubble's RAW stream length (reveal-lag-proof: _raw is written
    # synchronously by renderDelta, the paced tail is not).
    "raw_len": "() => { const mds = document.querySelectorAll('#chat-log .msg.agent .md');"
               " return mds.length ? (mds[mds.length - 1]._raw || '').length : 0; }",
    "raw_len_gt": "(n) => { const mds = document.querySelectorAll('#chat-log .msg.agent .md');"
                  " return mds.length > 0 && (mds[mds.length - 1]._raw || '').length > n; }",
    "log_text": "() => document.querySelector('#chat-log').textContent",
    "labels": "() => window.__T.labels",
    "row_toggles": "() => window.__T.rowToggles",
    "row_hidden": "() => document.querySelector('#chat-status').hidden",
    "send_mode": "() => (document.querySelector('#chat-send').dataset.mode || 'send') === 'send'",
    "calls_live": "() => window.__mock.calls.filter(p => p.includes('/live')).length",
    "calls_detach": "() => window.__mock.calls.filter(p => /\\/detach$/.test(p)).length",
    "peek": "() => window.__probe.peek()",
}


def rec(page, key, arg=_MISSING):
    return page.evaluate(RECOVER_JS[key], arg) if arg is not _MISSING else page.evaluate(RECOVER_JS[key])


def rec_labels(page):
    return [w["v"] for w in rec(page, "labels")]


def rec_submit(page, script, sid):
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [script, sid])
    send_message(page, f"Probe recover {script}")


def run_recover(page, c: Checks, engine: str) -> None:
    """BK-019 Step 5b: the CHAT transport's recovery paths in runstate.js —
    T15 socketDied (with T16 catchingUp and the watchdog's two verdicts),
    T03 screenLeave, T12 sendRejected, T13 sendFailed, T14 streamEndSilent —
    asserted on OBSERVABLE outcomes only: the status label's write ORDER
    (probe_scroll's textContent spy — the recovery phases land inside ONE
    synchronous task, which a MutationObserver cannot order), #chat-status's
    hidden toggles, the composer's mode, the mock's call log (the /detach
    POST, the watchdog's /live asks), and the transcript's own text. Field
    state is peeked for detail lines, never asserted on. The server side is
    honest: a died stream keeps running (ring-backed /live, persisted answer
    when the call returns), a 409 leaves no run, a rejected fetch leaves
    nothing at all. Desktop port: no scroll gesture is involved anywhere.
    """
    def nm(base):
        return f"[{engine}] {base}"

    def detail_extra():
        return " peek=" + json.dumps(rec(page, "peek"))[:110]

    print(f"== recover {engine} ==", flush=True)
    # T03's switch TARGET: a finished session with a long answer, so the
    # picker has a real option and the switch lands on a real transcript.
    # recover's OWN id (RC_HIST_SID): the matrix's fresh-page guard owns
    # HIST_SID, and both families run on the same desktop page.
    ensure_history(page, RC_HIST_SID)

    # -- T15a: die mid-stream → warn, detach POST, the honest 10 s watchdog --
    # tick #1 gap-fills the missed ring tail into the live bubble, tick #2
    # (after the server-side completion) runs the persisted heal and hands
    # the composer back. Nothing arrives over the dead SSE; nothing arrives
    # over /api/events either (the reroute is never armed here).
    sid = "probe-rc-t15a"
    rec_submit(page, "t15-die", sid)
    died = poll(page, "(v) => window.__T.labels.some(w => w.v === v)",
                arg=WARN_LABEL, ms=8000)
    t_death = time.monotonic()
    detach0 = rec(page, "calls_detach") >= 1
    mode_dead = metrics(page)["mode"]
    text_died = "request failed" in rec(page, "log_text")
    c.check(nm("recover-t15-warn-not-error: a mid-stream socket death shows "
               "'Reconnecting · still working' and NO error line (a recoverable "
               "state, not a failure)"),
            died and not text_died,
            f"warnLabel={died} errorLineShown={text_died}" + detail_extra())
    c.check(nm("recover-t15-detach-post: the death POSTs /detach (arms the reroute) "
               "and Stop STAYS armed (liveTurn kept — the run is still live)"),
            detach0 and mode_dead == "stop",
            f"detachPosts={rec(page, 'calls_detach')} modeAtDeath={mode_dead}")
    raw0 = rec(page, "raw_len")
    bubbles0 = page.evaluate(
        "() => document.querySelectorAll('#chat-log .msg.agent').length")
    live_seen = poll(page, "() => window.__mock.calls.some(p => p.includes('/live'))",
                     ms=13500)
    live_at = time.monotonic() - t_death
    c.check(nm("recover-t15-watchdog-live: the FIRST /live ask comes ~10 s after the "
               "death — the detach watchdog's interval, not an immediate ask"),
            live_seen and 8.5 <= live_at <= 13.5,
            f"firstLiveAt={live_at:.1f}s after death (interval=10000ms) seen={live_seen}")
    grew = poll(page, RECOVER_JS["raw_len_gt"], arg=raw0 + 25, ms=2500)
    # The gap-filled bytes enter through the PACED reveal too (paintStream):
    # WebKit's rAF cadence lags chromium's — poll for the bytes ON SCREEN,
    # bounded, instead of reading once at an engine-dependent instant.
    ring_shown = poll(page,
                      "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                      arg="t15ring", ms=4000)
    text_gap = rec(page, "log_text")
    bubbles_gap = page.evaluate(
        "() => document.querySelectorAll('#chat-log .msg.agent').length")
    c.check(nm("recover-t15-gapfill: tick #1 found the run ACTIVE and gap-filled the "
               "missed ring tail into the EXISTING bubble (head grew past its at-death "
               "length; the missed frames' bytes are on screen; no second bubble)"),
            grew and ring_shown and "t15ring" in text_gap and bubbles_gap == bubbles0,
            f"rawAtDeath={raw0} grew={grew} ringBytesShown={ring_shown} "
            f"bubbles={bubbles0}->{bubbles_gap}"
            + detail_extra())
    handed = poll(page, RECOVER_JS["send_mode"], ms=25000)
    healed = poll(page, "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                  arg="and the tail word.", ms=3000)
    stopped = poll(page, RECOVER_JS["row_hidden"], ms=3000)
    c.check(nm("recover-t15-heal-handback: after the server-side completion the next "
               "tick reads /live inactive and runs the PERSISTED heal — composer back "
               "to send, status row stopped"),
            handed and stopped,
            f"composerSend={handed} rowHidden={stopped}" + detail_extra())
    c.check(nm("recover-t15-healed-text: the heal re-rendered the FULL server-side "
               "answer (its tail bytes) from the store — the run the dead client "
               "never saw arrived complete"),
            healed, f"answerTailShown={healed}" + detail_extra())

    # -- T15b / T16: the reroute's first word flips "Catching up…" ONCE and --
    # -- retires the watchdog's premise: after the flip, /live is NEVER asked.
    sid = "probe-rc-t15b"
    rec_submit(page, "t15-die", sid)
    died = poll(page, "(v) => window.__T.labels.some(w => w.v === v)",
                arg=WARN_LABEL, ms=8000)
    t_death = time.monotonic()
    die_seq = page.evaluate("() => (window.__mock.chat || {}).dieSeq")
    run_id = "probe-run-t15-die"
    page.evaluate("(a) => window.__probe.deliver({ type: 'delta', text: ' rerouted tail word',"
                  " session_id: a[0], seq: a[1], run_id: a[2] })", [sid, die_seq, run_id])
    page.wait_for_timeout(300)
    page.evaluate("(a) => window.__probe.deliver({ type: 'delta', text: ' second rerouted',"
                  " session_id: a[0], seq: a[1], run_id: a[2] })", [sid, die_seq + 1, run_id])
    page.wait_for_timeout(300)
    labs = rec_labels(page)
    warn_i = labs.index(WARN_LABEL) if WARN_LABEL in labs else None
    catch_n = labs.count(CATCH_LABEL)
    catch_i = labs.index(CATCH_LABEL) if CATCH_LABEL in labs else None
    write_i = next((i for i, v in enumerate(labs) if i > (catch_i or 10**9) and v == "Writing…"),
                   None)
    c.check(nm("recover-t16-flip-once: the FIRST rerouted event flips the row to "
               "'Catching up…' exactly once (the second event does not re-flip) and "
               "its statusEvent writes over it in the same task — the label ORDER is "
               "warn → catching → writing"),
            died and warn_i is not None and catch_n == 1 and write_i is not None,
            f"order warn={warn_i} catching={catch_i}(x{catch_n}) writing={write_i}"
            + detail_extra())
    # The reveal is PACED (REVEAL_MIN_CPS=8): the rerouted bytes appear over
    # ~1-2 s, so the landing is POLLED, and the bubble must stay the SAME one
    # — the tail joins the existing answer, it never opens a second bubble.
    bubbles_before = page.evaluate(
        "() => document.querySelectorAll('#chat-log .msg.agent').length")
    landed = poll(page, "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                  arg="second rerouted", ms=4000)  # prefix reveal: the LAST word
    last_txt = page.evaluate(
        "() => { const mds = document.querySelectorAll('#chat-log .msg.agent .md');"
        " return mds.length ? mds[mds.length - 1].textContent : ''; }")
    bubbles_after = page.evaluate(
        "() => document.querySelectorAll('#chat-log .msg.agent').length")
    c.check(nm("recover-t16-rerouted-paints: the rerouted events' bytes land on the "
               "EXISTING bubble (T06 tail through the events sink — no second agent "
               "bubble opens)"),
            landed and bubbles_after == bubbles_before and "second rerouted" in last_txt,
            f"reroutedShown={landed} secondInLastBubble={'second rerouted' in last_txt} "
            f"bubbles={bubbles_before}->{bubbles_after}")
    # The premise retirement is time-based: the watchdog's FIRST tick would
    # land ~10 s after the arm; wait past it, then prove /live was never asked
    # (watchdogTick reads resync.seen and self-clears the timer).
    page.wait_for_timeout(max(0, int((11.5 - (time.monotonic() - t_death)) * 1000)))
    live_n = rec(page, "calls_live")
    mode_b = metrics(page)
    c.check(nm("recover-t15b-premise-retired: with resync.seen flipped, the watchdog's "
               "10 s tick asks /live NEVER (the reroute answered the question the "
               "timer exists to ask)"),
            live_n == 0,
            f"liveCalls={live_n} (expected 0 by t+11.5s after death)")
    c.check(nm("recover-t15b-stop-stays: the run is still live server-side — composer "
               "stays on stop and the row stays visible (nothing terminal arrived)"),
            mode_b["mode"] == "stop" and mode_b["statusHidden"] is False,
            f"mode={mode_b['mode']} rowHidden={mode_b['statusHidden']}")

    # -- T03: switching sessions mid-run latches the stream FOR GOOD — every --
    # -- later frame of that run must keep off the new screen's DOM entirely.
    sid = "probe-rc-t03"
    rec_submit(page, "t03-switch", sid)
    tagged = poll(page, "() => window.__mock.tagAt.switchNow !== undefined", ms=10000)
    page.wait_for_timeout(250)  # the frames around the tag are processed
    # The switch itself stops the row (sessions.js openSession — "the row
    # describes the OLD session's turn"), synchronously inside select_option,
    # so the stop window opens BEFORE the switch call; the latch windows
    # (paints/labels) stay after t_switch.
    t_pre = page.evaluate("() => Math.round(performance.now())")
    page.select_option("#session-select", RC_HIST_SID)  # the REAL picker change handler
    t_switch = page.evaluate("() => Math.round(performance.now())")
    done_ok = poll(page, "() => window.__mock.doneAt !== null", ms=15000)
    page.wait_for_timeout(700)
    text_t03 = rec(page, "log_text")
    labels_after = [v for w, v in zip((w["t"] for w in rec(page, "labels")), rec_labels(page))
                    if w > t_switch]
    toggles_after = [w for w in rec(page, "row_toggles") if w["t"] > t_switch and not w["hidden"]]
    stops_after = [w for w in rec(page, "row_toggles") if w["t"] >= t_pre and w["hidden"]]
    history_shown = "less important than how it was assembled" in text_t03
    m03 = metrics(page)
    c.check(nm("recover-t03-switch-lands: the picker switch really re-rendered the "
               "history session (its answer is on screen) before the run finished"),
            tagged and done_ok and history_shown,
            f"tagged={tagged} doneLanded={done_ok} historyText={history_shown}")
    c.check(nm("recover-t03-latched-silent: after the switch NO frame of the old run "
               "touches this screen — its bytes never appear, the status label is "
               "silent, the row is stopped EXACTLY once and never re-shows (the "
               "for-good latch kills paints AND status writes)"),
            "t03alpha" not in text_t03 and "final reading." not in text_t03
            and not labels_after and not toggles_after
            and len(stops_after) == 1 and m03["statusHidden"] is True,
            f"runBytesShown={'t03alpha' in text_t03} labelWrites={len(labels_after)} "
            f"rowReopens={len(toggles_after)} rowStops={len(stops_after)} "
            f"rowHiddenAfterDone={m03['statusHidden']}" + detail_extra())
    mode_t03 = m03["mode"]
    c.check(nm("recover-t03-done-hands-back: the latched run's OWN done frame still "
               "clears the turn (T09's lone hand-back) — the latch gates paints, "
               "never lifecycle"),
            mode_t03 == "send", f"modeAfterDone={mode_t03}" + detail_extra())

    # -- T12: 409 before the run exists — an error line, hand-back, no run. --
    sid = "probe-rc-t12"
    rec_submit(page, "t12-busy", sid)
    line = poll(page, "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                arg="another run is in flight (mock)", ms=5000)
    page.wait_for_timeout(400)
    m12 = metrics(page)
    untouched = page.evaluate(
        "(id) => !window.__mock.store[id] || !window.__mock.store[id].messages.length", sid)
    c.check(nm("recover-t12-error-lands: the rejected send shows the server's detail "
               "line (the news IS the error row — and the store stays untouched, "
               "the run never started)"),
            line and untouched,
            f"detailLineShown={line} storeUntouched={untouched}")
    c.check(nm("recover-t12-hands-back: composer back to send, row stopped — with no "
               "resync armed the rejected turn leaves nothing live (D5)"),
            m12["mode"] == "send" and m12["statusHidden"] is True,
            f"mode={m12['mode']} rowHidden={m12['statusHidden']}")

    # -- T12b (D4/T22): the SAME 409 with a detached run's resync armed — the --
    # -- hand-back must NOT strip Stop from the run that still needs it.
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("() => window.__probe.armResync('probe-rc-detached-x')")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["t12-busy", "probe-rc-t12b"])
    send_message(page, "Probe recover t12b (busy over a detached run)")
    line_b = poll(page, "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                  arg="another run is in flight (mock)", ms=5000)
    page.wait_for_timeout(400)
    mode_12b = metrics(page)["mode"]
    c.check(nm("recover-t12b-stop-stays-for-detached: a 409 while a DIFFERENT run's "
               "resync is armed shows the error line but keeps the composer on stop "
               "(the composer is the only Stop affordance — the ternary over ui.resync)"),
            line_b and mode_12b == "stop",
            f"detailLineShown={line_b} mode={mode_12b}" + detail_extra())

    # -- T13: the fetch throws before any server answer — error line, NOT a --
    # -- detach (the never-started turn must not arm recovery).
    sid = "probe-rc-t13"
    rec_submit(page, "t13-netfail", sid)
    line13 = poll(page, "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                  arg="request failed: mock: network down", ms=5000)
    page.wait_for_timeout(1200)  # a wrong T15 here would have POSTed detach by now
    m13 = metrics(page)
    labs13 = rec_labels(page)
    c.check(nm("recover-t13-error-lands: a send the wire never delivered shows "
               "'request failed: …' as the error line"),
            line13, f"errorLineShown={line13}")
    c.check(nm("recover-t13-not-detached: the never-started turn arms NOTHING — no "
               "warn label, no /detach POST, no /live ask (T15's turnStarted split), "
               "and the turn hands back"),
            WARN_LABEL not in labs13 and rec(page, "calls_detach") == 0
            and rec(page, "calls_live") == 0 and m13["mode"] == "send"
            and m13["statusHidden"] is True,
            f"warnShown={WARN_LABEL in labs13} detachPosts={rec(page, 'calls_detach')} "
            f"liveCalls={rec(page, 'calls_live')} mode={m13['mode']}" + detail_extra())

    # -- T14 (BK-036): the stream ends CLEANLY with no terminal frame AFTER the --
    # -- run started. A proxy/iOS can do this while the run goes on, so it is T15's
    # -- state (resync armed, Stop kept, /live decides) — NOT a hand-back. A
    # -- never-started turn still hands back (recover-t13 above).
    sid = "probe-rc-t14"
    rec_submit(page, "t14-silent", sid)
    closed = poll(page, "() => window.__mock.runClosed === true", ms=8000)
    died14 = poll(page, "(v) => window.__T.labels.some(w => w.v === v)",
                  arg=WARN_LABEL, ms=4000)
    # The partial's tail is behind the PACED reveal (8 cps floor) — poll for
    # the bytes to finish landing, then read. The check is that they STAY
    # (no re-render wipes them), not that they were there at a fixed instant.
    kept14 = poll(page, "(s) => document.querySelector('#chat-log').textContent.includes(s)",
                  arg="then the socket closes.", ms=3000)
    done_null = page.evaluate("() => window.__mock.doneAt === null")
    m14 = metrics(page)
    text14 = rec(page, "log_text")
    c.check(nm("recover-t14-silent-close: the stream really ended WITHOUT a terminal "
               "frame (reader exhausted, no done/stopped/error ever emitted)"),
            closed and done_null, f"runClosed={closed} doneAtNull={done_null}")
    c.check(nm("recover-t14-keeps-stop: a started stream's silent end KEEPS Stop "
               "(composer stays on stop, the status row stays visible) and the "
               "partial answer stays as streamed — no re-render"),
            m14["mode"] == "stop" and m14["statusHidden"] is False and kept14,
            f"mode={m14['mode']} rowHidden={m14['statusHidden']} partialKept={kept14}")
    c.check(nm("recover-t14-arms-resync: the silent end arms resync like a socket "
               "death — warn label, /detach POST — with NO error line"),
            died14 and "request failed" not in text14 and rec(page, "calls_detach") >= 1,
            f"warnShown={died14} errorShown={'request failed' in text14} "
            f"detachPosts={rec(page, 'calls_detach')}")

    # -- T14b (§10.13 corner): the SAME silent end with the run's OWN resync --
    # -- armed mid-stream hands NOTHING back — Stop belongs to the live run.
    sid = "probe-rc-t14b"
    rec_submit(page, "t14-silent", sid)
    streaming = poll(page, RECOVER_JS["raw_len_gt"], arg=10, ms=5000)
    page.evaluate("(id) => window.__probe.armResync(id)", sid)  # the sanctioned seam
    closed_b = poll(page, "() => window.__mock.runClosed === true", ms=6000)
    page.wait_for_timeout(500)
    m14b = metrics(page)
    c.check(nm("recover-t14b-resync-holds-stop: with resync armed, the silent stream "
               "end is NOT a hand-back — composer stays on stop and the row stays "
               "visible (only a terminal transition or the verdict clears a live run)"),
            streaming and closed_b and m14b["mode"] == "stop"
            and m14b["statusHidden"] is False,
            f"streamed={streaming} closed={closed_b} mode={m14b['mode']} "
            f"rowHidden={m14b['statusHidden']}" + detail_extra())

    page.evaluate("() => window.__probe.reset()")


# ── rollup guards (pwa_resume precedent) ─────────────────────────────────────
# The Step-5 acceptance is a TWO-engine matrix; a runner that can exit 0 while
# an engine contributed ZERO rows, or while one engine silently ran a smaller
# family set, is the worst failure this fixture can ship — the floor is
# structural. May only RISE: a deliberate merge/replacement edits the figure
# in the same change (pwa_resume's MIN_UNIQUE_ASSERTIONS discipline).
# Measured 2026-10-30 full run (two engines, no filters, RC_HIST_SID tree):
# unique (engine, name) PASS/FAIL rows = 125 per engine, sets identical. The
# placeholder 161 FIRED on that same run (exit 2, 0 failing checks) — the
# guard's own live proof before it was set to the measured number. DERIVE the
# figure from a run's unique rows, never carry one from a report (a remembered
# "42 mechanisms" — really 43 — produced 124 vs the true 125, twice).
# BK-029 (2026-10-31 full run, both engines, sets identical): 142 = 125 +
# run_touch_rules' 9 iPhone + 8 desktop rows (touch-tap/mouse-click-unpins,
# latest-repins, press-under-pill-stays-unpinned, touch-drag-unpins (iPhone),
# press-unscrollable-keeps, d1-keyboard-pill/think-pauses, d1-latest-resumes,
# d1-idle-open-keeps). Same total as the reversed tap-hold build, other rows.
MIN_UNIQUE_PER_ENGINE = 143


def engine_assertion_rows(lines):
    """Per-engine SETS of assertion identities from the check rows. The
    [engine] / [engine/port] prefix token folds to a placeholder so the two
    naming schemes compare across engines. Unique rows only, and PASS/FAIL
    only: every check prints twice (live + the final dump) and SKIPs mean the
    check could not run, not that an engine skipped a scenario."""
    rows = {}
    for ln in lines:
        if not ln.startswith(("PASS ", "FAIL ")):
            continue
        name = ln[5:].split(" | ")[0]
        m = re.match(r"^\[([a-z]+)(/[a-z]+)?\] (.*)$", name)
        if not m:
            continue
        eng, port, base = m.group(1), m.group(2) or "", m.group(3)
        rows.setdefault(eng, set()).add(f"[*{port}] {base}")
    return rows


def coverage_problem(engines, skips, rows):
    """A results block is NOT proof the scenarios ran. For every engine that
    was selected and did NOT record a launch SKIP, its unique assertion set
    must reach the floor — 'engine silently contributed nothing' exits 2."""
    skipped = {l[len("SKIP engine-"):].split(" ")[0]
               for l in skips if l.startswith("SKIP engine-")}
    for engine in engines:
        if engine in skipped:
            continue
        n = len(rows.get(engine, set()))
        if n < MIN_UNIQUE_PER_ENGINE:
            return (f"{engine} produced {n} unique assertion(s), floor "
                    f"{MIN_UNIQUE_PER_ENGINE} — a scenario family did not run; a "
                    "zero-row results block must never read as green")
    return None


def silent_engine_problem(engines, skips, rows):
    """The scope-free half of the zero-row rule (planted and REPRODUCED
    2026-10-30: webkit returning from run_recover early — an engine selected,
    launched, and contributing ZERO rows — exited 0 on a scoped run: an empty
    engine has no entry in rows, so set-identity saw one engine and coverage
    only speaks on full runs). A selected engine with zero rows and no launch
    SKIP means the family set silently ran nothing here: cannot measure, 2."""
    skipped = {l[len("SKIP engine-"):].split(" ")[0]
               for l in skips if l.startswith("SKIP engine-")}
    for engine in engines:
        if engine in skipped:
            continue
        if not rows.get(engine):
            return (f"{engine} contributed ZERO assertion rows without a launch "
                    "skip — a family silently ran nothing for this engine; a "
                    "zero-row results block must never read as green")
    return None


def engine_set_problem(rows):
    """Cross-engine identity: a family that ran on one engine and not the
    other shows up as a SET difference even when the totals happen to match."""
    if len(rows) < 2:
        return None
    base_engine = sorted(rows)[0]
    base = rows[base_engine]
    for engine in sorted(rows)[1:]:
        if rows[engine] != base:
            missing = sorted(base - rows[engine])
            extra = sorted(rows[engine] - base)
            return (f"{engine}'s assertion set differs from {base_engine}'s "
                    f"(missing={missing[:4]}, extra={extra[:4]}) — an engine "
                    "ran a different scenario set")
    return None


def empty_selection_problem(rows):
    """No engine produced a single assertion row. Reachable when every selected
    engine was launch-skipped (no WebKit install, a browser that would not
    start) or removed by --engines — the SKIP lines are loud, but 'ran nothing'
    is not a measurement and must never read green (main's 'no checks ran'
    usage error only catches the SKIP-less case)."""
    if rows:
        return None
    return ("no engine produced an assertion row — a skipped or unavailable "
            "engine is not a run; cannot measure")


def gap(m):
    return m["h"] - m["ch"] - m["top"]


def noncomp(writes):
    """Scroll writes that are NOT the read-preservation compensation — the
    unfollowed-flat contract tolerates keepRead/restoreRead brackets (they
    RESTORE position), it forbids everything else."""
    return [w for w in writes
            if "keepRead" not in w["stack"] and "restoreRead" not in w["stack"]]


def parse_args(argv):
    only, eng_f, port_f = set(FAMILIES), None, None
    for a in argv:
        k, _, v = a.partition("=")
        want = {x for x in v.split(",") if x}
        if k == "--only":
            if not want <= set(FAMILIES):
                print(f"usage error: --only wants a subset of {FAMILIES}", file=sys.stderr)
                sys.exit(2)
            only = want
        elif k == "--engines":
            if not want <= {"chromium", "webkit"}:
                print("usage error: --engines wants chromium and/or webkit", file=sys.stderr)
                sys.exit(2)
            eng_f = want
        elif k == "--ports":
            if not want <= {p["name"] for p in PORTS}:
                print(f"usage error: --ports wants a subset of "
                      f"{[p['name'] for p in PORTS]}", file=sys.stderr)
                sys.exit(2)
            port_f = want
        else:
            print(f"usage error: unknown flag {a!r} (see module docstring)", file=sys.stderr)
            sys.exit(2)
    return only, eng_f, port_f


def touch_drag_up(cdp, page, x, y, dist=DRAG_DIST):
    """A REAL touch scroll-up through Chromium's CDP input pipeline (the same
    channel Playwright's touchscreen.tap uses): finger travels DOWN so the
    content follows it and the view scrolls UP. Playwright's Touchscreen API
    has no drag — this is why the drag arm is chromium-only (WebKit: tap
    proves the unpin rule, wheel moves the view; recorded as such)."""
    page.evaluate("() => window.__probe.mark('dragAt')")
    cdp.send("Input.dispatchTouchEvent",
             {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
    for k in range(1, 11):
        cdp.send("Input.dispatchTouchEvent",
                 {"type": "touchMove", "touchPoints": [{"x": x, "y": y + dist * k / 10}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    page.wait_for_timeout(150)  # let the scroll events land (WebKit-style async)


def log_center(page, top_frac=0.3):
    box = page.locator("#chat-log").bounding_box()
    return box, box["x"] + box["width"] / 2, box["y"] + box["height"] * top_frac


def text_point(page):
    """A point on plain transcript text, never a control: BK-029's BARE press
    (one that opens nothing — the press alone must stop following).
    Falls back to log_center when no prose line is on screen."""
    pt = page.evaluate(
        """() => { const l = document.querySelector('#chat-log').getBoundingClientRect();
          const ps = [...document.querySelectorAll('#chat-log .md p, #chat-log .md li')].reverse();
          for (const p of ps) { const r = p.getBoundingClientRect(), y = r.top + 8;
            if (y < l.top + 20 || y > l.bottom - 20) continue;
            const x = l.left + l.width / 2, e = document.elementFromPoint(x, y);
            if (e && p.contains(e) && !e.closest('button, a, summary, [role=button]')) return [x, y]; }
          return null; }""")
    if pt:
        return pt
    _box, cx, cy = log_center(page)
    return [cx, cy]


def synth_drag(page, dy, steps=3, hold_ms=40):
    """A touch DRAG of dy px finger travel on #chat-log through DISPATCHED
    touch events (isTrusted false; Playwright builds the Touch objects —
    WebKit's own Touch constructor is illegal). It moves NO pixels: it drives
    follow.js's touch listeners exactly (touchstart's unpin, touchend's
    maybeRepin) — the one thing Playwright cannot do to a WebKit page with
    real input (no drag API)."""
    log = page.locator("#chat-log")
    box = log.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    def pt(yy):
        return {"touches": [{"identifier": 11, "clientX": x, "clientY": yy}]}
    log.dispatch_event("touchstart", pt(y))
    for k in range(1, steps + 1):
        log.dispatch_event("touchmove", pt(y + dy * k / steps))
    page.wait_for_timeout(hold_ms)
    log.dispatch_event("touchend", {"touches": []})


def scroll_up_gesture(page, engine, port, cdp, px, min_gap=None, tries=2):
    """Scroll a transcript UP by roughly px with the port's honest input:
    wheel on desktop, a real touch drag on chromium/iphone; webkit/iphone
    cannot be touch-DRAGGED through Playwright (no drag API, and wheel there
    is the position arm — the UNPIN proof stays the tap), so it gets wheel.

    With `min_gap`, a gesture that did not move the view that far is retried:
    WebKit delivers a wheel to whatever is under the pointer, and one matrix
    run unpinned on the wheel yet left the log 3 px up (measured 2026-10-29) —
    a missed input race, not a product result. The caller's check still
    decides; a gesture that never lands is a FAIL, never a skip."""
    for _ in range(tries):
        if port == "iphone" and engine == "chromium" and cdp is not None:
            box, cx, cy = log_center(page)
            touch_drag_up(cdp, page, cx, cy, dist=px)
        else:
            hover_log(page)
            wheel_up(page, -px)
        if min_gap is None or gap(metrics(page)) >= min_gap:
            return metrics(page)
        page.wait_for_timeout(150)
    return metrics(page)


def ensure_history(page, sid):
    """Give the page a FINISHED session with a long answer (8 paragraphs of
    prose, real submit -> SSE -> done), so state-(a) runs have scroll room and
    the guards have something to reopen. Returns the preamble telemetry when
    this call ran it (the preamble IS the fresh-session prose-b run), else
    None when the store already has it."""
    if page.evaluate(
            "(id) => { const r = window.__mock.store[id]; "
            "return !!(r && r.messages.some(m => m.role === 'assistant')); }", sid):
        return None
    tel, _st = scenario_stream(page, "control", sid)  # the long-prose script
    return tel


def wait_park_ready(page, base=[0], timeout=8000):
    """The static park (wait_log_static) only proves the DOM STOPPED
    changing — true before the first delta too, and on a history session true
    of the OLD bubbles forever. Gate on the NEW answer: more agent rows than
    `base` (the count at send) and THAT bubble's _raw past 40 chars — the
    stream is genuinely flowing. (Row count alone cannot gate: the seam parks
    with the user row, the agent bubble and the streamed text — the table
    block is the growth the seam exists to deliver.)"""
    try:
        page.wait_for_function(
            "([base]) => { const bs = document.querySelectorAll('#chat-log .msg.agent');"
            " if (bs.length <= base) return false;"
            " const m = bs[bs.length - 1].querySelector('.md');"
            " return !!m && (m._raw || '').length > 40; }",
            arg=base, timeout=timeout)
    except PlaywrightError as exc:
        raise HarnessError(f"the stream never started growing the log: {exc}") from None


def wait_reader_room(page, gap_need=30, timeout=25000):
    """Gate the a-state gesture: the live answer bubble carries >40 streamed
    chars AND the log can actually scroll (overflow past gap_need). A fixed
    tall-height gate cannot work on every port: the iPhone viewport swallows
    the code shape whole — its total 219 px of room only exists after the
    done flush, so the tall gate satisfied 12 ms AFTER done and the gesture
    chased it (measured 2026-10-29). Progress + room is the honest contract:
    the reader is mid-answer with something to scroll."""
    try:
        page.wait_for_function(
            "(need) => { const bs = document.querySelectorAll('#chat-log .msg.agent');"
            " if (!bs.length) return false;"
            " const m = bs[bs.length - 1].querySelector('.md');"
            " const l = document.querySelector('#chat-log');"
            " return !!m && (m._raw || '').length > 40"
            " && (l.scrollHeight - l.clientHeight) > need; }",
            arg=gap_need, timeout=timeout)
    except PlaywrightError as exc:
        raise HarnessError(f"the stream never gave the reader scroll room: {exc}") from None


def scenario_touch_up(page, script, sid, engine, port, cdp):
    """Matrix state (a) over TOUCH: submit, wait for streamed text + scroll
    room (wait_reader_room — per-port honest, see its note), a REAL tap on
    the log (follow.js unpins on touchstart — the shipping phone gesture),
    then the scroll-up: chromium gets a CDP touch drag, webkit gets a wheel
    for the POSITION only (Playwright cannot drag a webkit touch; the UNPIN
    proof is the tap either way)."""
    print(f"scenario {script}: start (touch)", flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", [script, sid])
    send_message(page, f"Probe {script}: touch unpin mid-stream")
    wait_reader_room(page)
    states = {"tall": metrics(page)}
    box, cx, cy = log_center(page)
    page.evaluate("() => window.__probe.mark('tapAt')")
    page.touchscreen.tap(cx, cy)
    page.wait_for_timeout(120)
    states["unpin"] = metrics(page)  # touchstart must already have unpinned
    touch_drag = engine == "chromium" and cdp is not None
    scroll_up_gesture(page, engine, port, cdp, DRAG_DIST, min_gap=MIN_STATE_GAP)
    states["gesture"] = ("tap+cdp-drag" if touch_drag
                         else "tap+wheel-position(webkit-no-drag)")
    states["moved"] = metrics(page)
    wait_done(page)
    page.wait_for_timeout(SETTLE_MS)
    return collect(page), states


def scenario_short_over_history(page, sid, engine, port, cdp, gesture=True):
    """Over the history transcript: reopen the finished session, send the short
    reply (submit jumps — addUserMsg). `gesture=True` is state (a): the gesture
    happens against the HISTORY's scroll room while the short answer is still
    to come (its delta is +900 ms). `gesture=False` is state (b): the follower
    at the history's bottom must be carried down to the short answer's newest
    bubble — a fresh-session short-b cannot prove anything (maxTop stays 0, so
    "at the bottom" is free; measured 2026-10-29)."""
    print(f"scenario short-plain: start (over history{'' if gesture else ', follower'})",
          flush=True)
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(id) => window.__probe.openSession(id)", sid)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)  # jump + spring settle after the open
    states = {"opened": metrics(page)}
    page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["short-plain", sid])
    send_message(page, f"Probe short-over-history: short reply while reading up")
    page.wait_for_timeout(250)  # the user row landed; no delta yet (+900)
    states["sent"] = metrics(page)
    if gesture:
        if port == "iphone":
            box, cx, cy = log_center(page)
            page.touchscreen.tap(cx, cy)
            page.wait_for_timeout(120)
            states["unpin"] = metrics(page)
            touch_drag = engine == "chromium" and cdp is not None
            scroll_up_gesture(page, engine, port, cdp,
                              DRAG_DIST if touch_drag else abs(WHEEL_SHORT_A),
                              min_gap=MIN_STATE_GAP)
            states["gesture"] = ("tap+cdp-drag" if touch_drag
                                 else "tap+wheel-position(webkit-no-drag)")
        else:
            states["unpin"] = scroll_up_gesture(
                page, engine, port, cdp, abs(WHEEL_SHORT_A), min_gap=MIN_STATE_GAP)
            states["gesture"] = "wheel"
        states["moved"] = metrics(page)
    wait_done(page, label="short-plain done (over history)")
    page.wait_for_timeout(SETTLE_MS)
    return collect(page), states


def tap_pill(page, port):
    """Tap the "↓ Latest" pill with the port's native pointer."""
    box = page.locator("#chat-jump").bounding_box()
    if box is None:
        return False
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    if port == "iphone":
        page.touchscreen.tap(x, y)
    else:
        page.mouse.click(x, y)
    # 900 ms: the pill's click starts the SPRING, not a snap — from a
    # 400-500 px read position it glides (measured ~15 frames; the short
    # shape's 500 px glide was not home inside 400 ms, 2026-10-29).
    page.wait_for_timeout(900)
    return True


def matrix_a_checks(c, nm, shape, gesture, tel, unpin_m, moved_m, page, port):
    """Criterion 2(a): unpinned at completion — Δ scrollTop <= 2 px across the
    completion re-render, no non-compensation scroll write, the pill visible,
    and the pill's tap lands the view at the newest message."""
    pre, post = pre_post(tel)
    jump = round(post["top"] - pre["top"], 2)
    win = writes_in(tel, -32, SETTLE_MS)
    stray = noncomp(win)
    pinstr = ""
    if stray:
        # Full, untruncated dump: the one-line detail caps length, and the
        # pin-edge stacks are the whole story of a stray write.
        print("  DIAG pins:", json.dumps(tel.get("pins", [])[-6:]), flush=True)
        print("  DIAG win-writes:", json.dumps(win[:8]), flush=True)
        trans, prev = [], None
        for s in tel["samples"]:
            cur = (s["pinned"], s["pill"])
            if cur != prev:
                trans.append({"t": s["t"], "pinned": s["pinned"], "pill": s["pill"],
                              "top": s["top"], "h": s["h"], "ch": s["ch"]})
                prev = cur
        print("  DIAG pin-transitions:", json.dumps(trans), flush=True)
        print("  DIAG marks:", json.dumps(tel.get("meta", {})), flush=True)
        edges = tel.get("pins", [])[-3:]
        pinstr = (f" doneAt={tel.get('doneAt')} strayT={stray[0].get('t')} "
                  " pinEdges=[" + " ;; ".join(
                      f"{'+PIN' if p['pinned'] else '-unpin'}@{p['t']} {p['stack'][:240]}"
                      for p in edges) + "]")
    c.check(nm(f"matrix-{shape}-a-unpinned-flat"),
            unpin_m["pinned"] is False and unpin_m["pill"] is True
            and gap(moved_m) >= MIN_STATE_GAP
            and abs(jump) <= 2 and not stray
            and post["pinned"] is False and post["pill"] is True
            # The completion must NOT land the view inside the repin window:
            # a clamp-induced arrival at the bottom is indistinguishable from
            # a repin here, so the flat contract holds the gap OPEN (measured:
            # the fenced-code shape's done re-layout drops it 96 px).
            and gap(post) >= 100,
            f"gesture={gesture} dscrollTop={jump:+.2f}px writes={len(win)} "
            f"stray={len(stray)} stray0={stray[0]['stack'][:500] if stray else '-'} "
            f"gapMoved={gap(moved_m):.0f}px gapDone={gap(post):.0f}px "
            f"pinned@unpin={unpin_m['pinned']} pinned@done={post['pinned']} "
            f"pill@done={post['pill']}{pinstr}")
    before = metrics(page)
    tapped = tap_pill(page, port)
    after = metrics(page)
    c.check(nm(f"matrix-{shape}-a-pilltap"),
            tapped and before["pill"] is True
            and after["pinned"] is True and gap(after) <= 2 and after["pill"] is False,
            f"pillBefore={before['pill']} pinnedAfter={after['pinned']} "
            f"gapAfter={gap(after):.1f}px pillAfter={after['pill']}")


def wait_bubble_paint(page, timeout=4000):
    """The finalize render is one synchronous innerHTML write; the sampler can
    catch the frame mid-paint (_shown stale, height not yet final). Wait for
    the bubble's rendered state to STOP CHANGING, then one rAF, then it is
    fair to assert on it."""
    deadline = time.monotonic() + timeout / 1000
    last = None
    while time.monotonic() < deadline:
        st = page.evaluate(
            "() => { const mds = document.querySelectorAll('#chat-log "
            ".msg.agent .md'); const md = mds.length ? mds[mds.length-1] : null; "
            "return md ? [md._shown | 0, md.textContent.length] : [0, 0]; }")
        if st == last:
            page.evaluate("() => new Promise((r) => requestAnimationFrame(r))")
            return True
        last = st
        page.wait_for_timeout(60)
    return False


def matrix_b_checks(c, nm, shape, tel, page):
    """Criterion 2(b): following — ends pinned to the newest message with no
    half-rendered gap, the pill never shown."""
    fin = metrics(page)
    mx = fin["h"] - fin["ch"]
    samples = tel["samples"]
    c.check(nm(f"matrix-{shape}-b-pinned-bottom"),
            all(s["pinned"] for s in samples) and not any(s["pill"] for s in samples)
            and fin["pinned"] is True and fin["pill"] is False
            and fin["top"] >= mx - 2 and mx > 100,
            f"overflow={mx:.0f}px top={fin['top']:.0f} pinned={fin['pinned']} "
            f"pill={fin['pill']} samples={len(samples)}")
    wait_bubble_paint(page)
    bs = page.evaluate("() => window.__probe.bubbleState()")
    # The REAL full-render contract, read off the DOM after the finalize
    # render settled: the rendered text carries the answer's tail bytes and
    # the bubble's bottom edge sits at the log's content bottom. (`_shown`
    # is reveal bookkeeping that finalize's synchronous render does not
    # update — reported as detail only, never asserted.)
    c.check(nm(f"matrix-{shape}-b-full-render"),
            bs.get("present") is True and bs.get("tailOk") is True
            and bs.get("textLen", 0) > 0
            # The bubble's bottom edge lands at the log's content bottom
            # (padding-box edge, <=2px spring/round slack — "pinned to the
            # NEWEST message" geometrically, no blank gap). Shapes shorter
            # than the viewport have no content bottom; the b-pinned-bottom
            # check already holds them at top, pinned, pillless.
            and (not bs.get("overflowed", True) or -2 <= bs.get("bottomDelta", 999) <= 2),
            f"textLen={bs.get('textLen')}/{bs.get('rawLen')} "
            f"tailOk={bs.get('tailOk')} tail={bs.get('tail')!r} "
            f"bottomDelta={bs.get('bottomDelta')}px last={bs.get('lastChild')!r} "
            f"revealShown={bs.get('shown')}")


def run_matrix(page, c, engine, port, cdp):
    """The criterion-2 matrix for ONE (engine, port): six shapes x
    unfollowed (gesture mid-stream; wheel on desktop, touch on the iPhone
    port) / followed. The keyboard third path is the mechanism suite's
    mech-c-keyboard-* family (desktop, already named). Every cell files a
    screenshot of its END state to the probe work dir (plan Step 3's
    "screenshots per shape") — for the (a) cells that frame is taken BEFORE
    the pill tap (the pill's tap is the same check's second act; tap first and
    the shot is just the (b) end state again)."""
    def nm(base):
        return f"[{engine}/{port}] {base}"

    def shot(label):
        page.screenshot(path=str(WORK / f"matrix-{label}-{engine}-{port}.png"))
    touch = port == "iphone"
    cfg = next(p for p in PORTS if p["name"] == port)
    print(f"== matrix {engine}/{port}: {len(MATRIX_SHAPES)} shapes x 2 states ==",
          flush=True)
    # A real assertion, not a restatement: the page must actually carry the
    # port's configured viewport (boot_page could be handed the wrong config).
    # Touch presence is load-bearing implicitly — every touch scenario calls
    # page.touchscreen, which throws on a non-touch context.
    c.check(nm("matrix-port-config"),
            page.viewport_size == cfg["viewport"],
            f"configured={cfg['viewport']} actual={page.viewport_size} touch={touch}")
    # 1) The history preamble IS the fresh-session long-prose followed run:
    #    no gesture, real SSE to done — asserted as matrix-prose-b.
    tel_p = ensure_history(page, HIST_SID)
    if tel_p is None:
        raise HarnessError(f"matrix expects a fresh page; {HIST_SID} already in store")
    matrix_b_checks(c, nm, "prose", tel_p, page)
    shot("prose-b")
    # 2) short-a over that history, then short-b over the SAME history (a
    #    follower carried down to the short answer — see the scenario docstring).
    tel_sa, st = scenario_short_over_history(page, HIST_SID, engine, port, cdp)
    shot("short-a")  # BEFORE the pill tap: the state (a) end frame is the
    #                  reader still up with the pill visible
    matrix_a_checks(c, nm, "short", st["gesture"], tel_sa, st["unpin"], st["moved"], page, port)
    # 2b) mixed-a also runs OVER the history: with the reveal off there is no
    # scroll room mid-stream (see scenario_mixed_over_history).
    tel_ma, st = scenario_mixed_over_history(page, HIST_SID, engine, port, cdp)
    shot("mixed-a")
    matrix_a_checks(c, nm, "mixed", st["gesture"], tel_ma, st["unpin"], st["moved"], page, port)
    # 3) The four self-tall shapes, a then b.
    for shape, script in MATRIX_SHAPES:
        if shape in ("prose", "short", "mixed"):
            continue
        if touch:
            tel_a, st = scenario_touch_up(page, script, f"probe-mx-{shape}-a", engine, port, cdp)
            unpin_m, moved_m, gesture = st["unpin"], st["moved"], st["gesture"]
        else:
            tel_a, st = scenario_stream(page, script, f"probe-mx-{shape}-a",
                                        wheel_dy=WHEEL_A)
            unpin_m = moved_m = st["wheel"]
            gesture = "wheel"
        shot(f"{shape}-a")  # state (a) end frame, before the pill tap
        matrix_a_checks(c, nm, shape, gesture, tel_a, unpin_m, moved_m, page, port)
        tel_b, _ = scenario_stream(page, script, f"probe-mx-{shape}-b",
                                   wheel_dy=None, settle=SETTLE_MS * 2)
        matrix_b_checks(c, nm, shape, tel_b, page)
        shot(f"{shape}-b")
    # 3b) mixed (b): a fresh-session follower. With the reveal off the whole
    #     answer lands in the FINALIZE render — the "block arrives while you
    #     follow" half of the shape the (a) cell measures as a reader.
    tel_mb, _ = scenario_stream(page, "shape-mixed", "probe-mx-mixed-b",
                                wheel_dy=None, settle=SETTLE_MS * 2)
    matrix_b_checks(c, nm, "mixed", tel_mb, page)
    shot("mixed-b")
    # 4) prose-a (its own wheel/touch row, same script as the preamble) and
    #    short-b (over the history session, above).
    if touch:
        tel_pa, st = scenario_touch_up(page, "control", "probe-mx-prose-a", engine, port, cdp)
        shot("prose-a")
        matrix_a_checks(c, nm, "prose", st["gesture"], tel_pa, st["unpin"], st["moved"],
                        page, port)
    else:
        tel_pa, st = scenario_stream(page, "control", "probe-mx-prose-a",
                                     wheel_dy=WHEEL_A)
        shot("prose-a")
        matrix_a_checks(c, nm, "prose", "wheel", tel_pa, st["wheel"], st["wheel"],
                        page, port)
    tel_sb, _ = scenario_short_over_history(page, HIST_SID, engine, port, cdp,
                                            gesture=False)
    matrix_b_checks(c, nm, "short", tel_sb, page)
    shot("short-b")
    return True


def run_guards(page, c, engine, port, cdp=None):
    """Criterion-4 regression guards (the legitimate scroll-to-bottom paths),
    per port: sending a message still jumps, reopening a session through
    openSession() still jumps, and the picker's change handler (sessions.js's
    OTHER renderSession call site) still jumps. The heal-follower and the
    stopped/error pair live in the mechanism suite (guard-heal-following,
    stop-*-flat/-line-landed)."""
    def nm(base):
        return f"[{engine}/{port}] {base}"
    print(f"== guards {engine}/{port} ==", flush=True)
    ensure_history(page, HIST_SID)

    def up(px):
        # Retry a missed gesture (see scroll_up_gesture); the guard's own
        # assertion still decides where the view must end.
        scroll_up_gesture(page, engine, port, cdp, px, min_gap=max(60, px - 60))

    # -- guard-send-jumps: addUserMsg's jump is unconditional by design ------
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(id) => window.__probe.openSession(id)", HIST_SID)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)
    up(400)
    up_ = metrics(page)
    page.evaluate("() => window.__probe.arm('guard-send', 'probe-guard-send')")
    send_message(page, "Probe guard-send: does sending still jump?")
    page.wait_for_timeout(250)  # the user row landed; the answer is at +2500 ms
    jumped = metrics(page)
    wait_done(page, label="guard-send done")
    page.wait_for_timeout(400)
    c.check(nm("guard-send-jumps"),
            gap(up_) >= 300 and up_["pinned"] is False
            and jumped["top"] >= jumped["h"] - jumped["ch"] - 2
            and jumped["pinned"] is True,
            f"gapBeforeSend={gap(up_):.0f}px pinnedBefore={up_['pinned']} "
            f"gapAfterSend={gap(jumped):.1f}px pinnedAfter={jumped['pinned']}")

    # -- guard-open-jumps: sessions.js openSession() call site ---------------
    up(600)
    up2 = metrics(page)
    page.evaluate("(id) => window.__probe.openSession(id)", HIST_SID)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)
    land2 = metrics(page)
    c.check(nm("guard-open-jumps"),
            gap(up2) >= 300
            and land2["h"] - land2["ch"] > 100
            and land2["top"] >= land2["h"] - land2["ch"] - 2
            and land2["pinned"] is True and land2["pill"] is False,
            f"gapBeforeOpen={gap(up2):.0f}px topAfter={land2['top']:.0f} "
            f"maxAfter={land2['h'] - land2['ch']} pinned={land2['pinned']} "
            f"pill={land2['pill']}")

    # -- guard-picker-jumps: the #session-select change handler call site ----
    has_opt = page.evaluate(
        "(id) => !!document.querySelector(`#session-select option[value=\"${id}\"]`)",
        HIST_SID)
    if not has_opt:
        raise HarnessError("the picker has no option for the history session — "
                           "the mock's /api/sessions rows are broken (guard needs "
                           "the REAL picker path)")
    up(600)
    up3 = metrics(page)
    page.select_option("#session-select", "")  # "+ New chat": the blank slate
    page.wait_for_timeout(250)
    page.select_option("#session-select", HIST_SID)  # real change event
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)
    land3 = metrics(page)
    c.check(nm("guard-picker-jumps"),
            gap(up3) >= 300
            and land3["h"] - land3["ch"] > 100
            and land3["top"] >= land3["h"] - land3["ch"] - 2
            and land3["pinned"] is True and land3["pill"] is False,
            f"gapBeforePick={gap(up3):.0f}px topAfter={land3['top']:.0f} "
            f"maxAfter={land3['h'] - land3['ch']} pinned={land3['pinned']} "
            f"pill={land3['pill']}")


def wait_tag(page, tag, timeout=20000):
    try:
        page.wait_for_function("(t) => window.__mock.tagAt[t] !== undefined",
                               arg=tag, timeout=timeout)
    except PlaywrightError as exc:
        raise HarnessError(f"the mock never emitted tag {tag!r}: {exc}") from None


def run_touch_rules(page, c, engine, port):
    """BK-029 (frontend-smoothness step 4, REVISED by the user 2026-10-31:
    "any tapping of screen to stop the following of streaming" — user
    control). Over `touchrules` (an overflowing log, then a streaming thinking
    strip, a slow tool pill, a word drip) and `trshort` (a dripped reply that
    never overflows), each on a fresh chat:
      touch-tap-unpins / mouse-click-unpins
                                a REAL bare press on text mid-answer (tap on the
                                iPhone port, mouse click on desktop): unpinned at
                                once, "↓ Latest" shown, and it STAYS unpinned
                                while the answer streams on below
      latest-repins             one tap/click on "↓ Latest" (outside #chat-log):
                                follows the rest of the stream and settles
      press-under-pill-stays-unpinned
                                a press right where the pill then appears: the
                                tap's own click (hit-tested at the lift) must not
                                land on the new pill and re-pin; a press that
                                starts ON the pill still resumes
      synth-click-on-pill-repins  a click with no press before it (VoiceOver) re-pins
      touch-drag-unpins         iPhone: a touch drag unpins (dispatched — no
                                WebKit drag API; the matrix has chromium's real one)
      press-unscrollable-keeps  a press on a log that cannot scroll changes
                                nothing (canScroll gate, as before)
      d1-keyboard-pill-pauses   Enter on the focused running pill: the drawer
                                opens, following pauses (no press saw it — D1's
                                readerOpened), drawer still on screen mid-drip
      d1-latest-resumes         one tap/click on "↓ Latest" resumes after that
      d1-idle-open-keeps        after done, an open changes nothing (as before)
      d1-keyboard-think-pauses  Enter on the streaming thinking header: opens,
                                pauses; closing it does NOT resume"""
    def nm(base):
        return f"[{engine}/{port}] {base}"
    touch = port == "iphone"
    print(f"== touch rules {engine}/{port} ==", flush=True)

    def start(script, sid, tag, after_ms):
        page.evaluate("() => window.__probe.reset()")
        page.select_option("#session-select", "")  # fresh chat via the real picker
        page.wait_for_timeout(200)
        page.evaluate("([n, s]) => window.__probe.arm(n, s)", [script, sid])
        send_message(page, f"Probe {sid}: touch rules")
        wait_tag(page, tag)
        page.wait_for_timeout(after_ms)

    def press_at(x, y):
        if touch:
            page.touchscreen.tap(x, y)
        else:
            page.mouse.click(x, y)
        page.wait_for_timeout(150)

    def press(sel):
        bx = page.locator(sel).last.bounding_box()
        if bx is None:
            raise HarnessError(f"nothing to press: {sel} has no box")
        press_at(bx["x"] + bx["width"] / 2, bx["y"] + bx["height"] / 2)

    def key_open(sel):
        # Focus without scrolling (a focus scroll would be a programmatic move,
        # not the reader's), then the keyboard's own activation of the button.
        page.evaluate("(s) => [...document.querySelectorAll(s)].pop().focus({ preventScroll: true })", sel)
        page.keyboard.press("Enter")
        page.wait_for_timeout(150)

    def finish(label):
        wait_done(page, label=f"touchrules done ({label})")
        page.wait_for_timeout(SETTLE_MS)
        return metrics(page)

    now = lambda: page.evaluate("() => performance.now()")
    in_view = lambda sel: page.evaluate(
        """(s) => { const e = [...document.querySelectorAll(s)].pop(); if (!e || e.hidden) return false;
          const r = e.getBoundingClientRect(), l = document.querySelector('#chat-log').getBoundingClientRect();
          return r.bottom > l.top + 4 && r.top < l.bottom - 4; }""", sel)
    scrollable = lambda: page.evaluate(
        "() => { const l = document.querySelector('#chat-log'); return l.scrollHeight - l.clientHeight > 1; }")

    # 1) The bare press mid-answer, then "↓ Latest".
    press_row = "touch-tap-unpins" if touch else "mouse-click-unpins"
    start("touchrules", "probe-tr-press", "tr-drip", 300)
    before = metrics(page)
    t0 = now()
    press_at(*text_point(page))
    at = metrics(page)
    page.wait_for_timeout(1200)
    mid = metrics(page)
    t1 = now()
    held = [s for s in collect(page)["samples"] if t0 + 150 <= s["t"] <= t1]
    c.check(nm(press_row),
            before["pinned"] is True and at["pinned"] is False and at["pill"] is True
            and len(held) > 10 and all(not s["pinned"] and s["pill"] for s in held)
            and gap(mid) > gap(at) + 40,
            f"pinned@before={before['pinned']} pinned@press={at['pinned']} pill@press={at['pill']} "
            f"samples={len(held)} pinnedSamples={sum(bool(s['pinned']) for s in held)} "
            f"gap {gap(at):.0f} -> {gap(mid):.0f}px (the answer streams on below)")
    press("#chat-jump")
    t2 = now()
    fin = finish("press")
    after = [s for s in collect(page)["samples"] if s["t"] >= t2 + 150]
    c.check(nm("latest-repins"),
            after and all(s["pinned"] for s in after)
            and fin["pinned"] is True and fin["pill"] is False and gap(fin) <= 2,
            f"samplesAfterTap={len(after)} unpinnedAfter={sum(not s['pinned'] for s in after)} "
            f"pinnedEnd={fin['pinned']} pillEnd={fin['pill']} gapEnd={gap(fin):.1f}px")

    # 1b) A press exactly where the pill APPEARS (bottom centre, over the
    # log): a tap's click is hit-tested at the lift, so the pill the press
    # just revealed would take that click and re-pin — the press undone.
    start("touchrules", "probe-tr-underpill", "tr-drip", 300)
    spot = page.evaluate(
        """() => { const l = document.querySelector('#chat-log').getBoundingClientRect();
          const x = l.left + l.width / 2, y = l.bottom - 34, e = document.elementFromPoint(x, y);
          const ok = !!e && !!e.closest('#chat-log') && !e.closest('button, a, summary, [role=button]');
          return ok ? [x, y] : null; }""")
    if spot is None:
        raise HarnessError("the pill's spot is not plain log content mid-drip")
    press_at(*spot)
    at = metrics(page)
    page.wait_for_timeout(600)
    later = metrics(page)
    press("#chat-jump")  # a press ON the pill still resumes
    fin = finish("underpill")
    c.check(nm("press-under-pill-stays-unpinned"),
            at["pinned"] is False and at["pill"] is True
            and later["pinned"] is False and later["pill"] is True
            and fin["pinned"] is True and gap(fin) <= 2,
            f"spot=({spot[0]:.0f},{spot[1]:.0f}) pinned@press={at['pinned']} pill@press={at['pill']} "
            f"pinned+600ms={later['pinned']} | after a press on the pill: pinnedEnd={fin['pinned']} "
            f"gapEnd={gap(fin):.1f}px")

    # 1c) A click with NO press before it (VoiceOver's synthesized activation,
    # detail 1): the ghost-click guard must let it through, or the pill is dead.
    start("touchrules", "probe-tr-synth", "tr-drip", 300)
    press_at(*text_point(page))
    unp = metrics(page)
    page.wait_for_timeout(700)  # past follow.js's 400ms press-clear: this click has no press of its own
    page.evaluate("() => document.querySelector('#chat-jump').dispatchEvent("
                  "new MouseEvent('click', { bubbles: true, detail: 1 }))")
    page.wait_for_timeout(150)
    fin = finish("synth")
    c.check(nm("synth-click-on-pill-repins"),
            unp["pinned"] is False and fin["pinned"] is True and gap(fin) <= 2,
            f"pinned@press={unp['pinned']} pinnedEnd={fin['pinned']} gapEnd={gap(fin):.1f}px")

    # 2) A drag (touch port).
    if touch:
        start("touchrules", "probe-tr-drag", "tr-drip", 300)
        m0 = metrics(page)
        synth_drag(page, 40)
        page.wait_for_timeout(80)
        m1 = metrics(page)
        finish("drag")
        c.check(nm("touch-drag-unpins"),
                m0["pinned"] is True and m1["pinned"] is False and m1["pill"] is True,
                f"pinned {m0['pinned']} -> {m1['pinned']} pill={m1['pill']}")

    # 3) A log that cannot scroll: nothing to stop, nothing changes.
    start("trshort", "probe-tr-short", "trs-drip", 400)
    can = scrollable()
    press_at(*text_point(page))
    m_s = metrics(page)
    fin = finish("short")
    c.check(nm("press-unscrollable-keeps"),
            can is False and m_s["pinned"] is True and m_s["pill"] is False
            and fin["pinned"] is True and fin["pill"] is False,
            f"scrollable={can} pinned@press={m_s['pinned']} pill@press={m_s['pill']} "
            f"pinnedEnd={fin['pinned']}")

    # 4) D1 by keyboard: the running pill.
    start("touchrules", "probe-tr-kpill", "tr-tool", 400)
    m0 = metrics(page)
    key_open(".pill.running")
    opened = page.evaluate("() => !document.querySelector('.pill-detail').hidden")
    m_open = metrics(page)
    wait_tag(page, "tr-drip")
    page.wait_for_timeout(1500)
    m_mid = metrics(page)
    drawer_seen = in_view(".pill-detail")
    press("#chat-jump")
    fin = finish("kpill")
    c.check(nm("d1-keyboard-pill-pauses"),
            m0["pinned"] is True and opened and m_open["pinned"] is False and m_open["pill"] is True
            and m_mid["pinned"] is False and drawer_seen and gap(m_mid) > gap(m_open),
            f"pinned@before={m0['pinned']} opened={opened} pinned@open={m_open['pinned']} "
            f"pill@open={m_open['pill']} pinned@drip={m_mid['pinned']} drawerInView@drip={drawer_seen} "
            f"gap {gap(m_open):.0f} -> {gap(m_mid):.0f}px")
    c.check(nm("d1-latest-resumes"),
            fin["pinned"] is True and fin["pill"] is False and gap(fin) <= 2,
            f"pinnedEnd={fin['pinned']} pillEnd={fin['pill']} gapEnd={gap(fin):.1f}px")
    # Idle: the run is over. Close, then re-open in place — the same click
    # handler every input reaches; nothing may change. Wait for the paced
    # reveal's tail first: a close mid-reveal is BK-017's clamp case, not D1.
    pill_el = page.locator(".pill").last
    wait_log_static(page, quiet_ms=300, timeout=6000)
    pill_el.dispatch_event("click")
    page.wait_for_timeout(150)
    m_shut = metrics(page)
    pill_el.dispatch_event("click")
    page.wait_for_timeout(150)
    reopened = page.evaluate("() => !document.querySelector('.pill-detail').hidden")
    m_idle = metrics(page)
    c.check(nm("d1-idle-open-keeps"),
            reopened and m_idle["pinned"] is True and m_idle["pill"] is False,
            f"reopened={reopened} pinned={m_idle['pinned']} pill={m_idle['pill']} "
            f"pinned@close={m_shut['pinned']} gap@close={gap(m_shut):.1f}")

    # 5) D1 by keyboard: the streaming thinking strip; closing never resumes.
    start("touchrules", "probe-tr-kthink", "tr-think", 600)
    m0 = metrics(page)
    key_open(".think.streaming .think-head")
    t_open = page.evaluate("() => document.querySelector('.think').classList.contains('open')")
    m_open = metrics(page)
    key_open(".think .think-head")
    t_closed = page.evaluate("() => !document.querySelector('.think').classList.contains('open')")
    m_close = metrics(page)
    finish("kthink")
    c.check(nm("d1-keyboard-think-pauses"),
            m0["pinned"] is True and t_open and m_open["pinned"] is False and m_open["pill"] is True
            and t_closed and m_close["pinned"] is False and m_close["pill"] is True,
            f"pinned@before={m0['pinned']} opened={t_open} pinned@open={m_open['pinned']} "
            f"pill@open={m_open['pill']} closed={t_closed} pinned@close={m_close['pinned']} "
            f"pill@close={m_close['pill']}")


def run_anchor_growth(page, c, engine, port):
    """Residual R1 (gates the overflow-anchor decision): content INSERTED
    ABOVE an unpinned parked reader. The production transcript is append-only
    — nothing in the app inserts above a reader (the renderSession swap does,
    and it is JS-bracketed) — so this drives the browser rule directly: with
    the shipped `overflow-anchor: none` the view must NOT move (the text
    slides — the cost); with `auto` the browser compensates (the benefit it
    buys elsewhere). A/B arms measured, shipped state asserted."""
    def nm(base):
        return f"[{engine}/{port}] {base}"
    print(f"== anchors R1 (growth above) {engine}/{port} ==", flush=True)
    ensure_history(page, HIST_SID)  # self-provisioned: --only=anchors must stand
    computed = page.evaluate("() => window.__probe.anchorGet()")
    c.check(nm("anchor-computed-none"), computed == "none",
            f"computed overflow-anchor={computed!r} (chat.css #chat-log)")
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(id) => window.__probe.openSession(id)", HIST_SID)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)
    hover_log(page)
    wheel_up(page, -600)
    parked = metrics(page)
    if parked["pinned"] is not False or gap(parked) < 400:
        raise HarnessError(f"anchor R1 needs an unpinned parked view, got "
                           f"pinned={parked['pinned']} gap={gap(parked):.0f}px")

    def arm(label):
        m0 = metrics(page)
        page.evaluate("() => window.__probe.insertAbove(240)")
        page.wait_for_timeout(250)  # the anchoring compensation, if any, lands
        m1 = metrics(page)
        t1 = page.evaluate("() => performance.now()")
        # Both bounds are page values: `t1` must be PASSED. It used to sit in
        # the JS string as a free name — a ReferenceError inside the callback,
        # hidden until now by `&&` short-circuiting: with no write inside the
        # window the upper bound is never evaluated, so the arm that exists to
        # prove "zero writes" was the only one that could not throw, and any
        # REAL write in the window crashed the harness (exit 2) instead of
        # failing the check.
        writes = page.evaluate(
            "([t0, t1]) => window.__T.writes.filter(w => w.t >= t0 - 8 && w.t <= t1)",
            [m0["t"], t1])
        print(f"ANCHOR-R1 {engine}/{port} {label}: dTop={m1['top'] - m0['top']:+.1f}px "
              f"dH={m1['h'] - m0['h']:+.0f}px writes={len(writes)} "
              f"gap {gap(m0):.0f} -> {gap(m1):.0f}px", flush=True)
        return m0, m1, writes

    m0, m1, w_none = arm("none(shipped)")
    d_none = m1["top"] - m0["top"]
    page.evaluate("() => window.__probe.removeGrow()")
    page.wait_for_timeout(150)
    page.evaluate("() => window.__probe.anchorSet('auto')")
    m2, m3, w_auto = arm("auto")
    d_auto = m3["top"] - m2["top"]
    page.evaluate("() => window.__probe.removeGrow()")
    page.evaluate("() => window.__probe.anchorSet('')")  # back to the stylesheet
    # The shipped contract: scrollTop unmoved (so the READER'S TEXT slid down
    # by the inserted height), zero writes. The auto arm is printed above —
    # it is the measurement behind the keep/go call, not an assertion.
    c.check(nm("anchor-growth-above-none"),
            abs(d_none) <= 2 and not w_none
            and abs((gap(m1) - gap(m0)) - 240) <= 2,
            f"dTop_none={d_none:+.1f}px writes_none={len(w_none)} | "
            f"dTop_auto={d_auto:+.1f}px writes_auto={len(w_auto)} "
            f"(auto = what anchoring WOULD compensate above a reader)")


def run_gap0_carry(page, c, engine, port):
    """Residual R2 (the rejection the decision entry recorded but never
    measured): a stray tap unpins an at-bottom reader WITHOUT moving it
    (touchstart unpins, no scroll); does growth ABOVE the view carry it down
    and read as intent (maybeRepin re-pins)? With the shipped
    `overflow-anchor: none` it must not; with `auto` BOTH engines carry at
    gap 0 — measured here (chromium and WebKit: dTop=+240px write-free, then
    the repin). Step 1's "WebKit has no carry" was about the bottom EDGE
    (growth below the view); growth ABOVE a scroller compensates on WebKit
    too (measured +120px, step 3) — and at gap 0 that is this insert."""
    def nm(base):
        return f"[{engine}/{port}] {base}"
    print(f"== anchors R2 (gap-0 carry) {engine}/{port} ==", flush=True)
    ensure_history(page, HIST_SID)  # self-provisioned (see R1)

    def gap0_run(sid):
        page.evaluate("() => window.__probe.reset()")
        # Fresh chat through the REAL picker handler (startSession): the gap0
        # answer's own 6 + drip paragraphs are the scroll room, and the spring
        # must settle at THIS answer's bottom before the tap.
        page.select_option("#session-select", "")
        page.wait_for_timeout(200)
        page.evaluate("([n, s]) => window.__probe.arm(n, s)", ["gap0", sid])
        send_message(page, f"Probe {sid}: tap at the bottom, then watch it grow")
        wait_log_tall_ctx(page, sid)
        # Park while the script's tagged STATIC window is open (1.7 s): the
        # tap-unpin, the park at gap 0 and the insertAbove all complete before
        # the drip resumes, so the ONLY mover of the parked view is the
        # browser's own anchor.
        try:
            page.wait_for_function(
                "() => window.__mock.tagAt['gap0-static-window'] !== undefined",
                timeout=10000)
        except PlaywrightError as exc:
            raise HarnessError(f"gap0 static window never opened ({sid}): {exc}") from None
        # Settle THEN silence, both before the tap: the settle test only
        # checks WHERE the spring is, not that it stopped — the reveal keeps
        # growing the log after the last frame, the spring chases it, and a
        # mid-chase tap hands the auto arm a frame() write indistinguishable
        # from the browser's carry (measured 2026-10-29: one chase write at
        # +2 ms made the none-vs-auto comparison meaningless).
        page.wait_for_timeout(200)  # the spring starts from the settled frame
        try:
            page.wait_for_function(
                """() => { const l = document.querySelector('#chat-log');
                  return l.scrollTop >= l.scrollHeight - l.clientHeight - 1
                      && l.scrollHeight > l.clientHeight; }""",
                timeout=3000)
        except PlaywrightError as exc:
            raise HarnessError(f"the spring never settled at the bottom ({sid}): {exc}") from None
        wait_log_static(page, quiet_ms=250, timeout=4000)  # spring SILENT now
        box, cx, cy = log_center(page)
        page.touchscreen.tap(cx, cy)
        page.wait_for_timeout(120)
        at = metrics(page)  # the stray-tap state: unpinned, gap 0
        if gap(at) > 2 or at["pinned"] is not False:
            raise HarnessError(f"the tap did not park the view at gap 0 unpinned "
                               f"({sid}): gap={gap(at):.1f} pinned={at['pinned']}")
        page.evaluate("() => window.__probe.mark('insertAt')")
        page.evaluate("() => window.__probe.insertAbove(240)")
        # The spring's rAF, if one is still queued, runs within ~17 ms of the
        # silence window opening — burn it BEFORE the measured window, so a
        # frame() write can only mean the tap failed to stop the spring.
        page.wait_for_timeout(80)
        mark = page.evaluate("() => performance.now()")
        # 120 ms window: the anchoring compensation lands IN THE MUTATION
        # TASK, write-free by construction; the repin's follow() write needs
        # the carry's scroll event first, so it lands AFTER this window (and
        # is reported in the detail). Same instant read in BOTH arms — the
        # arms differ only in the stylesheet value, so the comparison is
        # honest: none moves 0 px, auto moves the inserted height.
        page.wait_for_timeout(120)
        grown = metrics(page)
        grows = page.evaluate(
            "(t0) => window.__T.writes.filter(w => w.t >= t0 && w.t <= t0 + 120)"
            " .map(w => ({t: Math.round(w.t - t0), stack: w.stack}))", mark)
        print(f"ANCHOR-R2 {engine}/{port} {sid}: dTop={grown['top'] - at['top']:+.1f}px "
              f"dH={grown['h'] - at['h']:+.0f}px writes={len(grows)} "
              f"gap {gap(at):.0f} -> {gap(grown):.0f}px pinned={grown['pinned']}",
              flush=True)
        for w in grows:
            print(f"  GROW-WRITE t={w['t']:+} {w['stack']}", flush=True)
        # Keep the .probe-grow block until done: removing it would itself
        # clamp the auto arm's bottom-parked view, and that clamp is not the
        # residual being measured.
        wait_done(page, label=f"gap0 done ({sid})")
        page.wait_for_timeout(SETTLE_MS)
        page.evaluate("() => window.__probe.removeGrow()")
        page.wait_for_timeout(250)
        return at, grown, grows

    at1, g1, w1 = gap0_run("probe-gap0-none")
    fin1 = metrics(page)  # grown state HELD to done + settle (grow block stays)
    auto_style = page.evaluate("() => window.__probe.anchorSet('auto')")
    at2, g2, w2 = gap0_run("probe-gap0-auto")
    fin2 = metrics(page)
    page.evaluate("() => window.__probe.anchorSet('')")
    # The static-window measurement: with the shipped rule the parked view
    # stays put (the inserted text slides it — the priced cost); the auto arm
    # is what the browser's own anchor WOULD have carried — the keep call's
    # other half. Print first: the named checks below read these numbers.
    print(f"ANCHOR-R2 {engine}/{port} none: dTop={g1['top'] - at1['top']:+.1f}px "
          f"writes={len(w1)} gapEnd={gap(fin1):.0f}px pinnedEnd={fin1['pinned']} | "
          f"auto(style={auto_style}): dTop={g2['top'] - at2['top']:+.1f}px "
          f"writes={len(w2)} gapEnd={gap(fin2):.0f}px pinnedEnd={fin2['pinned']}",
          flush=True)
    # The shipped contract: the tap unpinned AT gap 0 and the insert did NOT
    # carry the view — the view stays while the reader's text slides (gap
    # widens by the inserted height), no write, no write-free re-pin, pill in
    # hand. That slid text is the PRICED COST of the rule; the auto arm is
    # what it buys.
    c.check(nm("anchor-none-gap0-no-carry"),
            at1["pinned"] is False and at1["pill"] is True and gap(at1) <= 2
            and abs(g1["top"] - at1["top"]) <= 2 and not w1
            and abs(gap(g1) - gap(at1) - 240) <= 2
            and fin1["pinned"] is False and fin1["pill"] is True,
            f"none arm: dTop={g1['top'] - at1['top']:+.1f}px writes={len(w1)} "
            f"gapEnd={gap(fin1):.0f}px pinnedEnd={fin1['pinned']} pillEnd={fin1['pill']}")
    # The auto arm, BOTH engines (step 1's "WebKit has no bottom-edge carry"
    # is about pure growth BELOW the view; growth ABOVE a scroller compensates
    # on both — measured +120px (BK-017 step 3) — and at gap 0 that IS
    # this insert). The harm chain is engine-wide: the browser's write-free
    # carry lands the view inside maybeRepin's 40 px window, the scroll event
    # reads as intent, the stray-tap reader is re-pinned and yanked.
    c.check(nm("anchor-auto-gap0-carries"),
            (g2["top"] - at2["top"]) >= 200 and not w2 and gap(g2) <= 44,
            f"auto arm: dTop={g2['top'] - at2['top']:+.1f}px writes={len(w2)} "
            f"(write-free = the BROWSER moved it) pinnedEnd={fin2['pinned']} "
            f"(= maybeRepin read the carry as intent)")




def main() -> None:
    if sync_playwright is None:
        print(
            f"harness error: the 'playwright' Python package is required ({IMPORT_ERR!r}) "
            "— it lives in the system python3 on this machine, run the probe with that; "
            "no browser download needed, the CACHED chrome-headless-shell is used "
            "via executable_path",
            file=sys.stderr,
        )
        sys.exit(2)
    only, eng_f, port_f = parse_args(sys.argv[1:])
    build_site()
    jsc_check()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), partial(_Quiet, directory=str(SITE)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    c = Checks()
    no_webkit = webkit_ready()
    engines = ["chromium"] if no_webkit else ["chromium", "webkit"]
    if eng_f:
        engines = [e for e in engines if e in eng_f]
    if no_webkit:
        c.skip("engine-webkit", no_webkit)
    all_pageerrors: list[tuple[str, str]] = []

    try:
        with sync_playwright() as pw:
            for engine in engines:
                pageerrors: list[str] = []
                console_tail: list[str] = []

                def dump_diagnostics(page, engine=engine, console_tail=console_tail) -> None:
                    """Where a scenario wedged: recent console lines + what the mock saw."""
                    print(f"--- diagnostics [{engine}] ---")
                    for line in console_tail[-25:]:
                        print(f"  console: {' '.join(line.split())}")
                    try:
                        state = page.evaluate(
                            """() => ({
                                 calls: (window.__mock && window.__mock.calls || []).slice(-12),
                                 chatArmed: !!(window.__mock && window.__mock.chat),
                                 doneAt: window.__mock && window.__mock.doneAt,
                                 runClosed: window.__mock && window.__mock.runClosed,
                                 tagAt: (window.__mock && window.__mock.tagAt) || null,
                                 mode: (document.querySelector('#chat-send') || {}).dataset
                                       && document.querySelector('#chat-send').dataset.mode,
                                 log: (() => { const l = document.querySelector('#chat-log');
                                   return l ? { kids: l.children.length,
                                                sh: l.scrollHeight, ch: l.clientHeight } : null; })(),
                               })"""
                        )
                        print("  state: " + json.dumps(state)[:800])
                    except Exception as exc:  # noqa: BLE001 — diagnostics never fail the run
                        print(f"  state: unavailable ({exc})")
                    print("--- end diagnostics ---")

                try:
                    browser = launch_engine(pw, engine)
                except PlaywrightError as exc:
                    c.skip(f"engine-{engine}",
                           f"launch failed: {' '.join(str(exc).split())}")
                    continue

                def boot_page(port_cfg):
                    """One page per (engine, port). The iPhone port: 390x844 with
                    hasTouch on BOTH engines — real touch events exist there.
                    is_mobile is a Chromium capability (Playwright: chromium-only
                    emulation); WebKit gets the same viewport with real touch and
                    no UA/DPR lie — stated here, stated in the README."""
                    page = browser.new_page(
                        viewport=dict(port_cfg["viewport"]),
                        has_touch=port_cfg["has_touch"],
                        **({"is_mobile": True}
                           if port_cfg["is_mobile"] and engine == "chromium" else {}),
                    )
                    page.on("pageerror", lambda e: pageerrors.append(str(e)))
                    page.on("console", lambda m: console_tail.append(f"[{m.type}] {m.text}"))
                    page.goto(f"http://127.0.0.1:{port}/probe.html", wait_until="load")
                    try:
                        page.wait_for_function(
                            "() => window.__probe && window.__probe.ready === true",
                            timeout=20000)
                    except PWTimeout:
                        raise HarnessError(
                            f"probe/app boot never completed on {engine}/"
                            f"{port_cfg['name']} (probe_scroll.ready false after 20 s)") from None
                    cdp = None
                    if engine == "chromium" and port_cfg["has_touch"]:
                        # The CDP input channel for the touch DRAG — the one
                        # gesture Playwright's Touchscreen API cannot drive.
                        cdp = page.context.new_cdp_session(page)
                    return page, cdp

                try:
                    for port_cfg in PORTS:
                        if port_f and port_cfg["name"] not in port_f:
                            continue
                        page, cdp = boot_page(port_cfg)
                        pname = f"{engine}/{port_cfg['name']}"
                        try:
                            if "mechanisms" in only and port_cfg["name"] == "desktop":
                                # The mechanism suite is the desktop diagnosis
                                # (its clamp-window arithmetic is port-free;
                                # the matrix covers BOTH ports).
                                run_scenarios(page, c, pageerrors, engine)
                            if "recover" in only and port_cfg["name"] == "desktop":
                                # Recovery paths are port-free — no gesture, no
                                # geometry: the desktop page runs them once.
                                run_recover(page, c, engine)
                            if "matrix" in only:
                                run_matrix(page, c, engine, port_cfg["name"], cdp)
                            if "guards" in only:
                                run_guards(page, c, engine, port_cfg["name"])
                                run_touch_rules(page, c, engine, port_cfg["name"])
                            if "anchors" in only:
                                if port_cfg["name"] == "desktop":
                                    run_anchor_growth(page, c, engine, port_cfg["name"])
                                else:
                                    run_gap0_carry(page, c, engine, port_cfg["name"])
                        except (PlaywrightError, HarnessError) as e:
                            # PlaywrightError (the base, not just TimeoutError):
                            # a page-side fixture throw is a harness break too —
                            # exit 2 WITH diagnostics, not a traceback that skips
                            # the remaining ports and the summary.
                            print(f"harness error: a scenario broke on {pname} — cannot "
                                  f"measure: {e}", file=sys.stderr)
                            dump_diagnostics(page)
                            browser.close()
                            sys.exit(2)
                        for e in pageerrors:
                            print(f"PAGEERROR [{pname}] {' '.join(str(e).split())}")
                            all_pageerrors.append((pname, e))
                        pageerrors.clear()
                finally:
                    browser.close()
    finally:
        httpd.shutdown()

    print()
    for line in c.lines:
        print(line)
    if not c.lines:
        # An empty selection must not be a green light: --only=mechanisms with
        # --ports=iphone runs nothing (mechanisms are desktop-only), and a
        # filter typo that still parses would otherwise exit 0 with 0 checks.
        print("usage error: no checks ran — --only/--engines/--ports selected "
              "nothing (mechanisms run on the desktop port only)", file=sys.stderr)
        sys.exit(2)
    fails = c.fails
    print(f"\n{len(fails)} failing check(s), {len(c.skips)} skipped, "
          f"{len(all_pageerrors)} pageerror(s)")
    # Rollup guards (pwa_resume precedent): "green" must mean every selected
    # engine CONTRIBUTED its rows and both engines ran the SAME set. Checked
    # before the verdict line so PROBE-RESULT never reads PASS above a
    # structural break; a structural break exits 2 — cannot measure, not "1".
    rows = engine_assertion_rows(c.lines)
    # Order matters: a silent engine has NO entry in rows, so set-identity
    # (which compares present engines) cannot see it — the planted webkit
    # early-return proves the zero-row check must run first, on every scope.
    problem = empty_selection_problem(rows)
    if problem is None:
        problem = silent_engine_problem(engines, c.skips, rows)
    if problem is None:
        problem = engine_set_problem(rows)
    if problem is None and only == set(FAMILIES) and not port_f:
        # The floor speaks on FULL matrix runs only: scoped runs legitimately
        # produce fewer rows (--ports=desktop drops a whole port's checks).
        problem = coverage_problem(engines, c.skips, rows)
    if problem:
        print(f"structural guard: {problem}", file=sys.stderr)
        print("PROBE-RESULT: FAIL")
        sys.exit(2)
    print("PROBE-RESULT: " + ("PASS" if not fails and not all_pageerrors else "FAIL"))
    sys.exit(0 if not fails and not all_pageerrors else 1)


def run_scenarios(page, c: Checks, pageerrors, engine="chromium") -> None:
    def nm(base):
        return f"[{engine}] {base}"

    sid_trigger = "probe-scroll-trigger"

    # -- positive control for the scrollTop setter spy -------------------------
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("() => window.__probe.scrollTop(37)")  # a JS write with no gesture
    ctl = page.evaluate("() => ({ w: window.__T.writes.length, first: window.__T.writes[0] || null })")
    c.check(nm("instrument: scrollTop spy catches a JS write"),
            ctl["w"] >= 1 and ctl["first"] is not None and "scrollTop" in json.dumps(ctl["first"]),
            f"writes={ctl['w']} firstStack={(ctl['first'] or {}).get('stack', '')[:60]}")

    # -- trigger: wheel-unpinned, live-SSE completion — the discriminator ------
    # The naive reading of the board ("some scroll fires at completion on the
    # live path") predicts a jump HERE. Chromium says otherwise: the done path
    # makes no scroll call while unpinned (follow() early-returns) and nothing
    # shrinks at done (the thinking strip collapses at SEGMENT end, not done),
    # so the browser has no clamp to perform. A flat trigger is what makes the
    # heal (a) and stale-pinned (c) repros below the named mechanisms — the
    # shrink-* scenarios are what put candidate (b) itself on trial.
    tel, st = scenario_stream(page, "trigger", sid_trigger, wheel_dy=WHEEL_TRIGGER)
    pre, post = pre_post(tel)
    jump = round(post["top"] - pre["top"], 2)
    dh = post["h"] - pre["h"]
    win_writes = writes_in(tel, -32, SETTLE_MS)
    telemetry_table(tel, f"trigger {engine}")
    page.screenshot(path=str(WORK / f"trigger-after-done-{engine}.png"))

    c.check(nm("instrument: wheel gesture scrolled the log and unpinned"),
            st["wheel"]["pinned"] is False and st["wheel"]["pill"] is True
            and (st["wheel"]["h"] - st["wheel"]["ch"] - st["wheel"]["top"]) >= WHEEL_MIN_GAP,
            f"topAtWheel={st['wheel']['top']:.0f} maxAtWheel={st['wheel']['h'] - st['wheel']['ch']} "
            f"topAtDone={pre['top']:.0f} pinned@done={pre['pinned']} pill@done={pre['pill']}")
    c.check(nm("live-path-flat: wheel-unpinned live SSE done moves NOTHING (no JS write, no clamp)"),
            abs(jump) <= 2 and not win_writes,
            f"dscrollTop={jump:+.2f}px scrollHeightDelta={dh:+.0f}px "
            f"gapAtDone={post['h'] - post['ch'] - post['top']:.0f}px writes={len(win_writes)}")
    c.check(nm("no-done-shrink: scrollHeight does NOT shrink at done "
               "(the done frame gives a clamp nothing to bite on here)"),
            dh > -2, f"dScrollHeight={dh:+.0f}px")

    # -- trigger variant: bullet list ------------------------------------------
    tel_l, st_l = scenario_stream(page, "trigger-list", "probe-scroll-list", wheel_dy=WHEEL_TRIGGER)
    pre_l, post_l = pre_post(tel_l)
    jump_l = round(post_l["top"] - pre_l["top"], 2)
    win_l = writes_in(tel_l, -32, SETTLE_MS)
    telemetry_table(tel_l, f"trigger-list {engine}")
    c.check(nm("live-path-flat-list: list answer behaves the same when unpinned (flat, no writes)"),
            abs(jump_l) <= 2 and not win_l, f"dscrollTop={jump_l:+.2f}px writes={len(win_l)}")

    # -- control: same length, plain prose -------------------------------------
    tel_c, st_c = scenario_stream(page, "control", "probe-scroll-control", wheel_dy=WHEEL_TRIGGER)
    pre_c, post_c = pre_post(tel_c)
    jump_c = round(post_c["top"] - pre_c["top"], 2)
    win_c = writes_in(tel_c, -32, SETTLE_MS)
    telemetry_table(tel_c, f"control {engine}")
    page.screenshot(path=str(WORK / f"control-after-done-{engine}.png"))
    c.check(nm("control-plain-flat: plain reply keeps the read position (|d|<=2px, no writes)"),
            abs(jump_c) <= 2 and not win_c,
            f"dscrollTop={jump_c:+.2f}px writes={len(win_c)}")

    # -- candidate (c): keyboard scroll-up never unpins -------------------------
    def keyboard_scroll(p):
        p.focus("#chat-log .think-head")          # a real focusable transcript control
        p.evaluate("() => window.__probe.mark('focusAt')")
        for _ in range(4):
            p.keyboard.press("PageUp")
            p.wait_for_timeout(60)
        p.evaluate("() => window.__probe.mark('pageup')")

    tel_k, st_k = scenario_stream(page, "keyboard", "probe-scroll-keyboard",
                                  wheel_dy=None, before_done=keyboard_scroll)
    pre_k, post_k = pre_post(tel_k)
    # The read position the keyboard achieved (the keyboard scroll-up can be
    # mid-stream — the spring may start yanking before done even fires).
    seq_k = [s for s in tel_k["samples"] if s["t"] >= tel_k["meta"].get("focusAt", 0)]
    low = None
    if seq_k:
        kmin = min(s["top"] for s in seq_k)
        # The LAST sample at the keyboard's low point, not the first: the
        # log sits at top=0 while the answer starts streaming, so the FIRST
        # min-top sample can be the pre-PageUp state (legitimately pinned,
        # pill hidden). The keyboard reader's claim is where the view ENDED
        # up — by then the unpin's scroll event has landed.
        low = [s for s in seq_k if s["top"] <= kmin + 0.5][-1]
    key_up = (st_k["tall"]["top"] - low["top"]) if low else 0.0
    # Post-fix (mech c): the scroll handler unpins on the FIRST PageUp, so the
    # spring must be silent from then on. Measured after the `pageup` mark (all
    # four keys landed + 240 ms): between focusAt and the unpin's scroll event
    # a frame write is a race the probe must not lose on purpose.
    pageup_at = tel_k["meta"].get("pageup", 0)
    spring_writes = [w for w in tel_k["writes"]
                     if w["t"] >= pageup_at
                     and ("frame" in w["stack"] or "snap" in w["stack"]
                          or "jumpToBottom" in w["stack"])]
    telemetry_table(tel_k, f"keyboard {engine}")
    page.screenshot(path=str(WORK / f"keyboard-after-done-{engine}.png"))
    c.check(nm("mech-c-keyboard-unpins: a keyboard scroll-up unpins (pill shown)"),
            low is not None and key_up >= 60 and low["pinned"] is False and low["pill"] is True,
            f"scrolledUp={key_up:.0f}px pinned={low['pinned'] if low else '?'} "
            f"pill={low['pill'] if low else '?'}")
    c.check(nm("mech-c-spring-silent: the spring never writes scrollTop after the keyboard scroll-up"),
            not spring_writes,
            f"springWrites={len(spring_writes)} first={spring_writes[0]['stack'][:120] if spring_writes else '-'}")
    c.check(nm("mech-c-stays-up: completion leaves the keyboard reader up (flat, still unpinned)"),
            abs(post_k["top"] - pre_k["top"]) <= 2 and post_k["pinned"] is False
            and post_k["pill"] is True
            and post_k["top"] <= post_k["h"] - post_k["ch"] - 60,
            f"dscrollTop={post_k['top'] - pre_k['top']:+.2f}px finalTop={post_k['top']:.0f} "
            f"finalMax={post_k['h'] - post_k['ch']} pinned@done={post_k['pinned']} "
            f"pill@done={post_k['pill']}")

    # -- candidate (a) post-fix: the completion HEAL renders IN PLACE ----------
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(id) => window.__probe.openSession(id)", sid_trigger)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    opened = metrics(page)
    hover_log(page)
    wheel_up(page, WHEEL_HEAL)
    at_read = metrics(page)  # the read position the heal must survive at
    scrolled = opened["top"] - at_read["top"]
    page.evaluate("(id) => window.__probe.armResync(id)", sid_trigger)
    page.evaluate(
        "(id) => window.__probe.deliver({ type: 'run_done', session_id: id, run_id: 'probe-run-trigger' })",
        sid_trigger,
    )
    page.wait_for_timeout(SETTLE_MS)
    healed = metrics(page)
    deliver_at = page.evaluate("() => window.__T.meta.deliverAt")
    after_deliver = [w for w in page.evaluate("() => window.__T.writes") if w["t"] >= deliver_at]
    # Intra-swap jumpToBottoms are EXPECTED (the replay's addUserMsg rows pin
    # mid-block); the fix is what the swap ENDS with: a restoreRead write back
    # to the captured top, after every jump.
    restore_writes = [w for w in after_deliver if "restoreRead" in w["stack"]]
    for w in after_deliver[-3:]:
        print(f"  HEAL-WRITE t={w['t'] - deliver_at:+6.0f} {w['from']} -> {w['to']} | {w['stack']}")
    c.check(nm("mech-a-heal-preserves: the run_done heal re-renders IN PLACE "
               "(read position + unpinned + pill survive the swap)"),
            scrolled >= 200 and restore_writes
            and after_deliver and "restoreRead" in after_deliver[-1]["stack"]
            and abs(healed["top"] - at_read["top"]) <= 2
            and healed["top"] <= healed["h"] - healed["ch"] - 200
            and healed["pinned"] is False and healed["pill"] is True,
            f"scrolledUp={scrolled:.0f}px readTop={at_read['top']:.0f} "
            f"topAfter={healed['top']:.0f} max={healed['h'] - healed['ch']} "
            f"restoreWrites={len(restore_writes)} lastStack="
            f"{after_deliver[-1]['stack'][:80] if after_deliver else '(none)'} "
            f"pinned={healed['pinned']} pill={healed['pill']}")

    # -- GUARD: the heal's OTHER half — a FOLLOWER lands at the healed bottom ---
    # preserveRead must not turn the heal into "always preserve": the heal's
    # own purpose is to repair a damaged DOM, and a follower has to end at the
    # bottom of the HEALED transcript (wizard Q3.6), not at the stale partial's
    # bottom it happened to be at. Same production path as the check above, no
    # gesture: read.following is true, so the swap ends in jumpToBottom().
    page.evaluate("() => window.__probe.reset()")
    page.evaluate("(id) => window.__probe.openSession(id)", sid_trigger)
    page.wait_for_function(
        "() => document.querySelector('#chat-log').children.length > 0", timeout=10000)
    page.wait_for_timeout(400)
    page.evaluate("(id) => window.__probe.armResync(id)", sid_trigger)
    page.evaluate(
        "(id) => window.__probe.deliver({ type: 'run_done', session_id: id, run_id: 'probe-run-trigger' })",
        sid_trigger,
    )
    page.wait_for_timeout(SETTLE_MS)
    healed_f = metrics(page)
    hf_max = healed_f["h"] - healed_f["ch"]
    c.check(nm("guard-heal-following: a FOLLOWER lands at the bottom of the healed "
               "transcript (preserveRead is not 'always preserve')"),
            hf_max > 400 and healed_f["top"] >= hf_max - 2
            and healed_f["pinned"] is True and healed_f["pill"] is False,
            f"max={hf_max} top={healed_f['top']:.0f} pinned={healed_f['pinned']} "
            f"pill={healed_f['pill']}")

    # -- candidate (b): the shrink clamp, three close paths ---------------------
    # Each scenario parks the view UNPINNED a few px above the bottom (fine
    # wheel steps, no JS write), lands a strip-collapse tagged "shrink", and
    # measures the 8 ms sampler against the setter spy. If the top moves to
    # the new maxTop with NO writes in the window, that move IS the browser
    # clamping scrollTop on the shrink — candidate (b), with the clamp
    # signature distinguished from a JS scroll call by construction.
    for sname, sname_sid in (("shrink-close", "probe-scroll-shrink-close"),
                             ("shrink-delta", "probe-scroll-shrink-delta"),
                             ("shrink-turn", "probe-scroll-shrink-turn")):
        tel_x, st_x = scenario_shrink(page, sname, sname_sid)
        tail, parked = st_x["tail"], st_x["parked"]
        shrink_at = st_x["shrinkAt"]
        snapped = st_x["snapped"]
        m = shrink_measure(tel_x, shrink_at)
        gap = parked["h"] - parked["ch"] - parked["top"]
        tail_h = tail["tailH"]
        print(f"SHRINK {sname} {engine}: shrinkAt={shrink_at:.0f} tailH={tail_h} "
              f"parkedGap={gap:.1f}px preTop={(m['pre'] or {}).get('top')} "
              f"dH={m['shrinkPx']:+.1f}px dTop={m['topDrop']:+.1f}px "
              f"writes={len(m['writes'])} clampWrites={len(m['clampWrites'])} "
              f"scrolls={len(m['scrolls'])}")
        for s in m["window"]:
            print(f"  t={s['t'] - shrink_at:+6.0f} top={s['top']:7.1f} h={s['h']:6} "
                  f"max={s['h'] - s['ch']:7.1f} pinned={str(s['pinned']):5} pill={s['pill']}")
        for w in m["writes"]:  # a clean clamp has ZERO writers; these name any
            print(f"  WRITE t={w['t'] - shrink_at:+6.0f} {w['from']} -> {w['to']} | {w['stack'][:200]}")
        if m["firstDrop"]:  # writers AFTER the clamp window, up to done: on
            # chromium the clamped-at-bottom view gets CARRIED down by regrowth
            # (browser, write-free) and the spring's first write then lands on
            # the spot the browser already moved to — these lines show both.
            for w in tel_x["writes"]:
                if w["t"] > shrink_at + 250:
                    print(f"  LATE-WRITE t={w['t'] - shrink_at:+6.0f} "
                          f"{w['from']} -> {w['to']} | {w['stack'][:160]}")
        page.screenshot(path=str(WORK / f"{sname}-clamped-{engine}.png"))

        c.check(nm(f"{sname}-parked-unpinned: parked by wheel UNPINNED within the "
                   "pre-D2 strip tail's shrink distance of the bottom"),
                parked["pinned"] is False and parked["pill"] is True
                and gap >= 2 and gap <= PRE_D2_TAIL_PX - 4,
                f"gap={gap:.1f}px tailH={tail_h} stripH={tail.get('stripH')} "
                f"strips={tail['strips']} streaming={tail['streaming']}")
        # D2 (BK-030), replacing the BK-017 "-compensated" row: the strip is
        # one fixed-height row (skeleton while streaming, no live tail), so
        # the close has NOTHING to compensate — the content does not shrink,
        # and the parked gap survives with no write but status.js's own
        # keepRead (shrink-turn: turn_complete also hides #chat-status, the
        # viewport change status-clamp guards). Pre-D2 this row is red: the
        # ~47.5 px collapse (dScrollHeight -47) under a 4 px park. The
        # keepRead content-shrink bracket itself stays guarded by status-*
        # (same restoreRead) and by the follow-shrink accordion clamp below.
        comp_writes = [w for w in m["clampWrites"]
                       if "keepRead" in w["stack"] or "restoreRead" in w["stack"]]
        gap_after = snapped["h"] - snapped["ch"] - snapped["top"]
        c.check(nm(f"{sname}-flat: the segment close does not shrink the content "
                   "(D2: no live tail), the parked reader keeps its gap and nothing "
                   "writes but a named keepRead, pinned stayed false"),
                tail_h is None and m["shrinkPx"] < 1
                and len(comp_writes) == len(m["clampWrites"])
                and gap_after >= gap - 2.5
                and snapped["pinned"] is False and snapped["pill"] is True,
                f"tailH={tail_h} dScrollHeight={-m['shrinkPx']:+.1f}px "
                f"gapParked={gap:.1f}px gapAfter={gap_after:.1f}px "
                f"keepReadWrites={len(comp_writes)} writesInWindow={len(m['clampWrites'])} "
                f"pinned@after={snapped['pinned']} pill@after={snapped['pill']}")
        pre_x, post_x = pre_post(tel_x)
        jump_x = round(post_x["top"] - pre_x["top"], 2)
        dh_x = post_x["h"] - pre_x["h"]
        done_writes_x = writes_in(tel_x, -32, SETTLE_MS)
        done_jumps_x = [w for w in done_writes_x if "jumpToBottom" in w["stack"]]
        c.check(nm(f"{sname}-done-flat: the DONE frame initiates no scroll of its own — "
                   "no jumpToBottom, no shrink (any spring write is a repin's tail, "
                   "attributed by the carry check)"),
                abs(jump_x) <= 2 and not done_jumps_x and dh_x > -2,
                f"dscrollTop={jump_x:+.2f}px dHeight={dh_x:+.0f}px "
                f"jumpToBottom@done={len(done_jumps_x)} writes@done={len(done_writes_x)} "
                f"pinned@pre={pre_x['pinned']} gapAtDone={post_x['h'] - post_x['ch'] - post_x['top']:.0f}px")
        # POST-FIX: overflow-anchor: none on #chat-log (measured: it deletes
        # chromium's write-free bottom-edge CARRY — the pre-fix run showed
        # repin@95ms with ~99 spring writes with it, none without) and the
        # compensated view is never parked AT maxTop anyway, so maybeRepin has
        # nothing to misread on either engine: NO pinned flip after the
        # collapse and no spring/snap/jump writes until done + settle (the
        # compensation's own writes are named and stay in the shrink window).
        # Pre-fix this is red on chromium (the carry cascade) — the WebKit row
        # is the engine cross-check of the README flip matrix.
        drop_t = m["firstDrop"]["t"] if m["firstDrop"] else shrink_at
        post_writes = [w for w in tel_x["writes"] if drop_t < w["t"] <= tel_x["doneAt"] + SETTLE_MS]
        flip = next((s for s in tel_x["samples"]
                     if s["t"] > drop_t and s["pinned"]), None)
        stray = [w for w in post_writes
                 if "keepRead" not in w["stack"] and "restoreRead" not in w["stack"]]
        repin_at = "none" if flip is None else f"{flip['t'] - drop_t:.0f}ms after drop"
        c.check(nm(f"{sname}-carry-repin-attribution: no repin after the compensated "
                   "collapse — nothing was parked AT maxTop for the browser's carry "
                   "to grab (no pinned flip, no spring writes to done)"),
                flip is None and not stray,
                f"repin={repin_at} postDropWrites={len(post_writes)} stray={len(stray)} "
                f"stray0={stray[0]['stack'][:90] if stray else '-'}")

    # -- GUARD: a FOLLOWER across a content-shrink CLAMP -------------------------
    # The risk fix (c) introduces (wizard Q3.4 iii): an unpin rule that reads
    # the CLAMP as the user's upward move would drop a follower at every
    # shrink — the exact opposite of BK-017. `guard-follow-pinned` cannot see
    # it: its log is not yet scrollable, so no clamp bites. Green pre-fix too
    # (no unpin rule existed) — a guard, not a flipped check; it reddens if the
    # direction + attribution rule loosens to "any upward scroll event
    # unpins" (planted 2026-10-29). D2 (BK-030) took away the shrink it used
    # (the ~47 px strip-tail collapse at thinking_done — the follower still
    # rides that close, now flat, and must not unpin across it either), so
    # the clamp is now the one content shrink left under a follower: an
    # accordion CLOSE, ~200 ms of per-frame clamps (each scroll event lands AT
    # maxTop). Idle and by keyboard — a press unpins by rule (BK-029), and an
    # open mid-run pauses (D1): open strip B, ride a real wheel down to the
    # new bottom (downward: stays pinned), close it.
    tel_fsc, st_fsc = scenario_shrink(page, "follow-shrink", "probe-scroll-follow-shrink",
                                      park=False)
    unpinned_at = next((s for s in tel_fsc["samples"]
                        if s["t"] >= st_fsc["shrinkAt"] - 40 and not s["pinned"]), None)
    key_toggle_last(page, ".think-head")  # open (idle: stays pinned, d1-idle-open-keeps)
    page.wait_for_timeout(400)
    wheel_to_bottom(page)
    opened = metrics(page)
    t_close = page.evaluate("() => window.__probe.mark('accClose')")
    key_toggle_last(page, ".think-head")  # close: the animated shrink under a follower
    page.wait_for_timeout(600)
    tel_acc = collect(page)
    win = in_window(tel_acc["samples"], t_close, -8, 550)
    hs = [s["h"] for s in win]
    acc_shrink = (max(hs) - min(hs)) if hs else 0
    acc_steps = len({round(h) for h in hs})  # >2: the shrink really spread over frames
    fin = metrics(page)
    finmax = fin["h"] - fin["ch"]
    c.check(nm("guard-follow-through-shrink: a FOLLOWER never unpins across a "
               "content-shrink clamp (an animated accordion close at the bottom; "
               "no pill, ends at the new bottom) nor across the D2-flat segment close"),
            unpinned_at is None and not any(s["pill"] for s in tel_fsc["samples"])
            and opened["pinned"] is True and opened["h"] - opened["ch"] - opened["top"] <= 1
            and acc_shrink >= 40 and acc_steps > 2
            and all(s["pinned"] and not s["pill"] for s in win)
            and fin["top"] >= finmax - 2,
            f"segClose unpinnedAt={'none' if unpinned_at is None else unpinned_at['t'] - st_fsc['shrinkAt']} "
            f"openedGap={opened['h'] - opened['ch'] - opened['top']:.0f} accShrink={acc_shrink:.0f}px "
            f"heightSteps={acc_steps} unpinnedSamples={sum(not s['pinned'] for s in win)} "
            f"pillSamples={sum(bool(s['pill']) for s in win)} finalTop={fin['top']:.0f} max={finmax:.0f}")

    # -- (b)-viewport: the completion boundary's OTHER height change ----------
    # No thinking strip at all: app.js's turn_complete handler calls
    # statusStop(), hiding the IN-FLOW #chat-status row, so #chat-log grows
    # ~28 px taller and maxTop drops by that much. An unpinned view inside
    # that strip is clamped down with no setter write and NO scrollHeight
    # change — a content-shrink-only compensation (renderMd / endThinking)
    # cannot see it. Parked after the reveal drains, 2.4 s before the frame.
    tel_v, st_v = scenario_shrink(page, "status-clamp", "probe-scroll-status-clamp",
                                  park_tag="lastText", static_first=True)
    shrink_at = st_v["shrinkAt"]
    parked = st_v["parked"]
    mv = max_measure(tel_v, shrink_at, parked)
    gap = parked["h"] - parked["ch"] - parked["top"]
    print(f"SHRINK status-clamp {engine}: shrinkAt={shrink_at:.0f} "
          f"statusH={parked.get('statusH')} parkedGap={gap:.1f}px "
          f"preTop={(mv['pre'] or {}).get('top')} maxDrop={mv['maxDrop']:+.1f}px "
          f"hDrop={mv['hDrop']:+.1f}px topDrop={mv['topDrop']:+.1f}px "
          f"atGap={mv['atGap']} writes={len(mv['writes'])} "
          f"clampWrites={len(mv['clampWrites'])} scrolls={len(mv['scrolls'])}", flush=True)
    for s in mv["window"]:
        print(f"  t={s['t'] - shrink_at:+6.0f} top={s['top']:7.1f} h={s['h']:6} "
              f"max={s['h'] - s['ch']:7.1f} pinned={str(s['pinned']):5} pill={s['pill']}")
    for w in mv["writes"]:
        print(f"  WRITE t={w['t'] - shrink_at:+6.0f} {w['from']} -> {w['to']} | {w['stack'][:200]}")
    page.screenshot(path=str(WORK / f"status-clamp-clamped-{engine}.png"))

    status_h = parked.get("statusH") or 0
    c.check(nm("status-clamp-parked-unpinned: parked by wheel UNPINNED inside the "
               "status row's height of the bottom"),
            parked["pinned"] is False and parked["pill"] is True
            and status_h >= 16 and 2 <= gap <= status_h - 6,
            f"gap={gap:.1f}px statusH={status_h} pinned={parked['pinned']} "
            f"pill={parked['pill']}")
    # POST-FIX flip (README matrix): the status hide now CARRIES the
    # compensation write; the view keeps its ~4 px read position instead of
    # being clamped to the new bottom. The viewport change itself (maxTop
    # drop, scrollHeight flat) still happens — it is the browser's clamp that
    # must no longer eat the position. Pre-fix red three ways: no writes in
    # the window, topDrop 24 vs maxDrop 28 (the clamp landed short of the
    # gap), gap@drop 0 vs parked ~4.
    comp_v = [w for w in mv["clampWrites"]
              if "keepRead" in w["stack"] or "restoreRead" in w["stack"]]
    c.check(nm("status-clamp-compensated: turn_complete hides the in-flow #chat-status "
               "row (log grows TALLER, scrollHeight flat) and the viewport change "
               "carries the keepRead compensation — the top moves WITH maxTop"),
            mv["maxDrop"] >= 12 and mv["first"] is not None
            and mv["atGap"] is not None and -50 <= mv["atGap"] <= 250
            and abs(mv["topDrop"] - mv["maxDrop"]) <= 2.5
            and abs(mv["hDrop"]) <= 2
            and len(comp_v) >= 1 and len(comp_v) == len(mv["clampWrites"]),
            f"maxTopDrop={mv['maxDrop']:+.1f}px scrollHeightDrop={mv['hDrop']:+.1f}px "
            f"dScrollTop={mv['topDrop']:+.1f}px atGap={mv['atGap']}ms "
            f"compWrites={len(comp_v)} writesAtClamp={len(mv['clampWrites'])} "
            f"scrollEvents={len(mv['scrolls'])}")
    vdrop = mv["first"] or {}
    gap_v = (vdrop["h"] - vdrop["ch"] - vdrop["top"]) if vdrop else None
    c.check(nm("status-clamp-read-preserved: the view keeps its parked gap above the "
               "new maxTop (not clamped onto it), still unpinned with the pill"),
            mv["first"] is not None and gap_v is not None
            and abs(gap_v - gap) <= 2.5
            and vdrop.get("pinned") is False and vdrop.get("pill") is True,
            f"gapParked={gap:.1f}px gapAtDrop={gap_v if gap_v is None else f'{gap_v:.1f}'} "
            f"topAtDrop={vdrop.get('top', '?')} "
            f"newMaxTop={vdrop.get('h', 0) - vdrop.get('ch', 0):.0f} "
            f"pinned@drop={vdrop.get('pinned')} pill@drop={vdrop.get('pill')}")
    vpre, vpost = pre_post(tel_v)
    vjump = round(vpost["top"] - vpre["top"], 2)
    v_done_jumps = [w for w in writes_in(tel_v, -32, SETTLE_MS) if "jumpToBottom" in w["stack"]]
    c.check(nm("status-clamp-done-flat: the DONE frame (1.2 s later) moves nothing "
               "and calls no jumpToBottom"),
            abs(vjump) <= 2 and not v_done_jumps,
            f"dscrollTop={vjump:+.2f}px jumpToBottom@done={len(v_done_jumps)} "
            f"gapAtDone={vpost['h'] - vpost['ch'] - vpost['top']:.0f}px")

    # -- terminal frame while scrolled up (BK-017 stop-up, wizard Q1.1) -------
    # The board's complaint with a different last frame: stopped/error land
    # system/error rows whose jumpToBottom is now gated on isFollowing()
    # (addSystemMsg/addErrorMsg). The row must still LAND — gating hides the
    # jump, not the news. addUserMsg (send) stays unconditional by design.
    for sname, sid_s, needle in (("stop-stopped", "probe-scroll-stop-stopped", "Stopped"),
                                 ("stop-error", "probe-scroll-stop-error", "model call failed")):
        tel_su, st_su = scenario_stop_up(page, sname, sid_s)
        pre_su, post_su = pre_post(tel_su)
        jump_su = round(post_su["top"] - pre_su["top"], 2)
        win_su = writes_in(tel_su, -32, SETTLE_MS)
        yank_su = [w for w in win_su
                   if "jumpToBottom" in w["stack"] or "snap" in w["stack"]]
        gap_su = post_su["h"] - post_su["ch"] - post_su["top"]
        landed = page.evaluate(
            "(s) => document.querySelector('#chat-log').textContent.includes(s)", needle)
        telemetry_table(tel_su, f"{sname} {engine}")
        page.screenshot(path=str(WORK / f"{sname}-after-done-{engine}.png"))
        c.check(nm(f"{sname}-wheel-unpinned: scrolled up before the terminal frame "
                   "(unpinned, pill shown)"),
                st_su["wheel"]["pinned"] is False and st_su["wheel"]["pill"] is True
                and (st_su["wheel"]["h"] - st_su["wheel"]["ch"] - st_su["wheel"]["top"]) >= WHEEL_MIN_GAP,
                f"topAtWheel={st_su['wheel']['top']:.0f} pinned@done={pre_su['pinned']}")
        c.check(nm(f"{sname}-terminal-flat: the terminal line lands WITHOUT a jump "
                   "for the up-scrolled reader (flat, no yank write, still unpinned)"),
                abs(jump_su) <= 2 and not yank_su and post_su["pinned"] is False
                and post_su["pill"] is True and gap_su >= 20,
                f"dscrollTop={jump_su:+.2f}px yankWrites={len(yank_su)} gapAtDone={gap_su:.0f}px "
                f"pinned@done={post_su['pinned']} pill@done={post_su['pill']}")
        c.check(nm(f"{sname}-line-landed: gating hides the JUMP, not the line — "
                   f"the terminal row ({needle!r}) is in the transcript"),
                landed, f"needle={needle!r} found={landed}")

    # -- status sub-line toggles (BK-017 status-sub, wizard Q1.2) -------------
    # Three clientHeight toggles mid-run: the slow-hint SHOW and the
    # phase-change HIDE only move the viewport back and forth around the
    # parked view (it must not move, and nothing may write — compensation
    # firing on a non-event is also a bug), and turn_complete's ROW HIDE
    # grows the viewport onto the restored 4 px gap — the clamp bites there,
    # so that window carries the keepRead write and the surviving gap.
    # Pre-fix the row hide clamps silently and the gap dies.
    tel_ss, st_ss = scenario_status_sub(page, "status-sub", "probe-scroll-status-sub")
    parked_ss = st_ss["parked"]
    shown = st_ss["shown"]
    hidden_ev = st_ss["hiddenEv"]
    tag_ss = st_ss["tagAt"]
    gap_ss = parked_ss["h"] - parked_ss["ch"] - parked_ss["top"]
    status_h_ss = parked_ss.get("statusH") or 0

    def samples_around(seq, center, lo, hi):
        return [s for s in seq if center + lo <= s["t"] <= center + hi]

    def last_before(seq, t):
        out = None
        for s in seq:
            if s["t"] <= t:
                out = s
            else:
                break
        return out

    def gap_of(s):
        return s["h"] - s["ch"] - s["top"] if s else None

    # SHOW window: the log's clientHeight must really have shrunk (observer's
    # ch, forced layout). A SHORTER viewport RAISES maxTop — a clamp cannot
    # bite — so the contract is negative: the view does not move, NOTHING
    # writes, and the gap grows by exactly the viewport change. (Pins the
    # toggle's harmlessness; the biting proof is the row hide below.)
    show_w = samples_around(tel_ss["samples"], shown["t"], -8, 400)
    show_writes = in_window(tel_ss["writes"], shown["t"], -8, 400)
    after_show = samples_around(tel_ss["samples"], shown["t"], 200, 400)
    gap_show = gap_of(after_show[-1] if after_show else None)
    show_move = (max(s["top"] for s in show_w) - min(s["top"] for s in show_w)
                 if len(show_w) >= 2 else 0.0)
    print(f"SHRINK status-sub {engine}: shownAt={shown['t']} ch {parked_ss['ch']} -> "
          f"{shown['ch']} gapParked={gap_ss:.1f} gapAfterShow={gap_show} "
          f"showWrites={len(show_writes)} subHideAt={hidden_ev['t']} "
          f"shrinkAt={tag_ss['shrink']}", flush=True)
    c.check(nm("status-sub-parked-unpinned: parked by wheel UNPINNED inside the "
               "status row's height of the bottom"),
            parked_ss["pinned"] is False and parked_ss["pill"] is True
            and status_h_ss >= 16 and 2 <= gap_ss <= status_h_ss - 6,
            f"gap={gap_ss:.1f}px statusH={status_h_ss}")
    c.check(nm("status-sub-show-harmless: the slow-hint sub-line SHOW shortens the "
               "viewport (maxTop RISES) — the parked view does not move, nothing "
               "writes, the gap grows by the viewport change"),
            shown["ch"] <= parked_ss["ch"] - 8
            and show_move <= 2.5 and not show_writes
            and gap_show is not None and abs(gap_show - (gap_ss + (parked_ss["ch"] - shown["ch"]))) <= 2.5
            and all(s["pinned"] is False for s in show_w),
            f"chDrop={parked_ss['ch'] - shown['ch']}px maxMove={show_move:.1f}px "
            f"writes={len(show_writes)} gapParked={gap_ss:.1f}px gapAfter={gap_show} "
            f"subH={shown['h']}px")
    # HIDE window: thinking_done's phase change hides the sub with ZERO
    # content growth — the viewport grows ~16 px back and the widened gap
    # absorbs it: top stays valid, so the correct shape is still unmoved and
    # SILENT (a write here would mean compensation firing on a non-event).
    hide_w = samples_around(tel_ss["samples"], hidden_ev["t"], -8, 400)
    hide_writes = in_window(tel_ss["writes"], hidden_ev["t"], -8, 400)
    hide_move = (max(s["top"] for s in hide_w) - min(s["top"] for s in hide_w)
                 if len(hide_w) >= 2 else 0.0)
    c.check(nm("status-sub-hide-flat: the phase change hides the sub line — the "
               "viewport grows, the widened gap absorbs it, the view stays put "
               "with zero writes (still unpinned)"),
            hide_move <= 2.5 and not hide_writes
            and all(s["pinned"] is False for s in hide_w),
            f"maxMove={hide_move:.1f}px writes={len(hide_writes)} subHidden={hidden_ev['hidden']}")
    # ROW HIDE at turn_complete: sub already hidden, zero growth since — the
    # ~28 px of viewport growth lands on the RESTORED ~4 px gap. The clamp
    # BITES: the compensation write must carry the top down WITH maxTop and
    # the gap must survive. Pre-fix: write-free clamp, gap 0 — red.
    base_ss = last_before(tel_ss["samples"], tag_ss["shrink"] - 20)
    mss = max_measure(tel_ss, tag_ss["shrink"], base_ss)
    drop_ss = mss["first"] or {}
    comp_ss = [w for w in mss["clampWrites"]
               if "keepRead" in w["stack"] or "restoreRead" in w["stack"]]
    gap_mss = gap_of(drop_ss) if drop_ss else None
    gap_base = gap_of(base_ss)
    c.check(nm("status-sub-shrink-compensated: turn_complete hides the whole row "
               "(maxTop drops onto the 4 px gap) and the viewport change carries "
               "the keepRead compensation — the top moves WITH maxTop, the gap "
               "survives, still unpinned"),
            mss["maxDrop"] >= 12 and mss["first"] is not None
            and abs(mss["topDrop"] - mss["maxDrop"]) <= 2.5
            and len(comp_ss) >= 1 and len(comp_ss) == len(mss["clampWrites"])
            and gap_mss is not None and abs(gap_mss - gap_base) <= 2.5
            and drop_ss.get("pinned") is False,
            f"maxTopDrop={mss['maxDrop']:+.1f}px dScrollTop={mss['topDrop']:+.1f}px "
            f"gapBefore={gap_base:.1f}px gapAtDrop={gap_mss} "
            f"compWrites={len(comp_ss)} writesAtClamp={len(mss['clampWrites'])}")
    ss_pre, ss_post = pre_post(tel_ss)
    ss_jumps = [w for w in writes_in(tel_ss, -32, SETTLE_MS) if "jumpToBottom" in w["stack"]]
    c.check(nm("status-sub-done-flat: the DONE frame moves nothing and calls no "
               "jumpToBottom (sub scenario rides out unpinned, pill shown)"),
            abs(ss_post["top"] - ss_pre["top"]) <= 2 and not ss_jumps
            and ss_post["pinned"] is False and ss_post["pill"] is True,
            f"dscrollTop={ss_post['top'] - ss_pre['top']:+.2f}px "
            f"jumpToBottom@done={len(ss_jumps)} gapAtDone={gap_of(ss_post):.0f}px "
            f"pinned={ss_post['pinned']} pill={ss_post['pill']}")

    # -- guard: a FOLLOWED answer (no gesture at all) settles at the bottom -----
    # The long spring lags the bottom while text streams, so the claim is the
    # END state on a log that REALLY overflows: never unpinned, pill never
    # shown, settled at the new bottom.
    tel_f, _ = scenario_stream(page, "follow", "probe-scroll-follow", wheel_dy=None,
                               settle=SETTLE_MS * 3)
    fin_f = metrics(page)
    max_f = fin_f["h"] - fin_f["ch"]
    c.check(nm("guard-follow-pinned: a followed answer settles at the bottom and never unpins"),
            max_f > 1000 and fin_f["top"] >= max_f - 2 and fin_f["pinned"] is True
            and fin_f["pill"] is False
            and all(s["pinned"] for s in tel_f["samples"])
            and not any(s["pill"] for s in tel_f["samples"]),
            f"overflow={max_f:.0f}px top={fin_f['top']:.0f} pinned={fin_f['pinned']} "
            f"pill={fin_f['pill']} samples={len(tel_f['samples'])}")

    # -- control: a reply SHORTER than the log has no scroll to move -----------
    # (The old check claimed this was a "following pinned" guard; with
    # maxTop == 0 "at the bottom" is a tautology, so it now states only the
    # fact it proves: short replies never overflow.)
    tel_s, _ = scenario_stream(page, "short", "probe-scroll-short", wheel_dy=None)
    pre_s, post_s = pre_post(tel_s)
    max_s = post_s["h"] - post_s["ch"]
    c.check(nm("short-reply-fits: the short answer never makes the log scrollable "
               "(the reporter's 'short replies don't snap' case has no scroll to move)"),
            max_s == 0 and post_s["pinned"] is True and post_s["pill"] is False,
            f"maxAtEnd={max_s:.0f} pinned={post_s['pinned']} pill={post_s['pill']}")

    # -- fake-scroller gate pin: ONE isFollowing() gate, BOTH paint paths ------
    # (plan criterion 3). No SSE, no spring from a real stream: 40 seeded rows
    # and the production paint calls, so the writes the spy sees can only come
    # from the gated follow()/flush paths.
    for followed in (True, False):
        gname, g = scenario_gate(page, followed)
        gmax = g["after"]["h"] - g["after"]["ch"]
        moved = abs(g["after"]["top"] - g["base"]["top"])
        if followed:
            track = [w for w in g["writes"] if "frame" in w["stack"] or "snap" in w["stack"]
                     or "jumpToBottom" in w["stack"]]
            c.check(nm("gate-followed-painted-through: the followed view tracks the "
                       "seeded paint through the same gate (spring writes, settles at "
                       "the new bottom, pill never shown)"),
                    g["followingAtPaint"] is True and g["base"]["pinned"] is True
                    and g["after"]["pinned"] is True and g["after"]["pill"] is False
                    and g["after"]["top"] >= gmax - 2 and len(track) >= 1,
                    f"moved={moved:.0f}px gateWrites={len(track)} writes={len(g['writes'])} "
                    f"top={g['after']['top']:.0f} max={gmax:.0f}")
        else:
            stray_g = [w for w in g["writes"] if "keepRead" not in w["stack"]
                       and "restoreRead" not in w["stack"]]
            c.check(nm("gate-unpinned-never-moved: the unfollowed paint moves the view "
                       "ZERO px with ZERO scroll writes across delta ticks AND the "
                       "finalize flush (one gate, both paths), pill stays shown"),
                    g["followingAtPaint"] is False and g["base"]["pinned"] is False
                    and moved <= 2.5 and g["after"]["pinned"] is False
                    and g["after"]["pill"] is True and not stray_g,
                    f"moved={moved:.1f}px writes={len(g['writes'])} "
                    f"stray={len(stray_g)} stray0={stray_g[0]['stack'][:90] if stray_g else '-'}")

    page.evaluate("() => window.__probe.reset()")


if __name__ == "__main__":
    main()
