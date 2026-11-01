"""The BK-017 scroll-follow probe's site build (tests/fixtures/scroll_follow/run_probe.py).

Only the entry-tag matching is pinned here — the part that silently decides
whether the probe can run at all (mirror of tests/test_pwa_resume_probe.py):

- it must TOLERATE the `?v=` bump the webui convention makes on every asset
  edit — the scroll probe exists to measure webui JS that Step 2 will edit;
- it must still FAIL LOUDLY when the app.js entry tag's shape really changes
  (renamed entry, src dropped) — the probe must never silently serve a page
  without the app under test.

The chrome launch itself is not exercised here (run the probe directly for
that, see tests/fixtures/scroll_follow/README.md); importing the module must
stay side-effect-free.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RUN_PROBE = REPO / "tests" / "fixtures" / "scroll_follow" / "run_probe.py"


def _run_probe():
    spec = importlib.util.spec_from_file_location("scroll_follow_run_probe", RUN_PROBE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entry_tag_matches_a_bumped_version_and_bare_src():
    tag = _run_probe().APP_ENTRY_RE
    assert tag.search('<script type="module" src="/app.js?v=47"></script>')
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
    assert tag.search('<script type="module" src="/main.js?v=47"></script>') is None
    assert tag.search('<script src="/mock_fetch.js"></script>') is None
    assert tag.search('<script type="module" src="/probe_scroll.js"></script>') is None


# ── BK-017 Step-3 pins: the matrix's plumbing ────────────────────────────────

import re

MOCK_FETCH = REPO / "tests" / "fixtures" / "scroll_follow" / "mock_fetch.js"


def _scenario_keys():
    """The scenario names in mock_fetch.js's `scenarios:` table. Keys appear
    bare (`trigger: function`) and quoted (`"shape-code": function`) — both
    spellings are real today; the extraction must catch both or the pin
    below passes on an empty set (hence the negative control)."""
    src = MOCK_FETCH.read_text()
    found = re.findall(r'(?m)^\s{4}(?:"([a-z0-9-]+)"|([a-z][a-z0-9-]*))\s*:\s*function\s*\(\s*\)',
                       src)
    return {a or b for a, b in found}


def test_matrix_shape_scripts_exist_in_the_mock():
    """Every (shape -> script) the matrix drives must be a real scenario.
    A renamed script otherwise fails as a mid-run page error, after minutes
    of browser time — this pins it in milliseconds."""
    mod = _run_probe()
    keys = _scenario_keys()
    assert {"trigger", "control", "gap0"} <= keys  # bare-key extraction works
    for shape, script in mod.MATRIX_SHAPES:
        assert script in keys, f"matrix shape {shape!r} names script {script!r}: no such scenario"


def test_scenario_keys_negative_control():
    """The extractor must reject a name the mock does not have — if the
    pattern ever matches everything (or the file moves), the superset pin
    above would silently pass."""
    keys = _scenario_keys()
    assert keys, "scenario extraction found nothing — fix the extractor, not the mock"
    assert "trigger-nonsense" not in keys
    assert "scenarios" not in keys


def test_ports_are_the_two_shipped_targets():
    """desktop + iPhone-sized, and the iPhone port is the touch one. The
    anchor verdict and the README both state results per port; a third port
    or a renamed one means the README and the plan's criterion-2 table move
    with it — fail here first."""
    mod = _run_probe()
    ports = {p["name"]: p for p in mod.PORTS}
    assert set(ports) == {"desktop", "iphone"}
    assert ports["desktop"]["has_touch"] is False
    assert ports["iphone"]["has_touch"] is True
    assert ports["iphone"]["viewport"] == {"width": 390, "height": 844}


def test_chat_log_ships_overflow_anchor_none():
    """The anchor verdict (KEEP) rests on this rule; the probe's anchor-*
    family measures it live, but a flip to `auto` is a one-word CSS change
    that this pin catches without any browser."""
    css = (REPO / "src" / "harness" / "gateway" / "webui" / "chat.css").read_text()
    block = re.search(r"#chat-log\s*\{[^}]*\}", css)
    assert block is not None, "#chat-log rule block not found in chat.css"
    assert "overflow-anchor: none" in block.group(0), (
        "#chat-log lost overflow-anchor: none — WebKit anchors too (+120 px measured; "
        "never 'chromium-only'), and at gap 0 both engines carry a stray-tap reader "
        "down into maybeRepin's window (probe anchor-auto-gap0-carries)"
    )


def test_mock_chat_appends_to_existing_sessions():
    """The mock must APPEND turns to a session's stored rows, never replace
    them — a replacing mock hides the history from openSession(), and the
    mixed/short state-(a) scenarios lose their scroll room (measured: log
    stuck at sh==ch, gesture measured 0 px, 2026-10-29)."""
    src = MOCK_FETCH.read_text()
    assert "prior.concat" in src, (
        "_startRun replaced the session rows again — openSession over a live "
        "session must keep its transcript, like the real backend")


def test_mdpace_reveal_switch_is_probe_only():
    """Step 3's one src line is a READ of `window.__mdPace` in transcript.js's
    reveal tick; the probe sets it (`probe_scroll.arm`), production must never.
    A production assignment — or a second read in another module — would leave
    streamed answers blank until `done` if it ever fired, and the README's
    "production never sets it" would stop being checkable."""
    webui = REPO / "src" / "harness" / "gateway" / "webui"
    readers, setters = [], []
    for f in sorted(webui.glob("*.js")):
        text = f.read_text()
        if "__mdPace" not in text:
            continue
        readers.append(f.name)
        # assignment, not the `=== "off"` comparison
        if re.search(r"__mdPace\s*=(?!=)", text):
            setters.append(f.name)
    assert readers == ["transcript.js"], f"__mdPace read outside transcript.js: {readers}"
    assert setters == [], f"production code SETS the probe reveal switch: {setters}"
    assert "__mdPace" in (REPO / "tests" / "fixtures" / "scroll_follow"
                          / "probe_scroll.js").read_text(), (
        "the probe lost the reveal switch it arms — transcript.js's branch is dead code")


# ── BK-019 Step-5b pins: the recover family and the rollup guards ─────────────

def test_recover_family_scripts_exist_in_the_mock():
    """Every recover scenario must be a real mock script (same class of break
    the matrix-shape pin guards: a renamed script is minutes of browser time
    otherwise). 'recover' being in FAMILIES is what routes --only=recover at
    all — a dropped family silently runs nothing."""
    mod = _run_probe()
    keys = _scenario_keys()
    assert "recover" in mod.FAMILIES
    assert mod.RECOVER_SCRIPTS, "recover family lost its scripts table"
    for script in mod.RECOVER_SCRIPTS:
        assert script in keys, f"recover script {script!r}: no such mock scenario"


def test_floor_is_a_real_figure_not_a_placeholder_zero():
    """MIN_UNIQUE_PER_ENGINE may only RISE (pwa_resume's discipline): the pin
    refuses a silent lowering below the measured full-run figure (125). The
    browser run enforces it exactly; a deliberate family change edits both the
    figure and this pin in the same change."""
    floor = _run_probe().MIN_UNIQUE_PER_ENGINE
    assert isinstance(floor, int) and not isinstance(floor, bool)
    assert floor >= 125, ("the per-engine floor fell below the measured full-run "
                          "figure (125, two engines, all five families) — it may "
                          "only RISE with the check count")


def test_empty_selection_problem_refuses_zero_rows():
    """A run where every selected engine was launch-skipped (no WebKit install,
    a browser that would not start) has SKIP lines in c.lines, so main's
    'no checks ran' usage error does not fire — the empty-rows guard is the
    only thing between 'nothing ran' and exit 0."""
    mod = _run_probe()
    assert mod.empty_selection_problem({"chromium": {"[*] a"}}) is None
    p = mod.empty_selection_problem({})
    assert p is not None and "no engine" in p


def test_engine_assertion_rows_folds_prefixes_and_dedupes():
    mod = _run_probe()
    rows = mod.engine_assertion_rows([
        "PASS [chromium] a-one | detail",
        "PASS [chromium] a-one | detail",       # the double print must fold
        "FAIL [chromium] a-two | boom",         # a FAIL is still a row
        "SKIP [chromium] a-three | not today",  # a SKIP is NOT a row
        "PASS [chromium/desktop] a-four | x",   # the /port scheme…
        "PASS [webkit] a-one",                  # …folds to compare across engines
    ])
    assert rows["chromium"] == {"[*] a-one", "[*] a-two", "[*/desktop] a-four"}
    assert rows["webkit"] == {"[*] a-one"}


def test_engine_set_problem_flags_one_sided_sets():
    """An engine that silently skipped a family must exit 2 even if its row
    COUNT happens to match — identity, not totals."""
    mod = _run_probe()
    assert mod.engine_set_problem({"chromium": {"[*] a"}, "webkit": {"[*] a"}}) is None
    assert mod.engine_set_problem({"chromium": {"[*] a"}}) is None  # single engine
    p = mod.engine_set_problem({"chromium": {"[*] a", "[*] b"}, "webkit": {"[*] a"}})
    assert p is not None and "[*] b" in p


def test_silent_engine_problem_fires_on_zero_rows_any_scope():
    """Planted 2026-10-30: webkit returned from run_recover early — selected,
    launched, ZERO rows — and the scoped run exited 0 (no rows entry, so
    set-identity saw one engine; coverage only speaks on full runs). The fix:
    zero rows without a launch SKIP exits 2 on EVERY scope; the SKIP exemption
    stays (a missing WebKit install is the one legitimate absence)."""
    mod = _run_probe()
    assert mod.silent_engine_problem(["chromium"], [], {"chromium": {"[*] a"}}) is None
    p = mod.silent_engine_problem(["chromium", "webkit"], [], {"chromium": {"[*] a"}})
    assert p is not None and "webkit" in p
    skips = ["SKIP engine-webkit | launch failed: not installed"]
    assert mod.silent_engine_problem(["chromium", "webkit"], skips,
                                     {"chromium": {"[*] a"}}) is None


def test_coverage_problem_is_the_floor_with_skip_exemption():
    """A zero-contribution engine must never exit 0; a launch-SKIPPED engine
    (missing WebKit install) is the one legitimate absence."""
    mod = _run_probe()
    floor = mod.MIN_UNIQUE_PER_ENGINE
    full = {f"[*] r{i}" for i in range(floor)}
    assert mod.coverage_problem(["chromium"], [], {"chromium": full}) is None
    p = mod.coverage_problem(["chromium"], [], {"chromium": set()})
    assert p is not None and "chromium" in p
    skips = ["SKIP engine-webkit | launch failed: not installed"]
    assert mod.coverage_problem(["chromium", "webkit"], skips,
                                {"chromium": full, "webkit": set()}) is None
