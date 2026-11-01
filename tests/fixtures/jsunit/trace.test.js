/* trace.test.js — the ?recoverdebug=1 capture's formatter (BK-023 Step 2).

   trace.js imports nothing and touches no DOM, so the real module runs here
   directly. The rules each check names are the plan's acceptance criteria:
   (1) a streamed answer's capture carries no per-chunk no-op lines — runs of
   no-op endThinking collapse to one `×count` line while hadBlock:true closes
   and the recovery transitions stay verbatim; (2) every line carries the
   EVENT-time stamp, so a paced stream and a catch-up burst render different
   spans.

   The stamp is LOCAL time (getHours), which this tier's tz cannot pin — so
   the checks assert the HH:MM:SS.mmm SHAPE plus the exact millisecond digits
   (ms is the only tz-free component of the clock, and e[3] % 1000 is exact),
   and compare rendered SPANS in seconds-of-day with a midnight-wrap-safe
   difference. Nothing here reformats a date: every expectation reads off the
   rendered line. */

import { test, check, eq } from "./runner.js";
import { renderEntry, createCapture } from "./trace.js";

// One fixed epoch base (2024-10-27T04:53:20.000Z) — any local day works.
const T0 = 1730000000000;

// Realistic stacks: the tlog frame is present and must be filtered; the
// leading "Error" line (V8) must drop too. paintStream/startToolPill are the
// frames pwa_resume probe F cares about living in these stacks.
const V8 = "Error\n" +
  "    at tlog (http://s/thinking.js:57:38)\n" +
  "    at endThinking (http://s/thinking.js:121:80)\n" +
  "    at paintStream (http://s/transcript.js:220:23)\n" +
  "    at startToolPill (http://s/pills.js:96:5)";
const JSC = "tlog@http://s/thinking.js:57:38\n" +
  "endThinking@http://s/thinking.js:121:80\n" +
  "paintStream@http://s/transcript.js:220:23";

const noopClose = (ms, extra) =>
  ["endThinking", { hadBlock: false, replay: false }, V8, ms + (extra || 0)];

const linesOf = (cap) => (cap.text() ? cap.text().replace(/\n$/, "").split("\n") : []);

const STAMP_RE = /^\d{2}:\d{2}:\d{2}\.\d{3}$/;
const isStamp = (s) => STAMP_RE.test(s);
const msDigits = (stamp) => Number(stamp.split(".")[1]); // tz-free
const secOfDay = (stamp) => {
  const [h, m, rest] = stamp.split(":");
  return Number(h) * 3600 + Number(m) * 60 + Number(rest);
};
// The span a collapsed line renders: `first-last` head. Midnight-wrap-safe.
const spanOf = (line) => {
  const m = line.match(/^(\d{2}:\d{2}:\d{2}\.\d{3})-(\d{2}:\d{2}:\d{2}\.\d{3})\s/);
  if (!m || !m[1]) return null;
  return (secOfDay(m[2]) - secOfDay(m[1]) + 86400) % 86400;
};
const headStamps = (line) =>
  line.match(/^(\d{2}:\d{2}:\d{2}\.\d{3})(?:-(\d{2}:\d{2}:\d{2}\.\d{3}))?/);

// ── criterion 1: no per-chunk no-op lines ─────────────────────────────────

test("collapse: 500 identical no-op endThinking entries render ONE ×500 line", () => {
  const cap = createCapture();
  const entries = [];
  for (let i = 0; i < 500; i++) entries.push(noopClose(T0 + i)); // 1 ms apart
  cap.push(entries);
  const lines = linesOf(cap);
  eq("collapse: 500 no-op closes → exactly one capture line", lines.length, 1);
  check("collapse: the line carries the run count ×500", lines[0].includes(" ×500 "), lines[0]);
  check("collapse: the line spans first→last event time (499 ms)",
    Math.abs(spanOf(lines[0]) - 0.499) < 0.0005, `span=${spanOf(lines[0])} line=${lines[0]}`);
  check("collapse: the span keeps the caller frames (paintStream)",
    lines[0].includes("paintStream (http://s/transcript.js"), lines[0]);
});

test("collapse: a single no-op close renders with count ×1 (uniform rule)", () => {
  const cap = createCapture();
  cap.push([noopClose(T0)]);
  const lines = linesOf(cap);
  eq("collapse: one no-op → one line", lines.length, 1);
  check("collapse: one no-op still renders ×1 — no-op closes never take the verbatim shape",
    lines[0].includes(" ×1 ") && headStamps(lines[0]) !== null &&
      headStamps(lines[0])[2] !== undefined, lines[0]);
  check("collapse: ×1 line spans first→last (equal stamps)",
    Math.abs(spanOf(lines[0])) < 0.0005, lines[0]);
});

