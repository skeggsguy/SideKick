/* resync.js — the run lifecycle's VERDICT half: T27 resyncVerdict (the ONE
   resync entry point) and the transitions that ask it — T17 runDoneHeal, T20
   visibleResume, T21 watchdogTick — plus phaseNow, the "resyncing" projection
   of the verdicts in flight. Split out of runstate.js at its 420-line cap
   (BK-034) as a verbatim move; the design notes are runstate.js's header and
   scratch/plans/bk-019-design.md §3.1.

   Ownership is unchanged: runstate.js stays the ONE writer of the lifecycle
   fields (state.js). This module READS ui and reaches every write through a
   runstate export (abandonRun, noteRunTerminal, bgLiveClear, bgLiveGapAll,
   observeLive) —
   the write-ownership pin scans it like any other non-runstate module.
   DAG: resync → runstate → state; runstate never imports this module. Same
   purity as runstate: replay's arithmetic and the /live fetch arrive as the
   injected `deps` bag, renderers as returned effect tokens. */
import { ui } from "./state.js";
import {
  abandonRun, bgLiveClear, bgLiveGapAll, bgRunStart, derivePhase, noteRunTerminal, observeLive, pendingFor,
} from "./runstate.js";

// Transient, deliberately NOT on ui: the ids with a resyncVerdict in flight —
// it NAMES the "resyncing" phase and gates nothing (the contract allows
// concurrent verdicts exactly as today's resume + watchdog + run_done trio).
const verdictsInFlight = new Set();

// A projection for the tier / ?recoverdebug, never a guard input.
export function phaseNow(id) {
  if (verdictsInFlight.has(id)) return "resyncing";
  return derivePhase(ui.resync, ui.liveTurn);
}

// T17 run_done for the PENDING run: the event itself is the terminal verdict
// — run_done can NEVER be served by /live, so no /live ask (knownTerminal).
// The router keeps tlog("run_done:refetch", …) on the on-screen case (D8).
export async function runDoneHeal(ev, deps) {
  if (!pendingFor(ev.session_id)) return null;
  noteRunTerminal(ev.session_id, ev.run_id);
  bgLiveClear(ev.session_id); // T29 — this heal re-renders anyway
  const r = await resyncVerdict(ev.session_id, true, deps);
  return [...r.effects, ["pickerReload"]]; // reload AFTER the heal
}

// T20 back-to-visible: ONE verdict per subject — an armed resync or a primed
// screen (watermark/provisional slot), plus a screen whose T28 run is in flight.
// Socket reconnect + picker freshness stay in events.js (not run-lifecycle).
export async function visibleResume(deps) {
  bgLiveGapAll(); // hidden ⇒ the socket was closed
  const scr = ui.sessionId, ids = ui.resync ? [ui.resync.id] : [];
  if (!ids.length && scr && (deps.storedWatermark(scr) != null || deps.isProvisionalSlot(scr))) ids.push(scr);
  if (ui.bgLive[scr] && !ids.includes(scr)) ids.push(scr);
  const rs = [];
  for (const id of ids) rs.push(await resyncVerdict(id, false, deps));
  return rs.length ? { verdict: rs[0].verdict, effects: rs.flatMap((r) => r.effects) } : null;
}

// T21 watchdog tick: returns T27's verdict; ALL timer state (tries, the
// netDetach retry POST, the clear on terminal/missing) stays with the timer
// owner in events.js (§10.4) — timer state inside a transition would make the
// DOM-free tier unable to drive it.
export async function watchdogTick(deps) {
  const pend = ui.resync;
  if (!pend || pend.seen) return null;
  return resyncVerdict(pend.id, false, deps);
}

// T32 observe-watch tick (BK-034): the observed run (another device's) gets
// T27's verdict — gap-fill while it lives, the persisted heal + Send once it
// ended (abandonRun clears ui.observed through T22). The timer (cadence, the
// hidden skip, the one-in-flight guard) is events.js's, like T21's. Null =
// nothing observed on this screen: the timer stops.
export async function observeTick(deps) {
  const o = ui.observed;
  if (!o || o.id !== ui.sessionId) return null;
  return resyncVerdict(o.id, false, deps);
}

