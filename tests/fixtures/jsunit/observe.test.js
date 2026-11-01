/* observe.test.js — BK-034 "Stop on every device" (plan frontend-smoothness
   step 2b): the THIRD Stop source. ui.observed = the on-screen session's live
   chat run this page did NOT start (another device's). Before BK-034 Stop
   armed only on this page's own send (T01) or its own resync — both
   page-local — so a phone that opened a reply the iPad started painted it
   and never showed Stop.

   Transitions under test (runstate.js unless noted):
     T30 observeLeave ....... the screen leaves the session → clear (+ Send)
     T31 observeJoin ........ the /messages live join → arm (Stop + watch)
     T06 paintRerouted ...... a rerouted run event for the screen arms too
     T10/T11/T18/T22 ........ every terminal clears it
     T09/T10/T11/T13/T14 .... the OWN-stream terminals keep Stop while armed
     T32 observeTick ........ (resync.js) the watch: T27 on the observed run
     T20 visibleResume ...... (resync.js) hidden mid-run → the one verdict
   Fixtures arm ui directly (the project's test seam); deps are replay.js's
   real arithmetic plus a scripted /live. DOM-free, rAF-free. */

import { ui } from "./state.js";
import {
  stopTarget, stopRequest, observeLeave, observeJoin, paintRerouted,
  runStopped, runError, runFinish, runDoneWatched, abandonRun, sendFailed,
  sendRejected, streamEndSilent, noteRunTerminal, applyEffects,
} from "./runstate.js";
import { observeTick, rowJoin, visibleResume } from "./resync.js";
import {
  alreadyPainted, markPainted, joinReason, raiseWatermark, storedWatermark,
  isProvisionalSlot,
} from "./replay.js";
import { test, check, eq } from "./runner.js";

const S = "obs-sess";
const T = "other-sess";
const R1 = "run-obs-1";
const R2 = "run-obs-2";
const ev = (type, seq, runId = R1, extra) =>
  ({ type, seq, run_id: runId, session_id: S, ...(extra || {}) });
const delta = (seq, runId = R1) => ev("delta", seq, runId, { text: "w" });
// The /messages `live` block (gateway/live.py persisted_tail_projection).
const liveOf = (events, seq, since = 0) => ({ since, seq, oldest_seq: 1, gap: false, events });

const deps = (over) => ({
  isReplaying: () => false, alreadyPainted, markPainted, joinReason,
  raiseWatermark, storedWatermark, isProvisionalSlot, ...(over || {}),
});
const fetcher = (res) => {
  const calls = [];
  return { calls, fetchLive: async (id, since) => { calls.push([id, since]); return res; } };
};
const neverAsk = async () => { throw new Error("must not ask /live here"); };
const names = (effects) => (effects || []).map((e) => e[0]);
const composerOf = (effects) => (effects || []).filter((e) => e[0] === "composer").map((e) => e[1]);

// An observer page: B opened S while the iPad's run was mid-answer. The join
// primed the slot exactly as replay.consumeLiveJoin does (attributed run,
// marks through the tail, watermark = live.seq).
function observing(runId = R1, seq = 3) {
  ui.sessionId = S;
  for (let n = 1; n <= seq; n++) markPainted(S, delta(n, runId));
  return observeJoin(S, liveOf([delta(seq, runId)], seq, seq - 1));
}

test("tier: state.js declares ui.observed (BK-034's third Stop source) and the runner resets it", () => {
  check("observed: a declared key on the shared object", Object.prototype.hasOwnProperty.call(ui, "observed"));
  eq("observed: starts null (nothing observed on a fresh page)", ui.observed, null);
});

test("arm (T31): the live join of a run this page didn't start arms Stop, ONE render", () => {
  const fx = observing();
  eq("arm: the join returns composer→stop + the watch, nothing else", fx, [["composer", "stop"], ["observeWatch"]]);
  eq("arm: observed names the session and the tail's run", ui.observed, { id: S, runId: R1 });
  eq("arm: stopTarget falls back to the observed run", stopTarget(), S);
  eq("arm: Stop posts the session's /stop (the server's stop is keyed by session)", stopRequest(), [["netStop", S]]);
});

test("arm (T31): an empty-tail join arms with no run named; the first rerouted event names it, silently", () => {
  ui.sessionId = S;
  eq("empty tail: still a live run ⇒ Stop", observeJoin(S, liveOf([], 0)), [["composer", "stop"], ["observeWatch"]]);
  eq("empty tail: runId null (the payload named none)", ui.observed, { id: S, runId: null });
  const fx = paintRerouted(delta(1), deps());
  eq("named later: the run is adopted ...", ui.observed, { id: S, runId: R1 });
  eq("named later: ... with NO second composer/watch effect (setSendMode per delta would thrash layout)",
    names(fx).filter((n) => n === "composer" || n === "observeWatch"), []);
});

