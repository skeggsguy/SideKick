// jsc ESM parse-check for the resume probe's JS (no node on this host).
// learnings 2026-10-13/16: this jsc build has no --checkModuleSyntax FLAG but
// does have checkModuleSyntax(src, name) / checkSyntax(src) functions; the CLI
// EXECUTES extra .js args (so: no args, file list embedded below) and quit(1)
// exits 0 — the PRINTED lines are the signal, not the exit code.
// Run as:  jsc tests/fixtures/pwa_resume/jsc_check.js   (cwd = repo root)
const files = [
  ["tests/fixtures/pwa_resume/probe.js", "module"],
  ["tests/fixtures/pwa_resume/probe_coldstart.js", "module"],
  ["tests/fixtures/pwa_resume/probe_live.js", "module"],
  // Step 4 (compaction-checkpoint-bookmark): the compaction live-UI scenario.
  ["tests/fixtures/pwa_resume/probe_compaction.js", "module"],
  ["tests/fixtures/pwa_resume/mock_fetch.js", "classic"],
  // The webui modules the probe drives (thinking-block-vanish instrumentation
  // lives here) — parse-checked on every probe run, so a syntax error exits 2
  // ("harness broke") before the chrome launch instead of failing the page.
  // app.js is NOT loaded by the probe (its entry tag is swapped) but carries
  // the ?recoverdebug=1 capture boot block — same parse guarantee, free.
  ["src/harness/gateway/webui/thinking.js", "module"],
  ["src/harness/gateway/webui/pills.js", "module"],  // BK-019 4b: changed module, the app/probe evaluates it via transcript
  ["src/harness/gateway/webui/transcript.js", "module"],
  ["src/harness/gateway/webui/stream_md.js", "module"], // BK-028: transcript.js renders the bubble through it
  // Step 4 (compaction-checkpoint-bookmark): the seam painter + the status
  // phase scenario M asserts on — both driven for real, so parse-checked.
  ["src/harness/gateway/webui/compaction.js", "module"],
  ["src/harness/gateway/webui/status.js", "module"],
  // Step 6 (pwa-accurate-resume): the seq/gap-fill machinery + the moved
  // replay helpers — transcript imports it and it imports transcript back.
  ["src/harness/gateway/webui/replay.js", "module"],
  ["src/harness/gateway/webui/events.js", "module"],
  // Step 7 (pwa-accurate-resume): state.js — the collapsed ui.resync recovery
  // state every module reads; probe_live asserts on it, so parse-check it too.
  ["src/harness/gateway/webui/state.js", "module"],
  // app.js + its cold-start boot path (sessions.js owns the lastChat store);
  // scenario I loads both for real, so a parse error must exit 2, not 1.
  ["src/harness/gateway/webui/app.js", "module"],
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
