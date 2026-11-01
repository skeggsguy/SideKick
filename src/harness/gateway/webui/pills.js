/* Tool pills (LOC-cap split from transcript.js): consecutive tool calls cluster
   into one wrapping row of small pills; a single shared drawer below the row
   shows the tapped pill's args/result. The drawer opens and closes ONLY on
   the user's tap (step 3b): a failure never opens it — the replay of every
   re-render (chat switch, resume heal, observe heal) re-ran that auto-open,
   re-opening drawers the user had closed and stealing the one they had open.
   Errors stay loud without it: filled red pill, "✗ failed", announce(), and
   the error-styled drawer when the user opens one. A long drawer flows at
   full height, clipped to a preview with "Show all" (BK-031, showall.js) —
   never an inner scroller; each open (or swap) starts clipped.

   The cluster state (pillRow, pendingPill, pillDetailSeq) is module-private
   HERE — the same contract transcript.js keeps for its own render state.
   Outside code breaks a cluster via breakPillRow()/resetPills(), never by
   writing the state. The neighbor closers startToolPill/addTruncatedChip
   need before opening (thinking.js's endThinking, transcript.js's
   finalizeAgentBubble) are INJECTED at evaluation by transcript.js via
   wirePills() — they used to be imports, and those imports were the
   pills.js ↔ thinking.js/transcript.js cycle legs (BK-019 criterion 5;
   same pattern, naming rationale, and pin as thinking.js's wireThinking). */

import { $, el, announce } from "./dom.js";
import { keepRead, readerOpened, holdShrink, settleSlack } from "./follow.js"; // D1: an open mid-run pauses following
import { makeClip, resetClip, syncClip } from "./showall.js";

// Injected neighbor-closers (BK-019 criterion 5) — see thinking.js for the
// pattern and the duplicate-declaration guard this naming gives.
let endThinking = null;
let finalizeAgentBubble = null;
export function wirePills(closers) {
  endThinking = closers.endThinking;
  finalizeAgentBubble = closers.finalizeAgentBubble;
}

const chatLog = $("#chat-log");
let pendingPill = null;   // tool pill awaiting its result
let pillRow = null;       // current cluster of consecutive tool pills
let pillDetailSeq = 0;    // unique ids for aria-controls

// Break the pill cluster WITHOUT closing the thinking segment — the segment
// boundary thinking.js's startThinking needs, so the next tool pill starts
// below the strip. State stays private here (same contract as closeSegments).
export function breakPillRow() { pillRow = null; }

// transcript.js's resetTranscript wipes the log: drop the cluster refs with
// it. The detail-id sequence NEVER resets — ids must stay unique across
// resets or aria-controls can point at a drawer from a dead render.
export function resetPills() { pendingPill = null; pillRow = null; }

function ensurePillRow() {
  if (pillRow) return pillRow;
  const row = el("div", "pill-row");
  row.setAttribute("role", "group");
  row.setAttribute("aria-label", "Tool calls");
  const detail = el("div", "pill-detail");
  detail.hidden = true;
  detail.id = `pill-detail-${++pillDetailSeq}`;
  const body = el("div", "pill-detail-body"); // the clipped content (BK-031)
  detail.append(body, makeClip(body));
  // Accordion (BK-030, see chat.css): the wrapper animates, detail.hidden is the state.
  const accIn = el("div", "acc-in");
  accIn.appendChild(detail);
  const acc = el("div", "acc pill-acc");
  acc.appendChild(accIn);
  row.appendChild(acc); // stays the last child; pills insert before it
  row._acc = acc;
  row._accIn = accIn;
  row._detail = detail;
  row._body = body;
  row._openPill = null;
  chatLog.appendChild(row);
  pillRow = row;
  return row;
}

function fillDrawer(row, pill) {
  const d = row._body;
  d.textContent = "";
  d.appendChild(el("div", "label", "arguments"));
  d.appendChild(el("pre", null, pill._args));      // textContent — untrusted
  if (pill._result != null) {
    d.appendChild(el("div", "label", "result"));
    d.appendChild(el("pre", null, pill._result));  // textContent — untrusted
    if (pill._truncNote) d.appendChild(el("div", "truncated", pill._truncNote));
  }
  row._detail.classList.toggle("error-open", pill.classList.contains("error"));
  syncClip(d);
}

