/* Cross-module mutable state. ES-module bindings are read-only for importers,
   so shared mutables live as properties on this one object. */

export const ui = {
  // The session currently on screen (null = empty new-chat view).
  sessionId: null,
  // Set by "Fire now" so the next run_start may auto-open its session.
  expecting: false,
  // THE recovery state (Step 7; was a TWO-flag pair that had to be armed and
  // torn down in pairs — and drifted apart doing it): a chat run whose stream
  // DIED but whose server-side run survived (PWA suspension kills the socket;
  // the server detaches, never cancels). { id, seen } — id is the session to
  // resync, seen flips on the first rerouted /api/events event (the status
  // row's one-time "Catching up…"). One slot suffices — the button is in stop
  // mode while a stream is live. BK-019: every verdict (run_done heal,
  // visibility resume, detach watchdog) runs through runstate.js's
  // resyncVerdict(), the ONE entry point: it GETS this state and CLEARS it
  // (abandonRun); nothing else writes this field outside runstate.js.
  resync: null,
  // The session a Stop targets — the live stream's, updated on `session`.
  // Composer state, NOT recovery: it exists while the stream is healthy.
  // BK-019 D3: `detached` is the stream's for-good screen-latch (undefined ≡
  // false); it rides this object so the latch dies with the turn, not in a
  // submit closure.
  liveTurn: null,
  // Per-session dedupe slots (replay.js arithmetic): [sessionId] =
  // { runId, water, painted: [[lo,hi],…] } — seq space is RUN-LOCAL, so the
  // watermark belongs to its run_id. runId null = PROVISIONAL (empty-tail
  // join): holds no marks and no watermark. BK-019 D6: the MAPS (keys, entry
  // create/delete, the terminal stamp below) are owned by runstate.js's
  // storage trio; slot CONTENT arithmetic stays in replay.js.
  liveSeq: {},
  // [sessionId] = runId of the last run known OVER on that session (seen via
  // run_done or displaced by a successor). Guards the late events of the run
  // that just ended (a stopped detached run can publish `title` after
  // run_done): they may relabel the picker but never touch a newer slot.
  liveDone: {},
  // BK-027: [sessionId] = { gap } for a background (cron/watcher) run seen at
  // run_start and not yet at run_done — written ONLY by runstate.js (T28 sets,
  // T29 = the delete in T17–T19). The page closes /api/events while hidden, so
  // such a run's run_done can land unseen and freeze its chat; at resume
  // (T20) every mark gets `gap` (its live paint has a hole) and the SCREEN's
  // mark is a resume subject. Its verdict is T27's: /live knows no background
  // run, so it answers inactive and the heal is the atomic store render —
  // offline asks again next resume; a live CHAT run on that session gap-fills
  // as ever. If the run is in fact still going, that render lacks the
  // in-flight call's head (decisions 2026-10-17), so a gapped run's run_done
  // on screen heals once more (T18). A mark whose run_done was missed is
  // never cleared: it costs a /live + /messages per resume while that chat is
  // on screen, until a run_done for the session or a reload.
  bgLive: {},
  // BK-034: the THIRD Stop source — { id, runId } for the on-screen
  // session's live CHAT run that this page did NOT start (another device's,
  // or its own from before a reload); runId null = an empty-tail join named
  // none yet. Armed from the /messages live join (T31) or a rerouted run
  // event (T06); cleared by that run's terminal (stopped/error/run_done/the
  // T27 heal, T22) or by the screen leaving the session (T30). While armed
  // the composer reads Stop and stopTarget() falls back to it; the observe
  // watch in events.js polls T27 for it (an ATTACHED sender's run publishes
  // nothing on /api/events — one sink per event). Written ONLY by runstate.js.
  observed: null,
};

let expectingTimer = null;
export function expectRun() {
  ui.expecting = true;
  clearTimeout(expectingTimer);
  expectingTimer = setTimeout(() => { ui.expecting = false; }, 20000);
}