test("collapse: a run straddling pump drains merges into ONE line (count and span update)", () => {
  const cap = createCapture();
  for (let d = 0; d < 6; d++) cap.push([noopClose(T0 + d * 2000), noopClose(T0 + d * 2000 + 1000)]);
  const lines = linesOf(cap);
  eq("collapse: 6 drains of 2 identical no-ops → one line", lines.length, 1);
  check("collapse: the merged line counts all 12 across drains",
    lines[0].includes(" ×12 "), lines[0]);
  check("collapse: the merged span reaches the LAST entry's stamp (11000 ms of event time)",
    Math.abs(spanOf(lines[0]) - 11.0) < 0.0005, `span=${spanOf(lines[0])} line=${lines[0]}`);
});

test("collapse: a no-op run is NOT merged across a transition in a later push", () => {
  // A drain boundary is not a semantic one (merging across it is the rule
  // above); a REAL entry between two runs of the same shape is — otherwise a
  // capture would print one ×4 line over a startThinking that bisects it.
  const cap = createCapture();
  cap.push([noopClose(T0), noopClose(T0 + 1)]);
  cap.push([["startThinking", { hadPrev: false }, null, T0 + 2]]);
  cap.push([noopClose(T0 + 3), noopClose(T0 + 4)]);
  const lines = linesOf(cap);
  eq("collapse: run / pushed transition / same-shape run → three lines",
    lines.length, 3);
  check("collapse: each side keeps its own count (×2 and ×2 — never ×4)",
    lines[0].includes(" ×2 ") && lines[2].includes(" ×2 ") &&
      !lines.join("").includes("×4"), lines.join(" ⏎ "));
});

test("collapse: a real close ENDS the run — the next no-op opens a new line", () => {
  const cap = createCapture();
  cap.push([noopClose(T0), noopClose(T0 + 1),
    ["endThinking", { hadBlock: true, replay: false }, V8, T0 + 2],
    noopClose(T0 + 3)]);
  const lines = linesOf(cap);
  eq("collapse: no-op run / verbatim close / new no-op → three lines", lines.length, 3);
  check("collapse: the hadBlock:true close between them stays verbatim",
    lines[1].includes('"hadBlock":true') && !lines[1].includes("×"), lines[1]);
  check("collapse: the run after the close starts fresh at ×1",
    lines[2].includes(" ×1 "), lines[2]);
});

test("collapse: runs key on state — a replay-flag flip is not the same run", () => {
  const cap = createCapture();
  cap.push([noopClose(T0),
    ["endThinking", { hadBlock: false, replay: true }, V8, T0 + 1],
    noopClose(T0 + 2)]);
  eq("collapse: replay:false / replay:true / replay:false → three separate ×1 lines",
    linesOf(cap).length, 3);
});

// ── criterion 2: the capture carries event-time stamps ────────────────────

test("stamps: a paced stream and a catch-up burst of identical entries render different spans", () => {
  const paced = createCapture();
  const burst = createCapture();
  const pacedEntries = [];
  for (let i = 0; i < 500; i++) pacedEntries.push(noopClose(T0 + i * 80)); // 80 ms apart
  paced.push(pacedEntries);
  const burstEntries = [];
  for (let i = 0; i < 499; i++) burstEntries.push(noopClose(T0)); // same instant…
  burstEntries.push(noopClose(T0 + 80)); // …except the last
  burst.push(burstEntries);
  const p = linesOf(paced)[0];
  const b = linesOf(burst)[0];
  eq("stamps: both shapes still collapse to one line each", [linesOf(paced).length, linesOf(burst).length], [1, 1]);
  check("stamps: the paced span reads 39920 ms of event time",
    Math.abs(spanOf(p) - 39.92) < 0.0005, `span=${spanOf(p)}`);
  check("stamps: the burst span reads 80 ms of event time",
    Math.abs(spanOf(b) - 0.08) < 0.0005, `span=${spanOf(b)}`);
  check("stamps: identical entries differing only in event time render DIFFERENT lines", p !== b);
});