// T34 (BK-041 G4) the same join's ROW: /messages' `live` (a chat run) or
// `bg_live` (cron/watch) carries {started_ms, phase} — the row opens at the
// run's current label and real elapsed time (statusJoin leaves a running row
// alone). bg_live also sets/clears the T28 mark through runstate's writers,
// so a bg run_done missed while hidden still heals; undefined = unknown. An
// existing mark is KEPT as-is: its gap flag still owes run_done's heal.
export function rowJoin(id, live, bg) {
  if (id == null || id !== ui.sessionId || live === undefined) return null;
  if (bg && !ui.bgLive[id]) bgRunStart({ session_id: id });
  else if (bg === null) bgLiveClear(id);
  return live || bg ? [["statusJoin", live || bg]] : [];
}

// ── T27 resyncVerdict: the ONE entry point, six named branches (§3.1) ─────
// deps: { fetchLive, joinReason, storedWatermark, isProvisionalSlot,
//         raiseWatermark, isReplaying }. Returns { verdict, effects } —
// verdicts kept for the watchdog's timer: "offline" (ask again) · "missing"
// (pre-/live agentd: STOP probing; never store-render — that server has no
// live join, the lossy path) · "stale" (recovery moved on) · "live" (gap-filled
// or cold-joined) · "terminal" (the persisted heal ran). The only question is
// deps.fetchLive — /live's `active` is the ONLY liveness predicate (no
// sessions-page consult anywhere). Mid-call, the only sanctioned writes are
// raiseWatermark and the paint effects into the EXISTING DOM; a store render
// appears solely in verdictHeal / verdictColdJoin (the one-fetch join).
export async function resyncVerdict(id, knownTerminal = false, deps) {
  verdictsInFlight.add(id);
  try {
    if (!knownTerminal) {
      const res = await deps.fetchLive(id, deps.storedWatermark(id));
      if (!res) return { verdict: "offline", effects: [] };
      if (res.missing) return { verdict: "missing", effects: [] };
      // Still whom we asked about? (the resync slot moved, or the chat closed)
      // The cold-open subject (this screen, primed slot or T28 mark) is ours.
      if (!pendingFor(id)
          && !(ui.sessionId === id && (deps.storedWatermark(id) != null
               || deps.isProvisionalSlot(id) || ui.bgLive[id]))) {
        return { verdict: "stale", effects: [] };
      }
      if (res.active === true) {
        const why = deps.joinReason(id, res.events, res.seq);
        if (why) {
          // verdictColdJoin: displacement proves the slot's run ended; the
          // repair is the ONE sanctioned messages+live join, whose render
          // carries the stale-buffer discard when the batch's run is terminal.
          const slot = ui.liveSeq[id];
          if (slot && slot.runId != null) noteRunTerminal(id, slot.runId);
          const effects = pendingFor(id) ? abandonRun(id) : []; // its heal is THIS join
          if (ui.sessionId === id && !deps.isReplaying()) {
            effects.push(["rerenderCold", id, why === "terminal"]);
          }
          return { verdict: "live", effects };
        }
        // verdictGapFill: fill the ring tail into the existing DOM, deduped by
        // painted intervals. The `gap`/oldest_seq suffix start is the SERVER's
        // decision — no client gap flag, no seq arithmetic; the watermark
        // rides the response's own `seq`, never its `since`.
        deps.raiseWatermark(id, res.seq);
        const effects = [];
        if (ui.sessionId === id && !deps.isReplaying()) effects.push(["gapFill", id, res.events]);
        // BK-041 G5: the run's CURRENT phase — an observer's row tracks it per tick.
        if (ui.sessionId === id && res.phase) effects.push(["statusEvent", res.phase]);
        // BK-036 backstop (T33): the screen's run is live — if no source holds
        // Stop for it (own stream, resync, observation), arm one; the slot
        // vouched for the batch, so its run id is the run's.
        effects.push(...observeLive(id, ui.liveSeq[id]?.runId ?? null));
        return { verdict: "live", effects };
      }
    }
    // verdictHeal — active !== true, or knownTerminal (run_done delivered the
    // server's own word). The PERSISTED refetch, POST-run heal only (bar a T28
    // run /live can't see — T18 heals it again). Cold-open with nothing armed:
    // abandonRun's gate still hands the composer back for a turn the server
    // forgot — but never over a live run's row/Stop (D4).
    const effects = abandonRun(id);
    if (ui.sessionId === id) effects.push(["rerenderHeal", id]); // the heal re-renders THIS screen
    return { verdict: "terminal", effects };
  } finally {
    verdictsInFlight.delete(id);
  }
}
