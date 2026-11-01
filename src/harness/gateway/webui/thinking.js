/* Thinking strips, split out of transcript.js (LOC cap): one quasi-hidden
   element per thinking SEGMENT (interleaved thinking can produce several per
   turn). D2 (BK-030): the strip is ONE fixed-height row throughout — while
   streaming a shimmer label + a skeleton preview (no raw text), at segment
   end the same row reads "✦ Thought for Ns" + a one-line preview, so nothing
   collapses. The header toggles the body (an animated accordion) the whole
   time: live while streaming, complete after. Thinking is model output →
   textContent ONLY, never markdown.

   thinkBlock/thinkRaf are module-private here; transcript.js drops them via
   resetThinking() when it wipes #chat-log. The segment start below closes
   its neighbors (transcript.js's finalizeAgentBubble, pills.js's
   breakPillRow) through module bindings that transcript.js INJECTS at
   evaluation (wireThinking, below) — those calls used to be imports, and
   they were the thinking.js ↔ transcript.js/pills.js cycle that BK-019
   criterion 5 broke; transcript.js is the only module that imports all
   three segment modules, so it is where the wiring lives. */
"use strict";

import { $, el } from "./dom.js";
// BK-017: keepRead around every height change that may shrink (the segment
// close no longer does — D2 — but it stays bracketed); BK-030: holdShrink
// before an ANIMATED close, whose frames keepRead's sync bracket can't reach.
// Import, never redeclare (an unimported sibling call
// throws AT THE CALL: test_webui_modules_import_sibling_exports_they_call).
import { keepRead, readerOpened, holdShrink } from "./follow.js"; // + D1: an open mid-run pauses following
import { makeClip, resetClip, syncClip } from "./showall.js"; // BK-031: a long body clips, never scrolls

// Injected neighbor-closers (BK-019 criterion 5): the replay.js precedent
// ("imports nothing from this file (the painter and dispatcher are
// injected), so there is no cycle"). transcript.js calls wireThinking() in
// its module body, before any segment can paint. The bindings are named
// EXACTLY like the real exports on purpose: re-adding the import collides
// with the let (duplicate declaration, SyntaxError at parse time) and can
// never silently shadow it (the extract-and-re-export gotcha). Pin:
// test_no_import_cycle_between_thinking_and_transcript (test_conventions).
let finalizeAgentBubble = null;
let breakPillRow = null;
export function wireThinking(closers) {
  finalizeAgentBubble = closers.finalizeAgentBubble;
  breakPillRow = closers.breakPillRow;
}

const chatLog = $("#chat-log");
let thinkBlock = null;    // live thinking segment being streamed into
let thinkRaf = 0;
const THINK_RAW_CAP = 20000;

// Passive recovery diagnostic (thinking-block-vanish): a state-transition log
// the probe / Safari console arms with `window.__sk._thinkLog = []`. Zero
// production overhead — the array does not exist, so nothing is built or
// pushed. NEVER mutates render state; it only reads it (that state is
// module-private: transcript.js header). Same local helper lives in transcript.js and events.js.
const tlog = (name, state, withStack) => {
  const L = window.__sk && window.__sk._thinkLog;
  if (!L) return;
  // BK-023: entry = [name, state, stack|null, event-time ms]. Stamped AT PUSH —
  // the ?recoverdebug pump drains every 2 s, so drain-time stamps cannot tell a
  // paced stream from a catch-up burst. Stack stays at index 2 (probes read
  // positionally); the null pad keeps the stamp always e[3].
  L.push([name, state, withStack ? new Error().stack : null, Date.now()]);
};

