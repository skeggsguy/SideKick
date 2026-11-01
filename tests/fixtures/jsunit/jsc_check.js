// jsc ESM parse-check for the jsunit tier's JS (no node on this host).
// Same traps as tests/fixtures/pwa_resume/jsc_check.js: this jsc build has no
// --checkModuleSyntax FLAG but does have checkModuleSyntax(src, name); the
// CLI EXECUTES extra .js args (so: no args, file list embedded below) and
// quit(1) exits 0 — the PRINTED lines are the signal, not the exit code.
// Run as:  jsc tests/fixtures/jsunit/jsc_check.js   (cwd = repo root)
const files = [
  ["tests/fixtures/jsunit/runner.js", "module"],
  ["tests/fixtures/jsunit/suite.js", "module"],
  ["tests/fixtures/jsunit/replay.test.js", "module"],
  ["tests/fixtures/jsunit/runstate.test.js", "module"],
  ["tests/fixtures/jsunit/trace.test.js", "module"],
  ["tests/fixtures/jsunit/observe.test.js", "module"],
  ["tests/fixtures/jsunit/senderstop.test.js", "module"],
  ["tests/fixtures/jsunit/stream_md.test.js", "module"],
  ["tests/fixtures/jsunit/labels.test.js", "module"],
  // The real modules the suite page loads for real (the suite imports the
  // symlinked webui files) — a syntax error must exit 2 ("harness broke")
  // before the chrome launch, not fail the page.
  ["src/harness/gateway/webui/state.js", "module"],
  ["src/harness/gateway/webui/replay.js", "module"],
  ["src/harness/gateway/webui/runstate.js", "module"],
  ["src/harness/gateway/webui/labels.js", "module"], // BK-041: labels.test.js
  ["src/harness/gateway/webui/resync.js", "module"], // BK-034: runstate's verdict half
  // BK-023 Step 2: trace.js is imported by trace.test.js — a real module in
  // the suite page's import graph from here on.
  ["src/harness/gateway/webui/trace.js", "module"],
  // replay.js's transitive imports: compaction.js (flushSeams) and, through
  // it, dom.js — both evaluate at page import time.
  ["src/harness/gateway/webui/compaction.js", "module"],
  ["src/harness/gateway/webui/dom.js", "module"],
  // BK-028: stream_md.test.js drives the real renderer (it imports dom.js).
  ["src/harness/gateway/webui/stream_md.js", "module"],
  // BK-019 Step 4: the transport routers. They are NOT in the suite page's
  // import graph (they pull the whole DOM app), but they are the modules this
  // step rewrites — parse-check them here so a syntax error exits 2
  // ("harness broke") before the chrome launch instead of riding to the
  // browser probes.
  ["src/harness/gateway/webui/app.js", "module"],
  ["src/harness/gateway/webui/events.js", "module"],
  ["src/harness/gateway/webui/sessions.js", "module"],
];
let bad = 0;
for (const pair of files) {
  const path = pair[0];
  const kind = pair[1];
  let verdict = "OK";
  let detail = "";
  try {
    if (kind === "module") checkModuleSyntax(readFile(path), path);
    else checkSyntax(path); // THIS build's checkSyntax takes a FILENAME, not source
  } catch (e) {
    verdict = "FAIL";
    detail = ": " + e;
    bad += 1;
  }
  print("SYNTAX-" + verdict + " " + kind + " " + path + detail);
}
print(bad === 0 ? "JSC-RESULT: PASS" : "JSC-RESULT: FAIL");
