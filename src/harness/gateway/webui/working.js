/* Working state (screen-reader announcements only).
   Sighted users get in-chat cues (thinking shimmer, tool pills); this keeps the
   equivalent for screen readers. Ref-counted so concurrent runs don't
   false-idle; a watchdog clears a stuck "on" if a run_done is ever missed
   (the daemon's primary failure mode). */
"use strict";

import { announce } from "./dom.js";

let activeRuns = 0;
let workWatchdog = null;
let workingAnnounced = false;

function updateWorking() {
  const on = activeRuns > 0;
  if (on && !workingAnnounced) { announce("Sidekick is working…"); workingAnnounced = true; }
  else if (!on && workingAnnounced) { announce("Ready"); workingAnnounced = false; }
  clearTimeout(workWatchdog);
  // 10 min, not 2: a local-lane turn queued behind another client runs well
  // past two minutes legitimately — but any stream traffic re-arms it fully.
  if (on) workWatchdog = setTimeout(() => { activeRuns = 0; updateWorking(); }, 600000);
}

export function workOn() { activeRuns++; updateWorking(); }
export function workOff() { activeRuns = Math.max(0, activeRuns - 1); updateWorking(); }

// Called on EVERY chat stream chunk (heartbeats included): bytes on the wire
// prove the run is alive, so a legitimately long turn never false-clears.
export function bumpWork() {
  if (activeRuns > 0) {
    clearTimeout(workWatchdog);
    workWatchdog = setTimeout(() => { activeRuns = 0; updateWorking(); }, 600000);
  }
}

/* Chat path: stop the indicator exactly once per request — on `turn_complete`
   (the answer has landed; auto-naming may still be running) or, as a fallback,
   when the stream ends/errors. Guarded so we never double-decrement and falsely
   idle a concurrent background run. */
let _chatWorkPending = false;
export function markChatWork() { _chatWorkPending = true; }
export function chatWorkPending() { return _chatWorkPending; }
export function stopChatWork() { if (_chatWorkPending) { _chatWorkPending = false; workOff(); } }
