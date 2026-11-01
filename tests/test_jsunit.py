"""The offline JS unit tier (tests/fixtures/jsunit/) — BK-020's first two
acceptance criteria, stood up by plan bk-019-run-lifecycle Step 2.

This test IS the tier's entry point: it builds the scratch site, jsc
parse-checks the suite's JS, drives the cached chrome-headless-shell, and
fails with the FAIL lines in the message on any red check (~3 s wall). For
the raw run standalone: `python3 tests/fixtures/jsunit/run_jsunit.py`.

The tier proves it can go red by the plant-and-remove the Step-2 worked-when
names (evidence in the run report, never a commit): flip an expected value in
tests/fixtures/jsunit/replay.test.js, rerun this file, expect a FAIL line
naming the rule; revert. runner.js additionally carries a permanent positive
control (xtest) so check()/eq() themselves cannot rot silently.

Importing run_jsunit.py must stay side-effect-free (the pwa_resume wrapper's
rule) — this file imports it before asserting anything.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "jsunit"
RUN_JSUNIT = FIXTURES / "run_jsunit.py"


def _run_jsunit():
    spec = importlib.util.spec_from_file_location("jsunit_run_jsunit", RUN_JSUNIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fixture_files_present_and_page_names_the_entry():
    """A renamed entry silently serves a page that tests nothing — fail the
    suite, not mid-review (the pwa_resume entry-tag rule)."""
    for name in ("suite.html", "suite.js", "runner.js", "replay.test.js",
                 "runstate.test.js", "observe.test.js", "stream_md.test.js",
                 "senderstop.test.js", "labels.test.js", "jsc_check.js"):
        assert (FIXTURES / name).is_file(), f"missing tests/fixtures/jsunit/{name}"
    page = (FIXTURES / "suite.html").read_text()
    assert 'src="/suite.js"' in page, "runner page no longer loads the suite entry"
    entry = (FIXTURES / "suite.js").read_text()
    for module in ("replay", "runstate", "observe", "stream_md", "senderstop", "labels"):
        assert f'import "./{module}.test.js"' in entry, (
            f"suite.js no longer imports the {module} suite — the tier would run it empty"
        )


def test_jsunit_tier_green(capsys):
    mod = _run_jsunit()
    rc = mod.main()
    out = capsys.readouterr().out
    assert rc == 0, f"JS unit tier is red (exit {rc}) — output:\n{out}"
    assert "JSUNIT-RESULT: PASS" in out
    n_pass = sum(1 for line in out.splitlines() if line.startswith("PASS"))
    # Shrink-guard: deleting tests is silently how a tier stops guarding.
    # 259 = the Step-2 baseline of 81 (78 replay rules + tier contract + the
    # eq half of the anti-vacuity control) + Step 3's 170 runstate-transition
    # checks (T01–T22, T27, phase derivation, the D4/D5/D6 pins, the reset
    # pair) + the Step-3 review's 4 (latched tool_call still polls approvals;
    # the module-source effect-token scan, two checks) + Step 4's T09
    # hand-back parity pin (net +1) + the Step-4 review's 3 (the hand-back is
    # withheld while a resync is armed — the old stream-end tail's !resync
    # conjunct, D4/T22) + BK-023 Step 2's 39 (the trace formatter's collapse/
    # stamp/verbatim/frames rules, incl. the review's cross-push run-break
    # check) + BK-027's 31 (the background-run resume marks T28/T29: start,
    # resume subject, run_done heal, off-screen, several runs, no double
    # render, beside another chat's resync, offline) + BK-034's 80
    # (observe.test.js: the third Stop source — arm from the live join and a
    # rerouted event, never for own runs, the switch, every terminal, the
    # own-stream hand-backs withheld, the observe watch, stop racing done,
    # hidden mid-run) + BK-028's 98 (stream_md.test.js: the real marked +
    # DOMPurify loaded, settle()'s hold-back/repair table, the per-frame
    # morph == fresh-render invariant, no re-type/raw marker/dropped node
    # between frames, selection + <pre> identity, the 3g golden) + BK-029's 27
    # (runstate screenRunLive, D1's "streaming": each source on screen, every
    # subset elsewhere, latched, pre-session, writes nothing) + BK-036's 34
    # (senderstop.test.js: a started stream's silent end is T15's — Stop kept,
    # /live decides; a never-started one hands back; T33's live-verdict
    # backstop and where it must NOT arm; T10/T11 scoped to their session). This may only RISE: a
    # deliberate merge/replacement edits the
    # figure in the same change. The floor is ALSO the only thing pinning a test
    # module's membership in suite.js — the page-presence test names replay/
    # runstate, not trace — so a new *.test.js that does not raise it leaves
    # every one of its checks deletable without a red.
    assert n_pass >= 569, f"tier shrank to {n_pass} PASS lines — coverage lost?"
