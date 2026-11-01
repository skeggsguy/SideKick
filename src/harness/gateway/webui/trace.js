/* trace.js — the ?recoverdebug=1 capture's formatter (BK-023). PURE logic:
   imports nothing, touches no DOM, starts no timer — the whole reason it is a
   module: app.js is at the 420-line web-UI cap, and this is the tier the
   offline jsunit suite can drive (tests/fixtures/jsunit — DOM-free, rAF-free).
   app.js's 2 s pump drains window.__sk._thinkLog and pushes each batch here;
   the capture owns the accumulated text, so a resetTranscript() wipe of the
   <pre> refills from this state (the pump re-arms via armRecoverDebug and
   re-renders cap.text() every tick).

   Entry shape is the tlog helpers' [name, state, stack|null, ms] (BK-023
   Step 1): the stamp is the EVENT time, taken at push — the pump drains every
   2 s, so only event time tells a paced stream from a catch-up burst.

   Noise rule (BK-022's device trace: ~85% of entries were the per-chunk no-op
   endThinking from paintStream's delta branch): a maximal run of consecutive
   no-op endThinking entries (state.hadBlock falsy) with identical state AND
   identical rendered frames collapses to ONE line carrying `×count` and the
   first/last stamp span. Uniform: even a run of one renders with `×1` — a
   no-op close never renders verbatim, so its line shape never depends on
   neighbors. Everything else — hadBlock:true closes (criterion: verbatim),
   appendThinking zombie-spawns (hadBlock:false is the SIGNAL there), the
   recovery transitions — renders one stamped line each, mirroring the
   headless probe's LOG-* dump plus the new stamp. */
"use strict";

// HH:MM:SS.mmm, local time (a trace is read against wall-clock events).
// ms is the ONLY tz-free component, so the suite asserts the exact ms digits
// and the HH:MM:SS SHAPE, never an absolute local clock.
const stamp = (ms) => {
  if (typeof ms !== "number" || !Number.isFinite(ms)) return "--:--:--.---"; // unsanitized entry
  const d = new Date(ms);
  const p = (n, w) => String(n).padStart(w, "0");
  return `${p(d.getHours(), 2)}:${p(d.getMinutes(), 2)}:${p(d.getSeconds(), 2)}.${p(d.getMilliseconds(), 3)}`;
};

const framesOf = (e) =>
  e[2]
    ? " < " + String(e[2]).split("\n").map((s) => s.trim())
        // V8 frames are "at fn (url)"; JavaScriptCore/Safari (the device this
        // capture exists for) emits "fn@url:line:col" — accept both, or the
        // phone would show names with no caller.
        .filter((s) => (s.startsWith("at ") || s.includes("@")) && !s.includes("tlog"))
        .slice(0, 3).join(" < ")
    : "";

// One verbatim capture line: `HH:MM:SS.mmm name {state} < frames`.
export function renderEntry(e) {
  return `${stamp(e[3])} ${e[0]} ${JSON.stringify(e[1])}${framesOf(e)}`;
}

// The collapse key's subject: the per-chunk no-op close (logged by endThinking
// BEFORE its null-guard, so every streamed delta pushes one). hadBlock:true
// closes are real transitions — they never join a run.
const isNoopClose = (e) => e[0] === "endThinking" && !!e[1] && !e[1].hadBlock;

export function createCapture() {
  const lines = [];
  let open = null; // open collapsed run {key, entry, count, first, last}; its line is always lines' last
  const renderOpen = () =>
    `${stamp(open.first)}-${stamp(open.last)} ${open.entry[0]} ${JSON.stringify(open.entry[1])}` +
    ` ×${open.count}${framesOf(open.entry)}`;
  return {
    // Feed one drained batch (the pump's splice(0) result). A run may straddle
    // drains — merging rewrites the open line's count/span in place, so a
    // 40 s paced stream collapsing across twenty drains still yields ONE line.
    push(entries) {
      for (const e of entries) {
        if (!isNoopClose(e)) {
          open = null;
          lines.push(renderEntry(e));
          continue;
        }
        const key = `${e[0]} ${JSON.stringify(e[1])}${framesOf(e)}`;
        if (open && open.key === key) {
          open.count += 1;
          open.last = e[3]; // arrival order: the stamp IS push order (tlog stamps at push)
          lines[lines.length - 1] = renderOpen();
          continue;
        }
        open = { key, entry: e, count: 1, first: e[3], last: e[3] };
        lines.push(renderOpen());
      }
    },
    text() {
      return lines.length ? lines.join("\n") + "\n" : "";
    },
  };
}
