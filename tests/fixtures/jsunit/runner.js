/* runner.js — the offline JS unit tier's micro-runner (plan
   bk-019-run-lifecycle Step 2; BK-020's minimal tier).

   Why a micro-runner (not node/vitest): there is no node on this host
   (.claude/gotchas.md "No node on the Mac") and the modules under test are
   browser ES modules. The tier runs the REAL modules under the cached
   chrome-headless-shell — recipe copied from
   tests/fixtures/pwa_resume/run_probe.py (symlink mirror, http.server,
   --dump-dom) — with NO DOM fixture and no fetch mock: only DOM-free
   modules belong here.

   The contract with run_jsunit.py: results land in <pre id="jsunit-results">
   wrapped in JSUNIT-RESULTS-BEGIN / JSUNIT-RESULTS-END markers, one
   `PASS name | detail` / `FAIL name | detail` line per assertion plus a
   final `JSUNIT-RESULT: PASS|FAIL` line. Detail strings are clipped and
   newline-free — a multi-line detail breaks the block parser (the probe
   textContent trap, tests/fixtures/pwa_resume/probe.js check()). rAF never fires under
   --virtual-time-budget (gotchas): fine here, rAF-free is this tier's
   admission rule, ENFORCED by suite.html's throwing requestAnimationFrame
   stub (a violation is a loud page error, not a silent no-op); the budget is
   what lets async (promise-driven, Step 3) tests finish before the dump. */

import { ui } from "./state.js";

const results = [];
const tests = [];
let failed = 0;
let checksRun = 0;
let mode = "normal"; // "normal" | "xfail" — see xtest()

export function test(name, fn) {
  tests.push({ name, fn, xfail: false });
}

// A positive control (the probes' rule: a detector that has never gone red
// proves nothing). Its checks are EXPECTED to fail: caught = PASS, any check
// that PASSES means the check itself is vacuous — that prints a real FAIL.
// Keeps check()/eq() themselves guarded without a permanent red line.
// A NEW reporting primitive must join the control in replay.test.js too, or it is unguarded.
export function xtest(name, fn) {
  tests.push({ name, fn, xfail: true });
}

export function check(name, pass, detail) {
  checksRun += 1;
  if (mode === "xfail") {
    if (pass) {
      results.push(`FAIL ${name} | expected-fail check PASSED — the assertion is vacuous`);
      failed += 1;
    } else {
      results.push(`PASS ${name} | xfail control caught the false assertion${detail ? " | " + detail : ""}`);
    }
    return;
  }
  results.push(`${pass ? "PASS" : "FAIL"} ${name}${detail ? " | " + detail : ""}`);
  if (!pass) failed += 1;
}

// Deep-compare via JSON (everything asserted here is a plain value: numbers,
// arrays of intervals, arrays of seqs). The detail line carries got/want so
// a red names the drift without editing the test to print anything.
export function eq(name, actual, expected) {
  let a, e;
  try {
    a = JSON.stringify(actual);
    e = JSON.stringify(expected);
  } catch (_) {
    check(name, false, "unserializable value");
    return;
  }
  check(name, a === e, a === e ? "" : `got ${clip(a)} want ${clip(e)}`);
}

function clip(v) {
  return String(v).replace(/\s+/g, " ").slice(0, 180);
}

// Fresh shared state before every test. Fixtures arm `ui` fields directly —
// that is the project's test seam (plan bk-019 constraint), and it is this
// tier's isolation mechanism too: replay.js and runstate.js read ui at every
// call, so re-arming the fields is the whole reset. (runstate.js tests write
// resync/liveTurn/expecting too — leaving them armed across tests would let
// one test's stream leak into the next; runstate.test.js pins this reset.)
function resetUi() {
  ui.sessionId = null;
  ui.expecting = false;
  ui.resync = null;
  ui.liveTurn = null;
  ui.liveSeq = {};
  ui.liveDone = {};
  ui.bgLive = {};
  ui.observed = null; // BK-034
}

// Direct view of the dedupe slot (the maps under test) — the tests read slot
// CONTENT through it exactly as the fixtures read ui elsewhere.
export function slotOf(id) {
  return ui.liveSeq ? ui.liveSeq[id] : undefined;
}

export async function runAll() {
  const pre = document.querySelector("#jsunit-results");
  for (const t of tests) {
    resetUi();
    const before = checksRun;
    mode = t.xfail ? "xfail" : "normal";
    try {
      await t.fn();
    } catch (e) {
      // Back to normal BEFORE reporting: a thrown xtest must be a real FAIL,
      // not an "xfail control caught it" PASS (the control's job is to catch
      // a false assertion, never its own exception).
      mode = "normal";
      check(t.name, false, "threw: " + clip(e && e.message ? e.message : e));
    }
    mode = "normal";
    if (checksRun === before) check(t.name, false, "no assertion ran (vacuous test)");
  }
  results.push(
    `JSUNIT-RESULT: ${failed === 0 ? "PASS" : "FAIL"} — ${tests.length} tests, ` +
    `${checksRun} checks, ${failed} failing`
  );
  pre.textContent =
    "JSUNIT-RESULTS-BEGIN\n" + results.join("\n") + "\nJSUNIT-RESULTS-END";
}
