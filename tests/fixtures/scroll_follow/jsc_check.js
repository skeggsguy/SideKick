// jsc ESM parse-check for the scroll-follow probe's JS (no node on this host).
// Same contract as tests/fixtures/pwa_resume/jsc_check.js: this jsc build has
// no --checkModuleSyntax FLAG but does have checkModuleSyntax(src, name);
// extra CLI args EXECUTE (so: no args, list embedded), and the PRINTED lines
// are the signal, not the exit code.
// Run as:  jsc tests/fixtures/scroll_follow/jsc_check.js   (cwd = repo root)
const files = [
  ["tests/fixtures/scroll_follow/probe_scroll.js", "module"],
  ["tests/fixtures/scroll_follow/mock_fetch.js", "classic"],
  // The webui modules this probe drives for real (the scroll path): a syntax
  // error must exit 2 ("harness broke") before the browser launch.
  ["src/harness/gateway/webui/app.js", "module"],
  ["src/harness/gateway/webui/follow.js", "module"],
  ["src/harness/gateway/webui/transcript.js", "module"],
  ["src/harness/gateway/webui/stream_md.js", "module"], // BK-028: transcript.js renders the bubble through it
  ["src/harness/gateway/webui/thinking.js", "module"],
  ["src/harness/gateway/webui/pills.js", "module"],  // BK-019 4b: changed module, the app/probe evaluates it via transcript
  ["src/harness/gateway/webui/events.js", "module"],
  ["src/harness/gateway/webui/replay.js", "module"],
  ["src/harness/gateway/webui/state.js", "module"],
  ["src/harness/gateway/webui/dom.js", "module"],
  ["src/harness/gateway/webui/sessions.js", "module"],
  ["src/harness/gateway/webui/status.js", "module"],
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
