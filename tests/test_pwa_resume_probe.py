"""The PWA-resume probe's site build (tests/fixtures/pwa_resume/run_probe.py).

Only the entry-tag matching is pinned here — the part that silently decides
whether the probe can run at all:

- it must TOLERATE the `?v=` bump the webui convention makes on every asset
  edit; a pinned `<script src="/app.js?v=45">` made the probe exit 2 on
  exactly the runs it exists to measure (Step 2/3 both edit webui JS);
- it must still FAIL LOUDLY when the app.js entry tag's shape really changes
  (renamed entry, src dropped) — the probe must never silently serve a page
  without the app.

The chrome launch itself is not exercised here (run the probe directly for
that); importing the module must stay side-effect-free.

BK-019 Step 5 added the WebKit engine mode; the pins below cover the part that
decides whether the "both engines green" acceptance line means anything — the
engine flag/default, the unique-assertion floor (a results block that printed
PASS with ZERO rows must never exit 0), and the cross-engine assertion-set
identity check.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_PROBE = REPO / "tests" / "fixtures" / "pwa_resume" / "run_probe.py"


def _run_probe():
    spec = importlib.util.spec_from_file_location("pwa_resume_run_probe", RUN_PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entry_tag_matches_a_bumped_version_and_bare_src():
    tag = _run_probe().APP_ENTRY_RE
    assert tag.search('<script type="module" src="/app.js?v=46"></script>')
    assert tag.search('<script type="module" src="/app.js?v=1" async></script>')
    assert tag.search('<script type="module" src="/app.js"></script>')


def test_entry_tag_matches_the_real_index_html():
    """Pins the tag the probe swaps out today — a rename here must fail in the
    suite, not as an exit-2 'harness error' mid-review."""
    index = (REPO / "src/harness/gateway/webui/index.html").read_text()
    match = _run_probe().APP_ENTRY_RE.search(index)
    assert match is not None, "app.js entry tag moved or renamed — update run_probe.py"
    assert "/app.js" in match.group(0)


def test_entry_tag_rejects_the_probes_own_scripts():
    """Negative control: the pattern matches the APP's entry tag only — hence
    the loud exit 2 when the real tag's shape changes, instead of a probe page
    that never loads the app under test."""
    tag = _run_probe().APP_ENTRY_RE
    assert tag.search('<script type="module" src="/main.js?v=45"></script>') is None
    assert tag.search('<script src="/mock_fetch.js"></script>') is None
    assert tag.search('<script type="module" src="/probe.js"></script>') is None


def test_engine_flag_and_default_are_pinned():
    """The Step-5 matrix is "chromium AND WebKit"; the flag that selects it must
    parse exactly, reject typos with exit 2, and never silently widen/narrow the
    engine list. `default` is a parameter so this pin is environment-free."""
    m = _run_probe()
    assert m.parse_engines([], default=["chromium"]) == ["chromium"]
    assert m.parse_engines([], default=["chromium", "webkit"]) == [
        "chromium", "webkit",
    ]
    assert m.parse_engines(["--engines=chromium,webkit"]) == ["chromium", "webkit"]
    assert m.parse_engines(["--engines=webkit,webkit,chromium"]) == [
        "webkit", "chromium",
    ]
    for bad in (["--engines=firefox"], ["--engines="], ["--bogus"], ["webkit"]):
        with pytest.raises(SystemExit) as exc:
            m.parse_engines(bad)
        assert exc.value.code == 2


def test_coverage_floor_rejects_a_zero_assertion_green():
    """The vacuous-green guard (planted during review): probe.js's finally
    prints `PROBE-RESULT: PASS` even when main() returns early and emits no
    assertion rows. A results block is evidence only if the scenario rows are
    there — unique names, so a doubled print cannot satisfy the floor."""
    m = _run_probe()
    full = "\n".join(f"PASS check {i}" for i in range(m.MIN_UNIQUE_ASSERTIONS))
    full += "\nPROBE-RESULT: PASS (0 failing assertions)"
    assert m.coverage_problem("chromium", full) is None

    zero = "PROBE-RESULT: PASS (0 failing assertions)"
    problem = m.coverage_problem("webkit", zero)
    assert problem is not None and "floor" in problem

    short = "\n".join(f"PASS check {i}" for i in range(m.MIN_UNIQUE_ASSERTIONS - 1))
    assert m.coverage_problem("webkit", short) is not None

    # 107 duplicated rows are 1 unique assertion — the double-print gotcha.
    dup = "\n".join("PASS one check" for _ in range(m.MIN_UNIQUE_ASSERTIONS))
    assert m.coverage_problem("webkit", dup) is not None


def test_cross_engine_assertion_sets_must_match():
    """A family skipped on one engine can leave the totals equal; only the SET
    difference catches it. Single-engine runs are exempt (nothing to compare)."""
    m = _run_probe()
    a = "\n".join(["PASS alpha", "PASS beta | detail", "FAIL gamma"])
    b = a
    assert m.engine_set_problem([("chromium", a), ("webkit", b)]) is None
    assert m.engine_set_problem([("chromium", a)]) is None
    trimmed = "\n".join(["PASS alpha", "PASS beta | detail"])
    problem = m.engine_set_problem([("chromium", a), ("webkit", trimmed)])
    assert problem is not None and "gamma" in problem