test("no arm (T31): no live run, a fetch that failed, an off-screen join, an already-ended run", () => {
  ui.sessionId = S;
  eq("live null: nothing to observe ⇒ no effects", observeJoin(S, null), null);
  eq("live undefined (fetch failed / old server): unknown ⇒ no change", observeJoin(S, undefined), null);
  eq("off-screen: a join for a session no longer on screen arms nothing", observeJoin(T, liveOf([delta(1)], 1)), null);
  noteRunTerminal(S, R1);
  eq("terminal: run_done already passed this run ⇒ no arm", observeJoin(S, liveOf([delta(2)], 2)), []);
  eq("no arm: observed stays null throughout", ui.observed, null);
});

test("own runs never observe (T31/T06): this page's liveTurn or resync already owns Stop", () => {
  ui.sessionId = S;
  ui.liveTurn = { session: S };
  eq("own liveTurn: the join arms nothing", observeJoin(S, liveOf([delta(1)], 1)), []);
  eq("own liveTurn: a rerouted event arms nothing", names(paintRerouted(delta(2), deps())), ["statusEvent", "paint"]);
  ui.liveTurn = null;
  ui.resync = { id: S, seen: false };
  eq("own resync: the join arms nothing", observeJoin(S, liveOf([delta(3)], 3)), []);
  eq("own: observed stays null", ui.observed, null);
});

test("third page (T06): a rerouted run event for the screen arms Stop once; background/done/terminal never do", () => {
  ui.sessionId = S;
  eq("seq-less background event: no arm (cron runs are not Stop-able here)",
    names(paintRerouted({ type: "delta", text: "x", session_id: S }, deps())), ["statusEvent", "paint"]);
  eq("a rerouted `done` arms nothing (it is the run's last word)", names(paintRerouted(ev("done", 9), deps())), ["statusEvent", "paint"]);
  check("... observed still null", ui.observed === null);
  noteRunTerminal(S, R2);
  eq("a terminal run's late event arms nothing", names(paintRerouted(delta(1, R2), deps())), ["statusEvent", "paint"]);
  eq("a live run's first event: paint + Stop + watch", names(paintRerouted(delta(1), deps())),
    ["statusEvent", "paint", "composer", "observeWatch"]);
  eq("... and arms the run", ui.observed, { id: S, runId: R1 });
});

test("switch (T30): leaving the session shows Send there; coming back re-arms from the join", () => {
  observing();
  eq("leave to the same session (a re-render): no change", observeLeave(S), null);
  eq("leave to another chat: cleared, composer → send", observeLeave(T), [["composer", "send"]]);
  check("leave: observed cleared, nothing to stop", ui.observed === null && stopTarget() === null);
  eq("leave again: nothing armed ⇒ null", observeLeave(S), null);
  ui.sessionId = S; // back
  eq("back: the join re-arms Stop", observeJoin(S, liveOf([delta(5)], 5)), [["composer", "stop"], ["observeWatch"]]);
  eq("+ New chat (leave to null): cleared too", observeLeave(null), [["composer", "send"]]);
});

test("switch (T30): with this page's OWN turn live elsewhere, leaving keeps Stop for that turn", () => {
  observing();
  ui.liveTurn = { session: T, detached: true };
  eq("own turn live: cleared, but NO composer hand-back", observeLeave(T), []);
  check("own turn live: Stop targets the own turn", ui.observed === null && stopTarget() === T);
});

test("terminal (T10): a rerouted `stopped` ends observing — Send + the stopped state, no reload", () => {
  observing();
  const fx = runStopped(ev("stopped", 4));
  eq("stopped: row stop, composer send, close + finalize + 'Stopped' line", fx,
    [["statusStop"], ["composer", "send"], ["closeSegments"], ["finalizeBubble"], ["systemMsg", "Stopped"]]);
  check("stopped: observed cleared", ui.observed === null);
  const done = runDoneWatched(ev("run_done", 5));
  eq("then run_done (detached runs publish it): closes + reloads, no second composer flip",
    composerOf(done), []);
});

test("terminal (T10/T11): a terminal naming ANOTHER run of the session leaves the observed run armed", () => {
  observing(R2);
  const fx = runStopped(ev("stopped", 9, R1)); // a late frame of the run before
  check("stale stopped: observed kept", ui.observed?.runId === R2);
  eq("stale stopped: composer NOT handed back while the observed run lives", composerOf(fx), []);
  const fx2 = runError(ev("error", 9, R1, { error: "x" }));
  check("stale error: observed kept", ui.observed?.runId === R2 && composerOf(fx2).length === 0);
  runError(ev("error", 4, R2, { error: "boom" }));
  check("own run's error: cleared", ui.observed === null);
});

