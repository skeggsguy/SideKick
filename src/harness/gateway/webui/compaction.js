/* compaction.js — the transcript's compaction seam (plan
   compaction-checkpoint-bookmark, Steps 4–5).

   The live `compaction` SSE frame (chat_run.py's on_compaction wiring; the
   same vocabulary as the run_events kind='compaction' row) says the
   conversation middle was just summarized into the frozen checkpoint. The
   seam is a blue var(--accent) rule. TWO painters, ONE node:

   - LIVE (mid-turn): paintSeam places it above the current turn's first
     row — the last user row painted (the live send appends it at submit;
     a join renders it from the persisted half). An approximation: the
     frame's anchor_idx is a STORE idx and a live DOM has no store indexes.
   - RELOAD (Step 5): /messages carries `compactions` (anchor_idx in the
     STORE idx space every message row's `idx` names). renderSession arms
     the list from the payload (reloadSeams), feeds the persisted-row loop
     each row's idx (dropSeamsTo → the seam lands before that row's first
     node; a row the renderer skips still advances the cursor, so the seam
     sits where that row WOULD have been), and the live join flushes what
     is left above the tail (flushSeams, replay.js). All three calls are
     synchronous inside renderSession's atomic swap, so the pending list
     never survives an await.

   ONE seam per compaction: a mid-turn reload hands the same compaction to
   BOTH painters — it is in `compactions` AND in the ring tail the join
   paints (join/flush double-paint is the shape here). The dedupe is the
   seam's own data attributes: data-anchor = anchor_idx (present on every
   Step-3+ frame and row), data-cp = checkpoint_id ("" when the checkpoint
   write failed — anchor still holds). Both are compared by iterating the
   on-screen seams' datasets — never interpolated into a selector (XSS
   rule: no server value reaches markup construction).

   Never a fake message row — a marker row would leak into the model
   context. The pulsing "Compacting…" phase rides the status row
   (status.js applyEvent on `compaction_start`, sharing the row's existing
   .cs-dot pulse); nothing here paints it, and nothing here cleans it up:
   an orphaned start is closed by the run's terminal frame, which every
   sink already routes through statusStop(). */

import { $, el } from "./dom.js";

// Shared node factory: both painters stamp the same identity attributes,
// so either one can dedupe the other.
function seamNode(info) {
  const seam = el("div", "compaction-seam");
  seam.setAttribute("role", "separator");
  seam.setAttribute("aria-label", "Conversation compacted — earlier turns summarized");
  if (info && info.checkpoint_id) seam.dataset.cp = String(info.checkpoint_id);
  if (info && Number.isInteger(info.anchor_idx)) {
    seam.dataset.anchor = String(info.anchor_idx);
  }
  return seam;
}

// This compaction already on screen? Anchor first, id second; an info
// with neither (a pre-Step-3 frame — the reload path filters those out
// server-side, so it can only come from a live sink, and there is no
// reload seam to double) never dedupes.
function alreadyPainted(log, info) {
  const anchor = info && Number.isInteger(info.anchor_idx) ? String(info.anchor_idx) : null;
  const cp = info && info.checkpoint_id ? String(info.checkpoint_id) : null;
  if (!anchor && !cp) return false;
  for (const s of log.querySelectorAll(".compaction-seam")) {
    if (anchor && s.dataset.anchor === anchor) return true;
    if (cp && s.dataset.cp === cp) return true;
  }
  return false;
}

// Paint the LIVE seam for one `compaction` frame. Called from transcript.js's
// paintStream (closeSegments() first) — the painter every source shares, so
// the live sink, the /live gap-fill and the /messages live join all land the
// seam through here (replay.js's RENDERED + seq dedupe, plus the identity
// dedupe above for the reload-join double offer).
export function paintSeam(info) {
  const log = $("#chat-log");
  if (!log || alreadyPainted(log, info)) return;
  // Fallback-append only for the pathological case of a compaction with no
  // user row on screen.
  const users = log.querySelectorAll(".msg.user");
  const turnStart = users.length ? users[users.length - 1] : null;
  if (turnStart && turnStart.parentElement === log) log.insertBefore(seamNode(info), turnStart);
  else log.appendChild(seamNode(info));
}

/* ---- reload seams (Step 5) ---- armed/drained inside ONE synchronous
   renderSession swap: arm before the persisted-row loop, drop per row,
   flush at the join. An unanchorable entry (no integer anchor_idx — a
   mocked or pre-Step-3 payload) is filtered at arm, mirroring the route. */

let _pending = []; // sorted by anchor_idx; drained shift-by-shift

export function reloadSeams(data) {
  const list = data && !Array.isArray(data) && Array.isArray(data.compactions)
    ? data.compactions : [];
  _pending = list
    .filter((c) => c && Number.isInteger(c.anchor_idx))
    .sort((a, b) => a.anchor_idx - b.anchor_idx);
}

// Top of each persisted-row iteration: every seam anchored at or before
// this row's STORE idx paints now — i.e. before this row's first node.
// (idx undefined on an older/mocked row: nothing qualifies, the flush
// parks the rest above the tail — same graceful degradation as position.)
export function dropSeamsTo(idx) {
  while (_pending.length && _pending[0].anchor_idx <= idx) append(_pending.shift());
}

// The join's front step (replay.js consumeLiveJoin): seams anchored past
// the last persisted row sit above the live tail — the race snapshot where
// the compaction event postdates the transcript read.
export function flushSeams() {
  while (_pending.length) append(_pending.shift());
}

function append(seamInfo) {
  const log = $("#chat-log");
  if (log) log.appendChild(seamNode(seamInfo));
}