export function startThinking() {
  tlog("startThinking", { hadPrev: !!thinkBlock });
  finalizeAgentBubble();
  breakPillRow(); // thinking breaks a tool cluster
  const block = el("div", "think streaming");
  const head = el("button", "think-head");
  head.type = "button";
  head.setAttribute("aria-expanded", "false");
  head.appendChild(el("span", "think-glyph", "✦"));
  const label = el("span", "think-label", "Thinking…");
  head.appendChild(label);
  // Skeleton preview (D2): decoration only — the label carries the state.
  const skel = el("span", "think-skel");
  skel.setAttribute("aria-hidden", "true");
  skel.append(el("span"), el("span"));
  head.appendChild(skel);
  const preview = el("span", "think-preview", "");
  head.appendChild(preview);
  head.appendChild(el("span", "chev", "▾"));
  // Accordion (BK-030): .acc animates its grid row; body.hidden is the state.
  // The text lives in .think-text, clipped to a preview with "Show all" (BK-031).
  const body = el("div", "think-body");
  body.hidden = true;
  const text = el("div", "think-text");
  body.append(text, makeClip(text));
  const accIn = el("div", "acc-in");
  accIn.appendChild(body);
  const acc = el("div", "acc");
  acc.appendChild(accIn);
  head.addEventListener("click", () => {
    const open = body.hidden;
    keepRead(() => { // a close shrinks: must not clamp an unfollowed reader (BK-017)
      // ...and it shrinks over ~200 ms, past this bracket: reserve it (BK-030).
      if (!open) holdShrink(accIn.getBoundingClientRect().height);
      block.classList.toggle("open", open);
      if (open) { resetClip(text); text.textContent = block._raw; } // lazy fill; appendThinking keeps it live
      body.hidden = !open;
      if (open) syncClip(text);
    });
    head.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) readerOpened(); // D1
  });
  block.append(head, acc);
  block._raw = "";
  block._t0 = performance.now();
  block._label = label;
  block._preview = preview;
  block._skel = skel;
  block._text = text;
  chatLog.appendChild(block);
  thinkBlock = block;
}

export function appendThinking(text) {
  // hadBlock:false ⇒ this call zombie-spawns a strip (buffered delta with no
  // live segment) — exactly the transition the recovery diagnostic is for.
  tlog("appendThinking", { hadBlock: !!thinkBlock });
  if (!thinkBlock) startThinking(); // mid-run join: no start event seen
  const block = thinkBlock;
  if (block._raw.length < THINK_RAW_CAP) {
    block._raw += text;
    if (block._raw.length >= THINK_RAW_CAP) block._raw += "\n… (thinking truncated for display)";
  }
  // Only an OPEN body shows the text (D2: no live tail); a closed one fills on open.
  if (!thinkRaf && block.classList.contains("open")) {
    thinkRaf = requestAnimationFrame(() => { // capture block — never the moving global
      thinkRaf = 0;
      if (block.classList.contains("open")) { block._text.textContent = block._raw; syncClip(block._text); }
    });
  }
}

// tlog STACK strings are test data: pwa_resume probe F asserts /startToolPill/ and
// /renderSession/ INSIDE this function's logged stack — moving or hoisting a call reddens it.
export function endThinking(opts) {
  // Logged BEFORE the null-guard so a no-op close still names its caller; the
  // stack distinguishes replay-loop closes from buffered-flush closes. Does
  // NOT touch when thinkBlock is nulled below — that timing is load-bearing.
  tlog("endThinking", { hadBlock: !!thinkBlock, replay: !!(opts && opts.replay) }, true);
  const block = thinkBlock;
  if (!block) return;
  thinkBlock = null;
  if (thinkRaf) { cancelAnimationFrame(thinkRaf); thinkRaf = 0; }
  keepRead(() => { // D2: same-height row swap (skeleton → preview) — bracketed regardless
    block.classList.remove("streaming");
    block.classList.add("done");
    const secs = Math.round((performance.now() - block._t0) / 1000);
    block._label.textContent = opts && opts.replay ? "Thoughts"
      : secs < 1 ? "Thought briefly" : `Thought for ${secs}s`;
    // Preview is written ONCE, at segment end — updating it while streaming
    // would spam the aria-live region.
    block._preview.textContent = (block._raw.split("\n").find((l) => l.trim()) || "").trim();
    block._skel.remove();
    if (block.classList.contains("open")) { block._text.textContent = block._raw; syncClip(block._text); }
  });
}

// resetTranscript() has wiped #chat-log — drop the segment refs with it so a
// detached block is never painted into again.
export function resetThinking() {
  if (thinkRaf) { cancelAnimationFrame(thinkRaf); thinkRaf = 0; }
  thinkBlock = null;
}