test("terminal (T18): the observed run finishing normally (rerouted run_done) hands Send back", () => {
  observing();
  const fx = runDoneWatched(ev("run_done", 6));
  check("run_done: observed cleared", ui.observed === null);
  eq("run_done: row stop, close, finalize, picker reload, then composer → send", names(fx),
    ["statusStop", "closeSegments", "finalizeBubble", "pickerReload", "composer"]);
  eq("run_done: the composer goes to send", composerOf(fx), ["send"]);
  ui.liveTurn = { session: T };
  observing(R2);
  eq("run_done with an own turn live elsewhere: no hand-back", composerOf(runDoneWatched(ev("run_done", 7, R2))), []);
});

test("own-stream terminals keep Stop while a run is observed (T09/T10/T11/T13/T14/T12)", () => {
  // This page's own turn on T finished while the screen shows S observed —
  // its hand-back would strand the observed run with no Stop (D4's rule).
  const own = () => { observing(); ui.liveTurn = { session: T, detached: true }; };
  own(); eq("T09 done: no send", composerOf(runFinish({ type: "done" })), []);
  own(); eq("T10 own stopped (chat SSE, no session_id): no send", composerOf(runStopped({ type: "stopped" })), []);
  own(); eq("T11 own error: no send", composerOf(runError({ type: "error", error: "e" })), []);
  own(); eq("T13 send failed: no send", composerOf(sendFailed("m", false)), []);
  own(); eq("T14 silent end: no send", composerOf(streamEndSilent()), []);
  own(); eq("T12 rejected: composer stays stop", composerOf(sendRejected("busy")), ["stop"]);
  check("own terminals: the observed run is still the Stop target", ui.observed?.id === S && stopTarget() === S);
});

test("T22: the one clear retires an observed run too — scoped to its session", () => {
  observing();
  eq("abandon another session: nothing", abandonRun(T), []);
  check("... observed kept", ui.observed?.id === S);
  eq("abandon the observed session: row + composer hand back", abandonRun(S), [["statusStop"], ["composer", "send"]]);
  check("... observed cleared", ui.observed === null);
});

test("watch (T32 observeTick): asks /live at the join's watermark and gap-fills the observed screen", async () => {
  check("nothing observed: no tick, no ask", (await observeTick(deps({ fetchLive: neverAsk }))) === null);
  observing();
  ui.sessionId = T;
  check("observed run off screen: no tick, no ask", (await observeTick(deps({ fetchLive: neverAsk }))) === null);
  ui.sessionId = S;
  const f = fetcher({ active: true, seq: 5, oldest_seq: 1, gap: false, events: [delta(4), delta(5)] });
  const res = await observeTick(deps(f));
  eq("watch: ONE ask, at the join's watermark", f.calls, [[S, 3]]);
  eq("watch: the sender's attached run streams here through the gap-fill", res.effects, [["gapFill", S, [delta(4), delta(5)]]]);
  check("watch: still observed (the run lives)", ui.observed?.id === S);
});

test("watch (T32): the run ended — stopped by any device or finished — heals the screen and hands Send back", async () => {
  observing();
  const res = await observeTick(deps(fetcher({ active: false })));
  eq("ended: verdict terminal", res.verdict, "terminal");
  eq("ended: row stop + Send, then the persisted heal render", res.effects,
    [["statusStop"], ["composer", "send"], ["rerenderHeal", S]]);
  check("ended: observed cleared ⇒ the next tick stops the timer", ui.observed === null
    && (await observeTick(deps({ fetchLive: neverAsk }))) === null);
});

test("watch (T32): offline keeps watching; a new run on the session cold-joins (re-armed by the join)", async () => {
  observing();
  const off = await observeTick(deps(fetcher(null)));
  check("offline: no effects, observed kept", off.verdict === "offline" && off.effects.length === 0 && ui.observed?.id === S);
  const res = await observeTick(deps(fetcher({ active: true, seq: 1, events: [delta(1, R2)] })));
  eq("new run: the one sanctioned join re-renders the screen", res.effects, [["rerenderCold", S, false]]);
  eq("new run: the join's render re-arms (observeJoin on the fresh live block)",
    observeJoin(S, liveOf([delta(1, R2)], 1)), [["composer", "stop"], ["observeWatch"]]);
  eq("new run: observed now names it", ui.observed, { id: S, runId: R2 });
});

