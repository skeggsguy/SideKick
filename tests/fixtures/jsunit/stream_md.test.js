/* stream_md.test.js — the streaming markdown renderer (BK-028, goals 3a–3g).

   Runs the REAL stream_md.js + dom.js with the REAL marked + DOMPurify:
   suite.html loads both as classic scripts. Without them renderMd falls back
   to esc() and every golden below passes vacuously — the first test pins
   that they are loaded AND that dom.js's del override is active.

   Detached elements only (no layout, no rAF): scrollLeft itself is a probe
   reading (scratch/smoothness_probe/extra.py); here its precondition is
   pinned instead — the <pre> node survives from first paint to the final.

   Fixtures are DATA copied from the probes: the smoothness probe's answer
   (scratch/smoothness_probe/instrument.js) and scroll_follow's shape-mixed
   (tests/fixtures/scroll_follow/mock_fetch.js), plus an edge fixture for the
   lexing traps the design names (reference links, setext, loose lists, raw
   HTML, tilde fences). */

import { test, check, eq } from "./runner.js";
import { renderMd } from "./dom.js";
import { settle, streamRender } from "./stream_md.js";

// ── fixtures ──────────────────────────────────────────────────────────────
const range = (n, f) => Array.from({ length: n }, (_, i) => f(i));
const PROBE_ANSWER = [
  "## What I found\n\nThe **ingestion path** is the bottleneck, not the parser. Three things stand out, and the `batch_size` default is the biggest one:\n\n",
  "1. The worker pool is capped at **four** threads even on the Mac mini.\n2. Each batch re-opens the SQLite connection (`connect()` per call).\n3. Retries back off linearly, so a flaky upstream stalls the whole queue.\n\n",
  "Here is the change I would make first:\n\n```python\nasync def ingest(items, *, batch_size=256):\n    async with pool.acquire() as conn:\n" +
    range(18, (i) => `        await conn.execute(SQL_${i}, items[${i}::batch_size])  # step ${i}\n`).join("") + "    return len(items)\n```\n\n",
  "And the measured effect across the last week of runs:\n\n| Run | Before (s) | After (s) | Items | Notes |\n| --- | --- | --- | --- | --- |\n" +
    range(12, (i) => `| run-${i + 1} | ${40 + i * 3} | ${12 + i} | ${1200 + i * 90} | ${i % 3 ? "warm_cache_prefix_reused_from_slot_0" : "**cold**_start_full_prefill_of_the_context"} |\n`).join("") + "\n",
  range(5, (i) => `Paragraph ${i + 1}: the remaining cost is mostly *prefill* on the local lane, which is why the second number barely moves when the cache is warm. If we wanted more, the next lever is the \`chunk_overlap\` setting, but that trades recall for speed and I would not touch it without an eval run first.\n\n`).join(""),
].join("");

const SEEDS = [
  "- 🚀 **ship** — the pilot hit every milestone ✅ and the weekly notes are signed off; the team that ran it would carry the migration effort themselves if asked, which says more than any benchmark chart did",
  "- ⚠️ **hold** — procurement flagged the licence 🧾 twice, and the third flag is a blocker: the per-seat number turns commercial exactly at our headcount, the line the committee was never given in writing",
  "- 🧪 **pilot** — two teams 🤝 volunteered for the Q1 fortnight, on the explicit condition that the raw notes stay in the shared folder this time, not in one person's drafts",
  "- 🗑️ **drop** — a single maintainer and an issue backlog that has been slow 🐌 since summer; neither is a criticism of the work, both are a description of the bus factor",
  "- 🔁 **revisit** — re-score when the pricing page 📄 changes or the licence terms move, whichever comes first, and not before: the September numbers were honest, they are just no longer current",
];
const SHAPE_MIXED = [
  "The short version first, then the list the committee argued over, then the table it actually voted on:\n\n",
  range(3, (s) => SEEDS.map((t, i) => t.replace(/—/, `—(${s + 1}.${i + 1})`)).join("\n") + "\n").join(""),
  "\nAnd the votes, as recorded:\n\n",
  "| Tool | Vote | Note |\n| --- | --- | --- |\n",
  range(8, (r) => [
    `| alpha-${r + 1} | 🚀 ship | warm cache wins 🏆 the cold benchmarks, and the migration cost stayed under the estimate for once |`,
    `| boron-${r + 1} | ⚠️ hold | the licence 🧾 turns commercial at our headcount; procurement wants the number in writing before anyone signs |`,
    `| cypher-${r + 1} | 🧪 pilot | two teams 🤝 volunteered for the fortnight, conditional on the raw notes living in the shared folder |`,
    `| dynamo-${r + 1} | 🗑️ drop | one maintainer, and the issue backlog has been slow 🐌 since summer — descriptive, not an insult |`,
  ].join("\n") + "\n").join(""),
  "\nOverall the room was warmer than the September vote — ship one, hold one, and revisit the rest when the pricing page changes.",
].join("");