// The pill's click handler is the ONLY caller (step 3b) — keep it that way.
function togglePill(row, pill) {
  if (row._openPill === pill) {
    row._openPill = null;
    // keepRead: the shrink must not clamp an unfollowed reader (BK-017) — and
    // it animates past that sync bracket, so reserve it first (BK-030).
    keepRead(() => {
      holdShrink(row._accIn.getBoundingClientRect().height);
      row._detail.hidden = true;
    });
    pill.setAttribute("aria-expanded", "false");
    return;
  }
  const swap = !!row._openPill;
  if (swap) row._openPill.setAttribute("aria-expanded", "false");
  row._openPill = pill;
  keepRead(() => { // swap may shrink — at once (no animation inside an open row)
    if (swap) holdShrink(row._accIn.getBoundingClientRect().height, 0);
    resetClip(row._body); // another pill (or a re-open): its preview first
    fillDrawer(row, pill);
    row._detail.hidden = false;
  });
  if (swap) settleSlack(); // trim the reserve to what the new content left short
  pill.setAttribute("aria-expanded", "true");
  readerOpened(); // D1 — opens only; a close never resumes following
}

export function startToolPill(tool, args) {
  endThinking();
  finalizeAgentBubble();
  const row = ensurePillRow();
  const pill = el("button", "pill running");
  pill.type = "button";
  pill.setAttribute("aria-expanded", "false");
  pill.setAttribute("aria-controls", row._detail.id);
  const face = el("span", "pill-face");
  const dot = el("span", "pill-dot");
  dot.setAttribute("aria-hidden", "true");
  face.appendChild(dot);
  face.appendChild(el("span", "pill-name", tool));
  const status = el("span", "pill-status", "");
  face.appendChild(status);
  pill.appendChild(face);
  let pretty;
  try { pretty = JSON.stringify(args, null, 2); } catch (_) { pretty = String(args); }
  pill._args = pretty;
  pill._result = null;
  pill._truncNote = "";
  pill._toolName = tool;
  pill._status = status;
  pill.addEventListener("click", () => togglePill(row, pill));
  row.insertBefore(pill, row._acc);
  pendingPill = pill;
  return pill; // the pill this call CREATED — replay finishes THIS one explicitly
}

// pill (optional): an explicitly finished pill. Session replay always passes
// the pill it just created, so a live event can never repoint the global
// pendingPill and make the replay finish the WRONG pill.
export function finishToolPill(isError, text, chars, pill) {
  const live = !pill; // replay always passes its pill — history isn't news
  pill = pill || pendingPill;
  if (!pill) return; // result with no matching call (e.g. reconnect) — skip
  const row = pill.parentElement;
  pill.classList.remove("running");
  pill.classList.add(isError ? "error" : "done");
  pill._status.textContent = isError ? "✗ failed" : "✓";
  const shown = (text || "(no output)").slice(0, 4000);
  pill._result = shown;
  // The live digest is capped server-side; show that detail was cut.
  if (chars && chars > shown.length) {
    pill._truncNote = `… (truncated, ${chars - shown.length} more chars)`;
  }
  if (isError && live) announce(`Tool ${pill._toolName} failed`);
  // Never opens the drawer (header); the user is watching this one — refresh it.
  if (row && row._openPill === pill) fillDrawer(row, pill);
  if (pill === pendingPill) pendingPill = null; // only the global holder clears it
}

// The model hit HARNESS_MAX_TOKENS mid-response — never leave that silent.
export function addTruncatedChip() {
  endThinking();
  finalizeAgentBubble();
  pillRow = null;
  chatLog.appendChild(el("div", "truncated-chip",
    "⚠ Response truncated — hit the output-token limit (lower the effort, or raise HARNESS_MAX_TOKENS, to allow longer answers)"));
}