test("stamps: every rendered line opens with HH:MM:SS.mmm carrying the entry's own e[3]", () => {
  const cap = createCapture();
  cap.push([
    ["startThinking", { hadPrev: false }, null, T0 + 5],
    ["appendThinking", { hadBlock: true }, null, T0 + 1234],
    ["catchingUp", { type: "delta" }, null, T0 + 5678],
  ]);
  const lines = linesOf(cap);
  eq("stamps: three transitions → three lines", lines.length, 3);
  const stamps = lines.map((l) => headStamps(l) && headStamps(l)[1]);
  eq("stamps: each line's stamp carries its entry's exact millisecond digits",
    stamps.map((s) => (s ? msDigits(s) : null)), [5, 234, 678]);
  check("stamps: each line's stamp is shaped HH:MM:SS.mmm", stamps.every(isStamp), JSON.stringify(stamps));
});

// ── criterion 1, other half: the verbatim set ─────────────────────────────

test("verbatim: hadBlock:true closes and the recovery transitions render one stamped line each", () => {
  const cap = createCapture();
  cap.push([
    ["renderSession", { children: 3, replaying: false }, JSC, T0],
    ["startThinking", { hadPrev: false }, null, T0 + 10],
    ["resetTranscript", { children: 2, hadBubble: true, replaying: false }, V8, T0 + 20],
    ["catchingUp", { type: "delta", session: "s1" }, null, T0 + 30],
    ["run_done:refetch", { session: "s1" }, null, T0 + 40],
    ["endThinking", { hadBlock: true, replay: true }, V8, T0 + 50],
  ]);
  const lines = linesOf(cap);
  eq("verbatim: six real transitions → six lines (nothing collapses)", lines.length, 6);
  check("verbatim: no transition line carries a collapse count",
    lines.every((l) => !l.includes("×")), lines.join(" ⏎ "));
  check("verbatim: every line opens with its own stamp",
    lines.every((l) => headStamps(l) !== null), lines[0]);
  check("verbatim: the hadBlock:true replay close keeps its state verbatim",
    lines[5].includes('"hadBlock":true') && lines[5].includes('"replay":true'), lines[5]);
});

test("verbatim: appendThinking {hadBlock:false} — the zombie-spawn signal — never collapses", () => {
  const cap = createCapture();
  const entries = [];
  for (let i = 0; i < 20; i++) {
    entries.push(["appendThinking", { hadBlock: false }, null, T0 + i]);
  }
  cap.push(entries);
  eq("verbatim: 20 identical appendThinking zombie-spawns render 20 lines", linesOf(cap).length, 20);
});

// ── the frame filter (engine dialects), moved verbatim out of app.js ──────

test("frames: V8 dialect (at fn (url)) renders the caller chain, tlog frame dropped", () => {
  const line = renderEntry(noopClose(T0));
  check("frames: V8 — paintStream renders as `fn (url)`",
    line.includes("at paintStream (http://s/transcript.js:220:23)"), line);
  check("frames: V8 — the tlog frame and the bare Error line are filtered out",
    !line.includes("tlog") && !line.includes("Error"), line);
  eq("frames: V8 — the frame cap keeps exactly the first 3 caller frames",
    line.split(" < ").length - 1, 3);
});

test("frames: JSC dialect (fn@url:line:col) renders the caller chain, tlog frame dropped", () => {
  const line = renderEntry(["endThinking", { hadBlock: true, replay: false }, JSC, T0]);
  check("frames: JSC — paintStream renders as `fn@url`",
    line.includes("paintStream@http://s/transcript.js:220:23"), line);
  check("frames: JSC — the tlog frame is filtered out", !line.includes("tlog"), line);
  eq("frames: JSC — both caller frames survive (2 joined)", line.split(" < ").length - 1, 2);
});

test("frames: a no-stack entry (null at index 2) renders name and state with no frames", () => {
  const line = renderEntry(["startThinking", { hadPrev: true }, null, T0]);
  check("frames: null stack → no ` < ` marker at all", !line.includes(" < "), line);
  check("frames: null stack still renders name and state after the stamp",
    line.endsWith(' startThinking {"hadPrev":true}'), line);
});

// ── the capture's accumulation contract (the pump re-renders text() whole) ──

test("capture: text() accumulates across pushes (a wiped <pre> refills from the capture)", () => {
  const cap = createCapture();
  eq("capture: empty capture renders the empty string", cap.text(), "");
  cap.push([["startThinking", { hadPrev: false }, null, T0]]);
  const first = cap.text();
  cap.push([["resetTranscript", { children: 1, hadBubble: false, replaying: false }, null, T0 + 9]]);
  check("capture: the second push APPENDS — the first line is still there in full",
    cap.text().startsWith(first) && linesOf(cap).length === 2, cap.text().replace(/\n/g, " ⏎ "));
  check("capture: the text ends with a newline (the <pre> body shape)",
    cap.text().endsWith("\n"));
});