// The design's lexing traps: a reference link resolved by a LATER def, a
// setext heading, a list that turns loose, raw HTML (details), a tilde fence,
// bold across a soft break, a table interrupting a paragraph, an unclosed
// fence at the very end of the answer.
const EDGE = [
  "Intro with a [reference link][docs] and an inline [one](https://example.com/a).\n\n",
  "Setext heading\n---\n\n",
  "- tight one\n- tight two\n\n- now loose\n\n",
  "<details><summary>More</summary>\n\nhidden **body**\n\n</details>\n\n",
  "~~~js\nconst a = `tick` ** 2;\n~~~\n\n",
  "Bold that **spans\na soft break** and snake_case_names and 2*3 and a * b.\n\n",
  "Para right above\n| h1 | h2 |\n| -- | -- |\n| c1 | c2 |\n\n",
  "> quoted *emph* and ~~struck~~ and ~5 to ~10\n\n",
  "[docs]: https://example.com/docs\n\n",
  "Last, an unclosed fence:\n\n```sh\necho hi\n",
].join("");

const FIXTURES = [["probe-answer", PROBE_ANSWER], ["shape-mixed", SHAPE_MIXED], ["edge", EDGE]];

// ── helpers ───────────────────────────────────────────────────────────────
const bubble = () => document.createElement("div");
const freshHTML = (src) => { const d = bubble(); d.innerHTML = renderMd(src); return d.innerHTML; };
const sig = (md) => Array.from(md.children, (c) => c.tagName + (c.className ? "." + c.className : ""));
const proseText = (md) => {
  let t = "";
  const w = document.createTreeWalker(md, NodeFilter.SHOW_TEXT);
  for (let n; (n = w.nextNode());) if (!n.parentElement.closest("pre, code")) t += n.data;
  return t;
};
// The smoothness probe's raw-marker signature (instrument.js), prose only.
const RAW_MARKER = /\*\*|^\s*\|.*\|\s*$|```/m;
// Stride generator: deterministic 1–37 char steps, like a paced reveal frame.
function* strides(len, seed) {
  let n = 0, x = seed;
  while (n < len) { x = (x * 1103515245 + 12345) & 0x7fffffff; n = Math.min(len, n + 1 + (x % 37)); yield n; }
}
const noSplit = (raw, n) => { const c = raw.charCodeAt(n - 1); return c >= 0xd800 && c <= 0xdbff ? n - 1 : n; };

// ── the tier contract: real marked + DOMPurify, or every golden is vacuous ─
test("stream_md: marked + DOMPurify are loaded (renderMd is not the esc() fallback)", () => {
  check("window.marked and window.DOMPurify exist", !!(window.marked && window.DOMPurify));
  check("renderMd('**x**') renders <strong>", renderMd("**x**").includes("<strong>x</strong>"), renderMd("**x**"));
  check("dom.js's del override is active (single ~ renders literally)",
    !renderMd("~5 to ~10").includes("<del>"), renderMd("~5 to ~10"));
});

// ── settle(): block hold-back and inline repair, table-driven ─────────────
const SETTLE_CASES = [
  // fences
  ["fence opener waits for its newline", "Intro:\n\n```py", "Intro:\n\n"],
  ["partial fence opener `` waits", "Intro:\n``", "Intro:\n"],
  ["fence open after its newline: empty code block shows", "Intro:\n\n```py\n", "Intro:\n\n```py\n"],
  ["inside an open fence: no inline repair", "```py\nf(**kw", "```py\nf(**kw"],
  ["inside an open fence: the closer waits for its newline", "```py\nx = 1\n``", "```py\nx = 1\n"],
  ["inside an open fence: a full closer line still waits", "```py\nx = 1\n```", "```py\nx = 1\n"],
  ["fence parity: after a closed fence, repair resumes", "```\na\n```\n\nb **c", "```\na\n```\n\nb **c**"],
  ["indented fence counts toward parity", "- item\n\n  ```\n  code **x", "- item\n\n  ```\n  code **x"],
  ["tilde fence opener waits", "Then\n~~~", "Then\n"],
  // tables
  ["table header typing waits", "Votes:\n\n| a | b", "Votes:\n\n"],
  ["table header with no delimiter row waits", "Votes:\n\n| a | b |\n", "Votes:\n\n"],
  ["partial delimiter row: header still waits", "Votes:\n\n| a | b |\n| --- | -", "Votes:\n\n"],
  ["complete delimiter row releases the table", "Votes:\n\n| a | b |\n| --- | --- |\n", "Votes:\n\n| a | b |\n| --- | --- |\n"],
  ["a body row repairs inline", "| a | b |\n| - | - |\n| 1 | **x", "| a | b |\n| - | - |\n| 1 | **x**"],
  ["a line merely containing a pipe is never held", "so a | b", "so a | b"],
  ["three pipe lines with no delimiter are text", "| a |\n| b |\n| c", "| a |\n| b |\n| c"],
  ["a table interrupting a paragraph: only the header waits", "Para\n| a | b |\n| -", "Para\n"],
  // headings, lists, rules
  ["bare # waits", "Foo\n#", "Foo\n"],
  ["bare ## waits", "##", ""],
  ["heading with text shows", "## T", "## T"],
  ["bare 1 waits", "Intro\n\n1", "Intro\n\n"],
  ["bare 1. waits", "1.", ""],
  ["list item with text shows", "1. x", "1. x"],
  ["a lazy 2 waits", "1. a\n2", "1. a\n"],
  ["bare - waits", "-", ""],
  ["bullet with no text waits", "- ", ""],
  ["setext - waits", "Para\n-", "Para\n"],
  ["bullet with text shows", "- a", "- a"],
  ["** opener at line start waits", "**", ""],
  ["raw HTML tag still arriving waits", "Text\n<details cl", "Text\n"],
  ["a lone < waits", "Text\n<", "Text\n"],
  ["prose starting with '< ' is not held", "Text\n< 5 ms is the target", "Text\n< 5 ms is the target"],
  ["prose starting with '<5' is not held", "<5ms is the target", "<5ms is the target"],
  // inline repair
  ["dangling ** closes", "Hello **wor", "Hello **wor**"],
  ["balanced ** unchanged", "Hello **world**", "Hello **world**"],
  ["half a closer is stripped then closed", "Hello **world*", "Hello **world**"],
  ["trailing opener stripped", "Hello **", "Hello "],
  ["closer goes before trailing whitespace", "**bold ", "**bold** "],
  ["unclosed backtick closes", "use `batch_si", "use `batch_si`"],
  ["** inside an open code span is not closed", "`a **b", "`a **b`"],
  ["** inside a complete code span is not counted", "`**` then **b", "`**` then **b**"],
  ["closers innermost first", "**b `c", "**b `c`**"],
  ["escaped backtick is not a code span", "a \\` b", "a \\` b"],
  ["single * opener closes", "mostly *pref", "mostly *pref*"],
  ["2*3 and a * b are spared", "2*3 and a * b", "2*3 and a * b"],
  ["*** is never repaired", "***x", "***x"],
  ["~~ is never repaired", "~~gone", "~~gone"],
  ["snake_case untouched", "the snake_case_name is", "the snake_case_name is"],
  ["a trailing _ waits", "warm_", "warm"],
  ["partial link shows as text, no href", "see [the docs](https://ex", "see the docs"],
  ["partial link with empty url", "see [the docs](", "see the docs"],
  ["complete link unchanged", "see [d](https://e.x) ok", "see [d](https://e.x) ok"],
  // surrogate pairs
  ["half an emoji is held", "ok \ud83d", "ok "],
  ["a whole emoji shows", "ok 🚀", "ok 🚀"],
  ["empty input", "", ""],
];
test("stream_md: settle() holds back re-typing lines and repairs the last line", () => {
  for (const [name, input, want] of SETTLE_CASES) {
    const got = settle(input);
    check(`settle: ${name}`, got === want, got === want ? "" : `got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
  }
});
test("stream_md: settle() is idempotent on its own output", () => {
  let bad = [];
  for (const [, input] of SETTLE_CASES) { const once = settle(input); if (settle(once) !== once) bad.push(input); }
  for (const [, raw] of FIXTURES) for (const n of strides(raw.length, 7)) {
    const once = settle(raw.slice(0, n));
    if (settle(once) !== once) { bad.push(raw.slice(0, n).slice(-40)); break; }
  }
  eq("settle(settle(x)) === settle(x) for every case and fixture frame", bad, []);
});

// ── per-frame invariant: the morphed bubble IS a fresh render, every frame ─
test("stream_md: after every streamRender the bubble equals a fresh renderMd(settle(prefix))", () => {
  for (const [name, raw] of FIXTURES) {
    for (const [mode, ns] of [["every char", range(raw.length, (i) => i + 1)], ["paced strides", [...strides(raw.length, 3)]]]) {
      const md = bubble();
      let first = null, frames = 0;
      for (const n0 of ns) {
        const n = noSplit(raw, n0);
        streamRender(md, raw.slice(0, n));
        frames++;
        if (first === null && md.innerHTML !== freshHTML(settle(raw.slice(0, n)))) first = n;
      }
      check(`${name} (${mode}): morph == fresh render on all ${frames} frames`, first === null,
        first === null ? "" : `first diverging prefix ends ${JSON.stringify(raw.slice(Math.max(0, first - 50), first))}`);
    }
  }
});

// ── between consecutive frames: no re-type, no raw markers, kept nodes kept ─
test("stream_md: consecutive frames never re-type a block or show raw markers (3a, 3b)", () => {
  for (const [name, raw] of FIXTURES.slice(0, 2)) { // edge carries the accepted residuals (setext, loose list)
    const md = bubble();
    let prev = [], retypes = [], rawFrames = [], dropped = 0;
    for (const n0 of strides(raw.length, 11)) {
      const n = noSplit(raw, n0);
      const before = Array.from(md.childNodes), lastSer = md._last || [];
      streamRender(md, raw.slice(0, n));
      const now = sig(md);
      for (let i = 0; i < Math.min(prev.length, now.length); i++) {
        if (prev[i] !== now[i]) { retypes.push(`${prev[i]}→${now[i]}@${n}`); break; }
      }
      if (RAW_MARKER.test(proseText(md))) rawFrames.push(n);
      // a node whose serialization did not change must be the SAME node
      const after = Array.from(md.childNodes);
      for (let i = 0; i < Math.min(before.length, after.length); i++) {
        if (md._last[i] === lastSer[i] && before[i] !== after[i]) dropped++;
      }
      prev = now;
    }
    eq(`${name}: block re-types between frames`, retypes.slice(0, 3), []);
    eq(`${name}: frames showing a raw ** / | row / fence in prose`, rawFrames.slice(0, 5), []);
    eq(`${name}: unchanged top-level nodes replaced`, dropped, 0);
  }
});

test("stream_md: a selection in a completed block survives the rest of the stream (3c)", () => {
  const md = bubble();
  const raw = PROBE_ANSWER, at = raw.indexOf("1. The worker");
  streamRender(md, raw.slice(0, at));
  const h2 = md.querySelector("h2");
  const r = document.createRange();
  r.selectNodeContents(h2);
  const text = r.toString();
  for (const n of strides(raw.length, 5)) if (n > at) streamRender(md, raw.slice(0, noSplit(raw, n)));
  streamRender(md, raw, true);
  check("the heading node is still in the bubble", md.contains(h2)); // detached: isConnected is always false
  eq("the range still selects the same text", r.toString(), text);
  check("the selected text is the heading", text === "What I found", text);
});

test("stream_md: the open block's text grows in place (appendData)", () => {
  const md = bubble();
  streamRender(md, "One two thr");
  const t = md.querySelector("p").firstChild;
  const r = document.createRange();
  r.setStart(t, 0); r.setEnd(t, 3);
  streamRender(md, "One two three four");
  check("same text node", md.querySelector("p").firstChild === t);
  eq("text", t.data, "One two three four");
  eq("a range over its prefix survives", r.toString(), "One");
});

test("stream_md: the code block <pre> is one node from first paint to the final (3d precondition)", () => {
  const md = bubble();
  const raw = PROBE_ANSWER;
  let pre = null, swaps = 0;
  for (const n of strides(raw.length, 13)) {
    streamRender(md, raw.slice(0, noSplit(raw, n)));
    const p = md.querySelector("pre");
    if (p && !pre) pre = p;
    else if (pre && p !== pre) swaps++;
  }
  streamRender(md, raw, true);
  check("a <pre> appeared mid-stream", !!pre);
  eq("times the <pre> node was replaced", swaps, 0);
  check("the final bubble still holds that <pre>", md.querySelector("pre") === pre);
  check("its code text is complete", pre.textContent.includes("return len(items)"));
});

test("stream_md: a <details> the reader opened stays open while later blocks stream", () => {
  const md = bubble();
  const raw = "<details><summary>More</summary>\n\nbody\n\n</details>\n\nAfter that, more text arrives here.";
  const at = raw.indexOf("After");
  streamRender(md, raw.slice(0, at));
  const d = md.querySelector("details");
  d.open = true;
  for (let n = at + 1; n <= raw.length; n++) streamRender(md, raw.slice(0, n));
  check("same details node", md.querySelector("details") === d);
  check("still open", d.open);
});

// ── 3g: the final render is exactly renderMd(raw) ──────────────────────────
test("stream_md: the finalized bubble is byte-identical to renderMd(raw) (3g golden)", () => {
  for (const [name, raw] of FIXTURES) {
    const md = bubble();
    for (const n of strides(raw.length, 17)) streamRender(md, raw.slice(0, noSplit(raw, n)));
    streamRender(md, raw, true);
    const want = freshHTML(raw);
    check(`${name}: streamed + final == innerHTML = renderMd(raw)`, md.innerHTML === want,
      md.innerHTML === want ? "" : `lengths ${md.innerHTML.length} vs ${want.length}`);
    check(`${name}: streamed + final == the renderMd(raw) string`, md.innerHTML === renderMd(raw));
    const replay = bubble();
    streamRender(replay, raw, true); // history replay: an empty bubble, finalized directly
    check(`${name}: history replay (empty bubble) == renderMd(raw)`, replay.innerHTML === want);
    check(`${name}: the golden is not vacuous (markdown really rendered)`,
      /<(strong|table|pre|ul|ol|h2)\b/.test(want), JSON.stringify(want.slice(0, 60)));
  }
});

test("stream_md: final skips settle — an answer ending mid-syntax renders as renderMd(raw)", () => {
  for (const raw of ["Ends with a dangling **bold", "Ends on a header\n| a | b |", "Ends on a fence\n```py"]) {
    const md = bubble();
    streamRender(md, raw.slice(0, 10));
    streamRender(md, raw, true);
    check(`final of ${JSON.stringify(raw.slice(-12))} == renderMd(raw)`, md.innerHTML === freshHTML(raw));
  }
});
