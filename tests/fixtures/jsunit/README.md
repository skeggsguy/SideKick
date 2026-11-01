# jsunit — the offline JS unit tier (minimal BK-020)

Stood up by plan `bk-019-run-lifecycle` Step 2 because BK-019's
transition-table criterion needs a place to unit-test DOM-free modules.

**What this tier is:** the REAL webui ES modules, imported unmodified by test
modules, driven under the cached `chrome-headless-shell` (no node on this
Mac — recipe from `tests/fixtures/pwa_resume/run_probe.py`: symlink mirror →
`http.server` → `--dump-dom` → parse the `JSUNIT-RESULTS` block).

**What may be tested here:** DOM-free modules only — `replay.js` and
`runstate.js` (design §2.6 keeps runstate renderer-free precisely so).
`transcript.js`/`status.js` touch the page at import time → they stay with
the probe fixtures. A module that only builds DETACHED elements and touches
nothing at import (`stream_md.js`, BK-028) is fine. **rAF never fires under `--virtual-time-budget`
(gotchas) — anything rAF-paced belongs to the probe tier, never here**, and
that admission rule is ENFORCED: `suite.html` replaces `requestAnimationFrame`
with a thrower and the suite (plus its page-error handler) turns any rAF use
into a loud red, never a silent no-op PASS.

## Run

```sh
uv run pytest tests/test_jsunit.py -q          # the tier's entry point
python3 tests/fixtures/jsunit/run_jsunit.py    # standalone (stdlib only)
```

Exit codes (pwa_resume precedent): `0` all green · `1` a check FAILED (red
tier) · `2` harness broke (jsc parse, no cached chrome, suite never
reported).

## Layout

- `suite.html` — the bare runner page (a `<pre>` + a page-error handler).
- `suite.js` — entry module: imports every `*.test.js`, calls `runAll()` once.
- `runner.js` — micro-runner: `test`/`xtest`/`check`/`eq`, per-test reset of
  `ui` (fixtures arming `ui` directly IS the project's test seam).
- `replay.test.js` — replay.js's dedupe rules as they exist pre-BK-019:
  per-run watermark, painted-interval skipping, the gap suffix, the
  run-identity cold-join triggers, `markRunDone` terminal stamping. Each
  check's name states the rule it pins.
- `runstate.test.js` — the run-lifecycle controller (BK-019 Step 3): one or
  more tests per transition T01–T22 + async T27, keyed to the recovery paths
  (names quote the design §4 table), plus the phase-derivation matrix, the
  D4/D5/D6 pins, and the reset pair pinning `resetUi()`'s runstate fields;
  BK-027's `bg …` tests cover the background-run marks (T28/T29, `ui.bgLive`).
  T17/T20/T21/T27 live in resync.js since BK-034 and are imported from there.
- `observe.test.js` — BK-034's observed run (the third Stop source,
  `ui.observed`): the arm from the /messages join (T31) and rerouted events
  (T06), leave/switch (T30), the terminals (T10/T11/T18/T22), the observe-watch
  tick (T32), stop racing done, and hidden mid-run.
- `stream_md.test.js` — BK-028's streaming markdown renderer (`stream_md.js`):
  `settle()`'s hold-back and repair table, the per-frame invariant (the
  morphed bubble equals a fresh `renderMd(settle(prefix))` on every frame),
  no re-type / raw marker / dropped node between frames, selection and
  `<pre>` identity, and the 3g golden (final == `renderMd(raw)`). It uses
  DETACHED elements — fine here — but there is still no `index.html` DOM
  fixture and no rAF. `suite.html` loads the real `marked.min.js` +
  `purify.min.js` for it: without them `renderMd` falls back to `esc()` and
  every golden passes vacuously, which its first test pins.
- `jsc_check.js` — jsc `checkModuleSyntax` parse-check (trust the printed
  line, `quit(1)` exits 0).
- `run_jsunit.py` — the driver; `main()` returns the exit code so
  `tests/test_jsunit.py` can call it in-process.

## Adding a test file

Create `<module>.test.js` here, import it from `suite.js`, add both to
`jsc_check.js`. `run_jsunit.py` symlinks `*.test.js` automatically.

## Proving it can still go red

The tier guards nothing if it cannot fail. Two rituals, both belong in the run
report, never a commit:

1. **A rule's expectation:** flip an expected value in any test, rerun, expect
   a `FAIL` line naming the rule and exit 1 — then revert.
2. **The runner itself:** the permanent `xtest` control covers BOTH reporting
   primitives (a false `check` AND a deliberately-unequal `eq`), so a rot to
   "always passes" reds instead of greening the file. `suite.html` throws on
   `requestAnimationFrame` and `replay.test.js` asserts that throw, so the
   admission rule is enforced, not prose; and an async test proves an await
   chain completes before the dump (an unfinished one exits 2, never PASS).