test("stop racing the answer: Stop posted, then `done` + run_done win — Send, no re-arm, no stray stop", async () => {
  observing();
  eq("click: Stop posts /stop for the observed session", stopRequest(), [["netStop", S]]);
  check("click: no state change — the terminal drives (T04)", ui.observed?.id === S);
  const d = paintRerouted(ev("done", 4), deps());
  eq("`done` rerouted: paints/row only, no composer flip yet", composerOf(d), []);
  eq("run_done: composer → send", composerOf(runDoneWatched(ev("run_done", 5))), ["send"]);
  eq("a late `title`-time event of the ended run cannot re-arm (terminal stamp)",
    names(paintRerouted(delta(6), deps())), ["statusEvent", "paint"]);
  check("nothing observed, nothing to stop", ui.observed === null && stopTarget() === null);
  // The attached-sender variant: no events reach this page at all — the watch
  // learns the end from /live (the stop raced `done`; either way inactive).
  ui.liveDone = {}; ui.liveSeq = {};
  observing();
  const res = await observeTick(deps(fetcher({ active: false })));
  eq("attached sender: the watch's terminal verdict hands Send back", composerOf(res.effects), ["send"]);
});

test("hidden mid-run (T20): resume asks ONE verdict for the observed screen — live keeps Stop, ended hands Send back", async () => {
  observing();
  const f = fetcher({ active: true, seq: 6, events: [delta(4), delta(5), delta(6)] });
  const res = await visibleResume(deps(f));
  eq("resume: ONE ask, at the join's watermark", f.calls, [[S, 3]]);
  eq("resume: gap-fill, no composer change", names(res.effects), ["gapFill"]);
  check("resume: still observed", ui.observed?.id === S);
  const res2 = await visibleResume(deps(fetcher({ active: false })));
  eq("resume, run ended while hidden: Send + the heal", res2.effects,
    [["statusStop"], ["composer", "send"], ["rerenderHeal", S]]);
  check("resume: observed cleared", ui.observed === null);
});

test("effects: the new tokens dispatch through a total fx map (observeWatch bound)", () => {
  const calls = [];
  const fx = { composer: (m) => calls.push(["composer", m]), observeWatch: () => calls.push(["observeWatch"]) };
  ui.sessionId = S;
  applyEffects(observeJoin(S, liveOf([delta(1)], 1)), fx);
  eq("dispatch: composer then watch", calls, [["composer", "stop"], ["observeWatch"]]);
});

// BK-041 G5: /live carries the run's CURRENT phase ({started_ms, phase},
// gateway/live.py LivePhase) — the observer's row tracks it every tick
// instead of freezing on whatever the join showed (or showing nothing).
test("watch (T32, BK-041 G5): each tick drives the observer's row from /live's phase", async () => {
  observing();
  const phase = { type: "tool_call", tool: "web_search" };
  const f = fetcher({ active: true, seq: 5, oldest_seq: 1, gap: false, events: [delta(4), delta(5)],
    started_ms: 1000, phase });
  const res = await observeTick(deps(f));
  eq("g5: gap-fill, then the row's phase", res.effects,
    [["gapFill", S, [delta(4), delta(5)]], ["statusEvent", phase]]);
  // A pre-BK-041 server (no phase field): the tick is unchanged.
  observing(R1, 5);
  const old = await observeTick(deps(fetcher({ active: true, seq: 6, oldest_seq: 1, gap: false, events: [delta(6)] })));
  eq("g5: no phase field ⇒ gap-fill only", names(old.effects), ["gapFill"]);
});

// T34 (BK-041 G4): the /messages join's ROW — a run already under way opens
// the row at its current phase and real elapsed time; bg_live also keeps
// the T28 mark honest (set: a missed run_done heals; null: none is running).
test("row join (T34): live or bg_live opens the row; bg_live sets/clears the T28 mark", () => {
  ui.sessionId = S;
  const view = { started_ms: 1000, phase: { type: "tool_call", tool: "web_search" } };
  const live = { ...liveOf([delta(1)], 1), ...view };
  eq("chat run: the row joins at the live block's phase", rowJoin(S, live, null), [["statusJoin", live]]);
  check("chat run: no T28 mark (bg_live null clears it)", !ui.bgLive[S]);
  eq("bg run: the row joins at bg_live's phase", rowJoin(S, null, view), [["statusJoin", view]]);
  eq("bg run: the T28 mark is set (resume heals a missed run_done)", ui.bgLive[S], { gap: false });
  ui.bgLive[S] = { gap: true }; // a hidden page's mark: run_done still owes a heal
  rowJoin(S, null, view);
  eq("bg run: an existing mark keeps its hole (the resume re-render must not drop the heal)",
    ui.bgLive[S], { gap: true });
  eq("nothing running: no row, mark cleared", rowJoin(S, null, null), []);
  check("nothing running: ... the stale mark is gone", !ui.bgLive[S]);
  ui.bgLive[S] = { gap: false };
  eq("pre-BK-041 server (bg_live undefined): no row, mark untouched", rowJoin(S, null, undefined), []);
  check("... the mark survives an unknown", !!ui.bgLive[S]);
  eq("fetch failed (live undefined): unknown ⇒ nothing", rowJoin(S, undefined, view), null);
  eq("off-screen: nothing", rowJoin(T, live, null), null);
});
