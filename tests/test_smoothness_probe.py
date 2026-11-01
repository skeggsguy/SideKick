"""The smoothness probe's plumbing and hard targets (tests/fixtures/smoothness/).

The browser suites are the manual tier (run_probe.py under
/opt/homebrew/bin/python3, ~25 min — see the fixture README). What is pinned
here is what silently decides whether a run means anything, offline:

- the runner's flags parse exactly and reject typos with exit 2;
- the rollup never reads green on a run that measured nothing: a timeout, a
  crashed suite, a missing PROBE-RESULT line, zero rows, or a both-engine run
  under a suite's MIN_ROWS floor is exit 2;
- each zero/structural target in metrics.py goes RED on the value it guards
  (one retype, one raw-marker frame, a 48 px collapse, a lost selection, a
  default tap highlight, a reset scrollLeft, churn over the goal line), and a
  vacuous sample (too few frames, no strip ever seen closing) is not a pass;
- every scenario a suite arms exists in instrument.js.

Importing run_probe.py / metrics.py / common.py must stay side-effect-free
and Playwright-free (the uv venv deliberately has no Playwright).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "tests" / "fixtures" / "smoothness"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"smoothness_{name}", FIX / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rp():
    return _load("run_probe")


@pytest.fixture(scope="module")
def mx():
    return _load("metrics")


# ── runner flags ─────────────────────────────────────────────────────────────

def test_defaults_run_every_suite_on_both_engines(rp):
    o = rp.parse_args([])
    assert o["only"] == list(rp.SUITES) and o["engines"] == ["webkit", "chromium"]
    assert rp.selected(o) == list(rp.SUITES)


def test_only_and_engines_narrow_and_keep_suite_order(rp):
    o = rp.parse_args(["--only=orphan,stream", "--engines=chromium"])
    assert o["only"] == ["stream", "orphan"] and o["engines"] == ["chromium"]
    # pills and bgrun are webkit-only: a chromium-only run drops them, never runs them empty
    assert rp.selected(rp.parse_args(["--only=pills,bgrun,taps", "--engines=chromium"])) == ["taps"]


@pytest.mark.parametrize("bad", [["--only=strem"], ["--only="], ["--engines=firefox"], ["--bogus=1"],
                                 ["stream"], ["--only=stream", "--red=slack"],
                                 ["--only=toggles", "--red=bogus"], ["--webui=/nonexistent/dir"]])
def test_bad_flags_exit_2(rp, bad):
    with pytest.raises(SystemExit) as exc:
        rp.parse_args(bad)
    assert exc.value.code == 2


def test_suite_argv_passes_only_what_each_suite_takes(rp):
    o = rp.parse_args(["--only=toggles,pills", "--red=keepat",
                       f"--webui={REPO / 'src/harness/gateway/webui'}"])
    tog, pil = rp.suite_argv("toggles", o), rp.suite_argv("pills", o)
    assert "--red=keepat" in tog and not any(a.startswith("--red") for a in pil)
    assert "--engines=webkit,chromium" in tog and "--engines=webkit" in pil  # pills is webkit-only
    assert any(a.startswith("--webui=") for a in tog) and any(a.startswith("--webui=") for a in pil)


def test_every_suite_has_a_script_and_a_floor(rp):
    assert set(rp.MIN_ROWS) == set(rp.SUITES)
    for name, (script, engines, timeout) in rp.SUITES.items():
        assert (FIX / script).is_file(), script
        assert set(engines) <= {"webkit", "chromium"} and timeout > 0
        assert rp.MIN_ROWS[name] > 0


# ── rollup: a run that measured nothing is never green ──────────────────────

def test_parse_log_counts_unique_rows_and_fail_is_sticky(rp):
    text = "\n".join([
        "PASS [webkit] a | {}", "PASS [webkit] a | {}",            # doubled print: one row
        "FAIL [webkit] b | {\"x\": 1}", "PASS [webkit] b | {}",     # a FAIL is never overwritten
        "PASS [webkit] d", "FAIL [webkit] d",                       # ...and a later FAIL wins
        "PASS [webkit] c", "MEASURE k = 1", "noise PASS x",
        "PROBE-RESULT: FAIL (1 failing)",
    ])
    p = rp.parse_log(text)
    assert p["rows"] == {"[webkit] a": "PASS", "[webkit] b": "FAIL", "[webkit] d": "FAIL",
                         "[webkit] c": "PASS"}
    assert p["measures"] == ["k = 1"] and p["result"] == "FAIL"


def test_structural_problems(rp):
    floor = rp.MIN_ROWS["bgrun"]
    full = rp.parse_log("\n".join(f"PASS r{i}" for i in range(floor)) + "\nPROBE-RESULT: PASS (0 failing)")
    assert rp.structural_problem("bgrun", 0, full, True) is None
    assert "timed out" in rp.structural_problem("bgrun", None, full, True)
    assert "exited 2" in rp.structural_problem("bgrun", 2, full, True)
    no_result = rp.parse_log("PASS r0")
    assert "PROBE-RESULT" in rp.structural_problem("bgrun", 0, no_result, False)
    zero = rp.parse_log("PROBE-RESULT: PASS (0 failing)")
    assert "ZERO rows" in rp.structural_problem("bgrun", 0, zero, False)
    short = rp.parse_log("\n".join(f"PASS r{i}" for i in range(floor - 1)) + "\nPROBE-RESULT: PASS (0 failing)")
    assert "floor" in rp.structural_problem("bgrun", 0, short, True)
    # the floor speaks on both-engine runs only: an --engines=webkit run legitimately has fewer rows
    assert rp.structural_problem("bgrun", 0, short, False) is None


# ── hard targets go red ─────────────────────────────────────────────────────

GREEN_STREAM = {"frames": 996, "bubble_frames": 876, "block_retypes": 0, "retype_kinds": [],
                "raw_marker_frames": 0, "think_collapse_px": [0]}


def _failed(rows):
    return {n for n, ok, _ in rows if not ok}


def test_stream_checks_green_then_each_target_red(mx):
    assert _failed(mx.stream_checks(GREEN_STREAM)) == set()
    assert _failed(mx.stream_checks({**GREEN_STREAM, "block_retypes": 1})) == {"block-retypes-0 (BK-028)"}
    assert _failed(mx.stream_checks({**GREEN_STREAM, "raw_marker_frames": 1})) == {"raw-marker-frames-0 (BK-028)"}
    assert _failed(mx.stream_checks({**GREEN_STREAM, "think_collapse_px": [48]})) == {"think-collapse-0px (BK-030)"}
    # vacuous: the strip was never seen closing, or the sampler saw no stream
    assert "think-collapse-0px (BK-030)" in _failed(mx.stream_checks({**GREEN_STREAM, "think_collapse_px": []}))
    assert {"sampler-watched-the-stream", "block-retypes-0 (BK-028)"} <= _failed(
        mx.stream_checks({**GREEN_STREAM, "frames": 10}))


def test_selection_highlight_scroll_left_and_churn_go_red(mx):
    assert mx.selection_check({"before": 12, "after": 12, "anchorConnected": True})[1]
    assert not mx.selection_check({"before": 12, "after": 0, "anchorConnected": False})[1]
    assert not mx.selection_check({"before": 0, "after": 0, "anchorConnected": True})[1]  # nothing selected

    t = "rgba(0, 0, 0, 0)"
    named = [f".think-head: {t}", f".pill: {t}"]
    sweep = {"checked": 7, "kinds": [], "default_highlight": []}
    assert mx.highlight_check(named, sweep)[1]
    assert not mx.highlight_check(named, {**sweep, "default_highlight": ["a: rgba(0, 0, 0, 0.4)"]})[1]
    assert not mx.highlight_check([".think-head: n/a", f".pill: {t}"], sweep)[1]
    assert not mx.highlight_check(named, {**sweep, "checked": 0})[1]

    h = {"set": 40, "mid_after250ms": 40, "mid_sameNode": True, "post_complete": 40, "post_sameNode": True}
    assert mx.scroll_left_check(h)[1]
    assert not mx.scroll_left_check({**h, "mid_after250ms": 0})[1]
    assert not mx.scroll_left_check({**h, "post_complete": 0})[1]

    assert mx.long_churn_check({"frames": 2000, "nodes_removed": mx.LONG_CHURN_CEILING})[1]
    assert not mx.long_churn_check({"frames": 2000, "nodes_removed": 62_322})[1]
    assert not mx.long_churn_check({"frames": 0, "nodes_removed": 0})[1]  # vacuous


def test_timing_numbers_are_reported_not_asserted(mx):
    """The timing rule: a slow host (p95 60 ms, frames over 50 ms) fails no row."""
    slow = {**GREEN_STREAM, "frame_p95": 60, "frames_over_50ms": 40, "frame_max": 400}
    assert _failed(mx.stream_checks(slow)) == set()


# ── the scenarios the suites arm exist ──────────────────────────────────────

def test_every_armed_scenario_is_defined():
    instrument = (FIX / "instrument.js").read_text()
    defined = set(re.findall(r"M\.scenarios\.([a-z0-9]+)\s*=", instrument))
    armed = set()
    for py in FIX.glob("*.py"):
        src = py.read_text()
        armed |= set(re.findall(r'send\(page, "([a-z0-9]+)"\)', src))
        armed |= set(re.findall(r'mid_tool\(page(?:, "([a-z0-9]+)")?\)', src)) - {""}
        armed |= set(re.findall(r'\["(toggler|longbody|thinker)", "sid-', src))
    assert armed >= {"smooth", "tapper", "long", "tapper2", "thinker", "toggler", "longbody"}, armed
    assert armed <= defined, armed - defined


def test_common_loads_scroll_follow_entry_swap_without_playwright():
    common = _load("common")
    sf = common.scroll_follow_runner()
    assert sf.APP_ENTRY_RE.search((REPO / "src/harness/gateway/webui/index.html").read_text())
    for f in ("mock_fetch.js", "probe_scroll.js"):
        assert f in sf.PROBE_ENTRY and (common.SCROLL_FOLLOW / f).is_file()
    assert common.WORK.relative_to(REPO).parts[0] == "scratch"  # gitignored


# ── main(): the exit code the rollup really returns ─────────────────────────

def _run_main(rp, monkeypatch, capsys, rc, log):
    import sys
    import types
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setattr(sys, "argv", ["run_probe.py", "--only=bgrun", "--engines=webkit"])
    monkeypatch.setattr(rp, "run_suite", lambda name, opts: (rc, log, 0.1))
    with pytest.raises(SystemExit) as exc:
        rp.main()
    capsys.readouterr()
    return exc.value.code


ROWS_OK = "PASS [a] r1\nPASS [a] r2\n"


@pytest.mark.parametrize("rc,log,want", [
    (0, ROWS_OK + "PROBE-RESULT: PASS (0 failing)", 0),
    (1, "FAIL [a] r1\nPASS [a] r2\nPROBE-RESULT: FAIL (1 failing)", 1),
    (0, "FAIL [a] r1\nPROBE-RESULT: PASS (0 failing)", 1),                 # a FAIL row beats a green exit
    (0, ROWS_OK + "PROBE-RESULT: FAIL (1 failing)", 1),                    # a FAIL verdict with no FAIL row
    (1, ROWS_OK + "PROBE-RESULT: FAIL (1 failing)", 1),
    (None, ROWS_OK + "PROBE-RESULT: PASS (0 failing)", 2),                 # timeout
    (3, ROWS_OK + "PROBE-RESULT: PASS (0 failing)", 2),                    # crash
    (0, ROWS_OK, 2),                                                       # never reached its end
    (0, "PROBE-RESULT: PASS (0 failing)", 2),                              # zero rows
    (1, "FAIL [a] r1\nPROBE-RESULT: FAIL (1 failing)", 1),
])
def test_main_exit_codes(rp, monkeypatch, capsys, rc, log, want):
    assert _run_main(rp, monkeypatch, capsys, rc, log) == want
