/* runstate.js's transition table (T01–T22, T27) — plan bk-019-run-lifecycle
   Step 3. One owner, one table: each check names its transition (T-id) and the
   recovery path it covers, keyed to the design's §4 coverage map —
     P1 PWA suspend/detach ....... T15 (socket), T16 (reroute's first word),
                                   T20→T27 gap-fill, T06 (paint meanwhile)
     P2 watchdog ................. T21 → T27 verdicts, no timer state inside
     P3 run_done heal ............ T17 (knownTerminal — /live is NEVER asked),
                                   T18, T19
     P4 cold-open resume ......... T20 guards, T27 stale/heal/cold-join
     P5 Stop ..................... T04 + the T09/T10 outcomes
     P6 error .................... T11, T12, T13, T14
     P7 session switch ........... T03 latch, T05 suppression, T09/T10/T11
                                   clears while latched, T02 §10.2, T15, T17
     + post-terminal late title .. T07 (per-run liveDone guard, not a phase)
     + run-identity cold-join .... T27 mismatch / terminal / provisional
   plus the tier-contract pair pinning the runner's run-state reset.

   deps: replay.js's REAL arithmetic is injected (the same functions events.js
   will pass in Step 4); fetchLive/isReplaying are scripted fakes. Effects are
   asserted as data; the fx spy doubles as the dispatch smoke. rAF-free,
   DOM-free — which is the whole point of the module's shape (design §2.6). */

import { ui } from "./state.js";
import {
  derivePhase, screenOwns, latched, pendingFor, stopTarget, screenRunLive,
  submit, sessionAnnounce, screenLeave, reopenResume, stopRequest, paintChat, paintRerouted,
  titleSeen, turnComplete, runFinish, runStopped, runError, sendRejected,
  sendFailed, streamEndSilent, socketDied, catchingUp,
  runDoneWatched, runDoneOther, abandonRun, bgRunStart,
  slotFor, dropSlot, noteRunTerminal, applyEffects,
} from "./runstate.js";
// BK-034: the verdict half (T17/T20/T21 → T27 + phaseNow) moved verbatim to
// resync.js at runstate's line cap — the same table, one more file.
import { phaseNow, runDoneHeal, visibleResume, watchdogTick, resyncVerdict } from "./resync.js";
import {
  alreadyPainted, markPainted, joinReason, raiseWatermark, storedWatermark,
  isProvisionalSlot, isTerminalRun, flushBuffered,
} from "./replay.js";
import { test, check, eq, slotOf } from "./runner.js";

const S = "unit-run";
const R1 = "run-aaaa";
const R2 = "run-bbbb";
const R9 = "run-cccc";

const delta = (seq, runId = R1, extra) =>
  ({ type: "delta", text: "w", seq, run_id: runId, ...(extra || {}) });

// The design §2.6 fx map (the 23 tokens Step 4 built, + BK-034's observeWatch). The spy records calls
// as [name, ...args]; every assertion on effects goes through eq/check — this
// helper reports nothing, so the tier's anti-vacuity control (replay.test.js,
// covering check AND eq) stays sufficient for this suite.
const FX_NAMES = ["statusStart", "statusPhase", "statusWarn", "statusStop",
  "statusEvent", "statusLane", "composer", "paint", "paintUser", "closeSegments",
  "finalizeBubble", "errorMsg", "systemMsg", "rerenderHeal", "rerenderCold",
  "gapFill", "pickerReload", "pickerRelabel", "pickerOpenNew", "netStop",
  "netDetach", "armWatchdog", "pollApprovalsSoon",
  "observeWatch", // BK-034: arm the observe watch (events.js owns the timer)
  "statusJoin"]; // BK-041 T34: open the row at a joined run's phase + start
function fxSpy() {
  const calls = [];
  const fx = {};
  for (const n of FX_NAMES) fx[n] = (...args) => calls.push([n, ...args]);
  return { fx, calls };
}

// The deps bag: replay.js's real arithmetic + scripted transport.
const realReplay = { alreadyPainted, markPainted, joinReason, raiseWatermark,
  storedWatermark, isProvisionalSlot };
const depsWith = (over) => ({ isReplaying: () => false, ...realReplay, ...over });
const fetcher = (res) => {
  const calls = [];
  return { calls, fetchLive: async (id, since) => { calls.push([id, since]); return res; } };
};
const neverAsk = async () => { throw new Error("T27 must not ask /live here"); };

// ── tier contract ──────────────────────────────────────────────────────────

test("tier: state.js DECLARES every ui field the lifecycle reads (BK-019's declaration rule)", () => {
  for (const k of ["sessionId", "expecting", "resync", "liveTurn", "liveSeq", "liveDone", "bgLive"])
    check("declaration: ui." + k + " is a declared key on the shared object",
      Object.prototype.hasOwnProperty.call(ui, k));
  check("declaration: liveSeq/liveDone/bgLive start as objects, not lazy-undefined (replay's `|| {}` init dies in Step 4)",
    typeof ui.liveSeq === "object" && typeof ui.liveDone === "object" && typeof ui.bgLive === "object");
});

// The reset pair: THIS test arms, the NEXT asserts the runner cleared. If the
// arms fail, this test reds; if the reset regresses, the next test reds — one
// of the pair is always watching. (runstate transitions read/write these three
// fields; without the reset, one test's stream would leak into the next.)
// Transitions MUTATE the liveTurn they are handed (the D3 latch rides on it):
// build a fresh object per test — a reused, latched one reds like a product bug.
test("tier: THIS test arms resync/liveTurn/expecting — the next test must find them reset", () => {
  ui.resync = { id: S, seen: false };
  ui.liveTurn = { session: S };
  ui.expecting = true;
  ui.bgLive[S] = { gap: true };
  check("tier: the arming landed (if this reads false, the shared-state seam broke)",
    ui.resync?.id === S && ui.liveTurn?.session === S && ui.expecting === true && ui.bgLive[S]?.gap === true);
});

test("tier: the runner re-arms the run-state fields between tests (the seam IS the reset)", () => {
  check("tier: resync/liveTurn/expecting/sessionId back to initial (the previous test armed them)",
    ui.resync === null && ui.liveTurn === null && ui.expecting === false && ui.sessionId === null);
  eq("tier: the maps are fresh (the previous tests' slots/stamps/background marks are gone)",
    [Object.keys(ui.liveSeq).length, Object.keys(ui.liveDone).length, Object.keys(ui.bgLive).length], [0, 0, 0]);
});

// ── phase: DERIVED, never stored; no transition reads it ───────────────────

test("phase: derived from the fields alone — idle / streaming / detached", () => {
  eq("phase: no fields ⇒ idle", derivePhase(null, null), "idle");
  eq("phase: liveTurn ⇒ streaming", derivePhase(null, { session: S }), "streaming");
  eq("phase: resync ⇒ detached", derivePhase({ id: S, seen: false }, null), "detached");
  eq("phase: resync WINS over liveTurn (T15 keeps the turn while recovery arms — the screen reads detached)",
    derivePhase({ id: S, seen: false }, { session: S }), "detached");
  ui.sessionId = S;
  ui.liveTurn = { session: S };
  eq("phase: phaseNow projects the fields", phaseNow(S), "streaming");
  ui.liveTurn = { session: S, detached: true };
  eq("phase: latched is a FACET — the phase still reads streaming (it gates paints, not lifecycle)",
    phaseNow(S), "streaming");
  check("predicates: latched() and screenOwns() read the live fields", latched() === true && screenOwns() === true);
  ui.sessionId = "other";
  check("predicates: pendingFor(null) is false even with no resync (a name-less event must never 'match' the slot)",
    pendingFor(null) === false);
});

test("phase: 'resyncing' names an in-flight verdict and nothing else (transient, never a guard)", async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  ui.sessionId = S;
  markPainted(S, delta(3)); // primed slot: this screen is the cold-open subject
  const p = resyncVerdict(S, false, depsWith({ fetchLive: async () => { await gate; return { active: false }; } }));
  eq("phase: while the /live fetch is in flight, phaseNow reads resyncing", phaseNow(S), "resyncing");
  eq("phase: a different id is not resyncing", phaseNow("elsewhere"), "idle");
  release();
  const r = await p;
  eq("verdict: the post-fetch branch ran to its terminal verdict", r.verdict, "terminal");
  eq("phase: settles back to the derived phase once the fetch lands", phaseNow(S), "idle");
});

// ── P5/P7 healthy stream: T01–T11 ──────────────────────────────────────────

test("submit (T01, P5): arms the turn with the screen's session and hands the composer to Stop", () => {
  ui.sessionId = "chat-1";
  const r = fxSpy();
  applyEffects(submit("hello", [{ id: "a1" }], "sonnet"), r.fx);
  eq("submit: liveTurn armed with the screen's session", ui.liveTurn, { session: "chat-1" });
  eq("submit: composer→stop, lane, statusStart, paintUser — in order", r.calls,
    [["composer", "stop"], ["statusLane", "sonnet"], ["statusStart"], ["paintUser", "hello", [{ id: "a1" }]]]);
  eq("stopTarget: the turn is the Stop target now", stopTarget(), "chat-1");
  ui.sessionId = null;
  submit("hi", [], undefined);
  check("submit: a new chat arms with session null (the session frame names it — T02)", ui.liveTurn.session === null);
});

test("session frame (T02, P7 §10.2): the liveTurn write is UNCONDITIONAL; the screen follows only while it owns", () => {
  ui.liveTurn = { session: null }; // new chat, just submitted
  const r = fxSpy();
  applyEffects(sessionAnnounce({ type: "session", session_id: "chat-9" }), r.fx);
  eq("session frame: liveTurn.session takes the frame's id", ui.liveTurn, { session: "chat-9" });
  eq("session frame: ui.sessionId follows while the screen owns", ui.sessionId, "chat-9");
  eq("session frame: pickerOpenNew is the only effect", r.calls, [["pickerOpenNew", "chat-9"]]);

  ui.liveTurn = { session: null };
  ui.sessionId = "elsewhere"; // user switched between submit and the frame
  const r2 = fxSpy();
  applyEffects(sessionAnnounce({ type: "session", session_id: "chat-9" }), r2.fx);
  eq("session frame (off-screen): liveTurn.session STILL updated — a screen-gated write would arm T15 with resync.id null and kill the turn's Stop/recovery", ui.liveTurn, { session: "chat-9" });
  eq("session frame (off-screen): ui.sessionId NOT dragged to the run's session", ui.sessionId, "elsewhere");
  eq("session frame (off-screen): no picker op for a stream that lost the screen", r2.calls, []);
});

test("session switch (T03, P7): the first off-screen chat event latches the stream once, for good", () => {
  ui.liveTurn = { session: "A" };
  ui.sessionId = "B";
  const r = fxSpy();
  applyEffects(screenLeave(), r.fx);
  eq("switch: the latch rides liveTurn (D3 — dies with the turn, not a submit closure)", ui.liveTurn, { session: "A", detached: true });
  eq("switch: the row stops once — the visible session owns it now", r.calls, [["statusStop"]]);
  check("switch: a later event does not re-fire (one-way latch, today's ctx semantics)", screenLeave() === null);
  eq("switch: ... and no second statusStop", r.calls.length, 1);
  ui.liveTurn = null;
  check("switch: no turn ⇒ nothing to latch", screenLeave() === null);
});

test("paint (T05, P7): the screen's stream marks and paints; a latched one does neither", () => {
  const deps = depsWith({});
  ui.liveTurn = { session: S };
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(paintChat({ type: "delta", text: "x" }, S, deps), r.fx);
  eq("paint: statusEvent then paint (status-only frames ride the SAME tail — §10.11)", r.calls,
    [["statusEvent", { type: "delta", text: "x" }], ["paint", { type: "delta", text: "x" }]]);
  const r2 = fxSpy();
  applyEffects(paintChat({ type: "tool_call", name: "web_search" }, S, deps), r2.fx);
  eq("paint: a tool_call polls approvals between row and paint", r2.calls.map((c) => c[0]),
    ["statusEvent", "pollApprovalsSoon", "paint"]);
  check("paint: a seq'd event is marked for the dedupe",
    paintChat(delta(4), S, deps) !== null && alreadyPainted(S, delta(4)) === true);
  check("paint: an already-painted seq is refused (the reroute race)", paintChat(delta(4), S, deps) === null);
  ui.liveTurn = { session: S, detached: true };
  ui.sessionId = "elsewhere"; // the latch: the screen left the stream's session
  check("paint: latched paints nothing", paintChat(delta(9), S, deps) === null);
  check("paint: ... and marks nothing (its DOM is gone; reopening re-renders and re-primes the slot)",
    alreadyPainted(S, delta(9)) === false);
  const tc = { type: "tool_call", name: "web_search", seq: 12 };
  const r3 = fxSpy();
  applyEffects(paintChat(tc, S, deps), r3.fx);
  eq("paint: a LATCHED tool_call still polls approvals (today's sink polls outside its detached return — the off-screen run's card must still appear)", r3.calls, [["pollApprovalsSoon"]]);
  check("paint: ... while still marking nothing", alreadyPainted(S, tc) === false);
  // BK-024: back on its session, a latched stream still paints nothing until
  // the reopen's render un-latches it (T35) — the join owns what came before.
  ui.sessionId = S;
  check("paint: latched + back on its session, pre-render → still nothing", paintChat(delta(10), S, deps) === null);
});

test("paint (T05/T06, BK-024): mid-replay the paint is buffered, so the FLUSH marks it", () => {
  const deps = depsWith({ isReplaying: () => true });
  ui.liveTurn = { session: S };
  ui.sessionId = S;
  eq("replaying: the event still paints (into the buffer)", paintChat(delta(30), S, deps).map((c) => c[0]),
    ["statusEvent", "paint"]);
  check("replaying: ... unmarked — a mark now would make flushBuffered skip the very event it buffered",
    alreadyPainted(S, delta(30)) === false);
  const rr = delta(31, R1, { session_id: S });
  check("replaying: a rerouted paint defers its mark the same way",
    paintRerouted(rr, deps) !== null && alreadyPainted(S, rr) === false);
  const seen = [];
  eq("replaying: the flush paints both and marks them", flushBuffered(S, [delta(30), rr], (ev) => seen.push(ev.seq)), 2);
  check("replaying: ... now marked", seen.join() === "30,31" && alreadyPainted(S, delta(30)) && alreadyPainted(S, rr));
});

test("reopen (T35, BK-024): the render for the latched stream's own session un-latches it", () => {
  ui.liveTurn = { session: S, detached: true };
  check("reopen: another session's render leaves the latch", reopenResume("elsewhere") === null && latched() === true);
  check("reopen: a greeting render (id null) leaves it", reopenResume(null) === null && latched() === true);
  reopenResume(S);
  check("reopen: its own session's render un-latches", latched() === false);
  eq("reopen: ... and keeps the turn (Stop still targets it)", [ui.liveTurn.session, stopTarget()], [S, S]);
  ui.sessionId = "elsewhere";
  eq("reopen: a later switch re-latches (T03)", screenLeave(), [["statusStop"]]);
  ui.liveTurn = null;
  check("reopen: no turn ⇒ nothing", reopenResume(S) === null && ui.liveTurn === null);
});

test("paint (T06, P1): a rerouted event paints only its own session while it is on screen", () => {
  const deps = depsWith({});
  ui.sessionId = S;
  check("reroute: a screen-matched event paints", paintRerouted({ type: "delta", session_id: S }, deps) !== null);
  check("reroute: an off-screen event is refused", paintRerouted({ type: "delta", session_id: "elsewhere" }, deps) === null);
  const ev = delta(6, R1, { session_id: S });
  check("reroute: the paint marks the slot", paintRerouted(ev, deps) !== null && alreadyPainted(S, ev) === true);
  check("reroute: the duplicate is skipped (word-doubling class)", paintRerouted(ev, deps) === null);
});

test("late title (T07): a POST-terminal title relabels the RUN's option and touches nothing else", () => {
  markPainted(S, delta(2));
  noteRunTerminal(S, R1); // the run is OVER; a stopped detached run may still publish `title`
  ui.sessionId = "B";     // and the user has since switched screens
  const r = fxSpy();
  applyEffects(titleSeen({ type: "title", title: "Trip notes", session_id: S }, S), r.fx);
  eq("late title: pickerRelabel names the RUN's session, never the screen's (§10.6)", r.calls, [["pickerRelabel", S, "Trip notes"]]);
  eq("late title: no slot touch — the guard is per-run (liveDone), NOT a phase", [slotOf(S) && slotOf(S).painted, ui.liveDone[S]], [[[2, 2]], R1]);
  check("late title: liveTurn/resync untouched (terminality persists in liveDone, D2)", ui.liveTurn === null && ui.resync === null);
  eq("late title: a title-less frame claims the event with no effect", titleSeen({ type: "title", session_id: S }, S), []);
});

test("turn_complete (T08): the screen's own answer lands; a latched stream stops nothing", () => {
  ui.liveTurn = { session: S };
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(turnComplete({ type: "turn_complete" }), r.fx);
  eq("turn_complete: statusStop then closeSegments", r.calls, [["statusStop"], ["closeSegments"]]);
  check("turn_complete: no state write — naming may still run (this is not terminal)", ui.liveTurn?.session === S);
  ui.liveTurn = { session: S, detached: true };
  ui.sessionId = "B";
  check("turn_complete: a latched stream is refused (today's detached statusStop would stop the VISIBLE session's row)", turnComplete({ type: "turn_complete" }) === null);
  check("turn_complete: events frames must name the screen's session",
    turnComplete({ type: "turn_complete", session_id: "off" }) === null
    && turnComplete({ type: "turn_complete", session_id: "B" }) !== null);
});

test("done (T09, P7): terminal lands on idle; latched suppresses PAINTS, never the clear or the hand-back", () => {
  ui.liveTurn = { session: S };
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(runFinish({ type: "done", title: "T" }), r.fx);
  check("done: liveTurn cleared — the fields land back at idle", ui.liveTurn === null);
  // Today's `done` left the composer on stop and the post-loop stream-end tail
  // (dataset-mode check) fired statusStop+send right after. With T14 stated
  // over fields (liveTurn — which this clear nulls), that tail can never fire
  // post-done, so the row + composer hand-back rides HERE (same task, the
  // tail ran before the next event could). This is THE pinning test for that
  // move: the hand-back must be here or `done` never frees Stop.
  eq("done: reload (title present), close + finalize, then the row + composer hand-back", r.calls,
    [["pickerReload"], ["closeSegments"], ["finalizeBubble"], ["statusStop"], ["composer", "send"]]);
  check("done: the hand-back follows the clear (T14 reads liveTurn — double-fire is structurally impossible)", ui.liveTurn === null);
  ui.liveTurn = { session: S, detached: true };
  ui.sessionId = "B";
  const r2 = fxSpy();
  applyEffects(runFinish({ type: "done" }), r2.fx);
  check("done: latched still clears (terminal events always retire liveTurn, matching today)", ui.liveTurn === null);
  // Today's tail wasn't latch-suppressed either: a latched silent stream-end
  // handed the composer back — and the latched `done` cleared liveTurn, so
  // T14 can't cover it. A latch-gated hand-back would be a change, not parity.
  eq("done: ... latched suppresses the transcript effects, NOT the hand-back (the tail it replaces wasn't latch-gated)", r2.calls,
    [["statusStop"], ["composer", "send"]]);
  ui.liveTurn = { session: S, detached: true }; // the last clear consumed the latch
  const r3 = fxSpy();
  applyEffects(runFinish({ type: "done", title: "T" }), r3.fx);
  eq("done: even latched, a finished title reloads the picker (bookkeeping, not paint)", r3.calls,
    [["pickerReload"], ["statusStop"], ["composer", "send"]]);
  // A chat `done` cannot normally coexist with an armed resync (the stream
  // that armed it is closed, and the composer stays on stop until its heal) —
  // but T10/T11 hand the composer back beside an armed resync, so the old
  // stream-end tail's `!ui.resync` conjunct stays: the composer is the only
  // Stop affordance and that detached run may still be live (D4/T22).
  ui.liveTurn = { session: S };
  ui.resync = { id: "A", seen: false };
  const r4 = fxSpy();
  applyEffects(runFinish({ type: "done", title: "T" }), r4.fx);
  check("done: the clear still lands while a resync is armed", ui.liveTurn === null);
  check("done: ... but Stop is NOT handed back over a live detached run (the old tail's !resync guard, D4/T22)",
    ui.resync?.id === "A" && r4.calls.every((c) => c[0] !== "composer"));
  eq("done: ... the transcript + picker still run, the hand-back alone is withheld", r4.calls,
    [["pickerReload"], ["closeSegments"], ["finalizeBubble"]]);
});

test("stopped/error (T10/T11, P5+P6): row + composer always; transcript only for the screen's stream", () => {
  ui.liveTurn = { session: S };
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(runStopped({ type: "stopped" }), r.fx);
  eq("stopped: statusStop, composer send, close, finalize, the Stopped line", r.calls,
    [["statusStop"], ["composer", "send"], ["closeSegments"], ["finalizeBubble"], ["systemMsg", "Stopped"]]);
  check("stopped: liveTurn cleared", ui.liveTurn === null);
  ui.liveTurn = { session: S, detached: true };
  ui.sessionId = "B";
  const r2 = fxSpy();
  applyEffects(runStopped({ type: "stopped" }), r2.fx);
  eq("stopped: latched — the clear + row/composer still run (a finished run must not keep Stop armed)", r2.calls, [["statusStop"], ["composer", "send"]]);
  check("stopped: ... and no Stopped line on a stranger's transcript", r2.calls.every((c) => c[0] !== "systemMsg"));
  ui.liveTurn = { session: S, detached: true }; // the last clear consumed the latch
  const r3 = fxSpy();
  applyEffects(runError({ type: "error", error: "boom" }), r3.fx);
  eq("error: latched — clear + row/composer only", r3.calls, [["statusStop"], ["composer", "send"]]);
  ui.liveTurn = { session: S };
  ui.sessionId = S;
  const r4 = fxSpy();
  applyEffects(runError({ type: "error", error: "boom" }), r4.fx);
  eq("error: on screen the error line lands", r4.calls, [["statusStop"], ["composer", "send"], ["errorMsg", "boom"]]);
  check("error: events frames naming another session are refused", runError({ type: "error", session_id: "off", error: "x" }) === null);
});

// ── P6: failed sends and the silent end ────────────────────────────────────

test("send failure (T12/T13, P6 D5): the armed-but-never-started turn is cleared, uniformly", () => {
  ui.liveTurn = { session: "chat-1" };
  const r = fxSpy();
  applyEffects(sendRejected("session is busy"), r.fx);
  check("rejected: liveTurn cleared (today's leak left phaseNow reading STREAMING with no stream)", ui.liveTurn === null);
  eq("rejected: error line, row stop, composer send", r.calls, [["errorMsg", "session is busy"], ["statusStop"], ["composer", "send"]]);
  ui.liveTurn = { session: "chat-2" };
  ui.resync = { id: "A", seen: true };
  const r2 = fxSpy();
  applyEffects(sendRejected("busy"), r2.fx);
  eq("rejected: with a recovery pending the composer STAYS on stop", r2.calls[2], ["composer", "stop"]);
  check("rejected: ... and Stop falls back to resync.id exactly as app.js intends", stopTarget() === "A");
  ui.liveTurn = { session: "chat-3" };
  const r3 = fxSpy();
  applyEffects(sendFailed("request failed: net down", false), r3.fx);
  check("failed: liveTurn cleared too (D5 uniform clear)", ui.liveTurn === null);
  eq("failed: error line, row stop, composer send", r3.calls, [["errorMsg", "request failed: net down"], ["statusStop"], ["composer", "send"]]);
  ui.liveTurn = { session: "chat-4" };
  check("failed: with the stream CONFIRMED the death is recovery, not failure (T15's case — the split is the transport's flag, §10.1)", sendFailed("x", true) === null);
  eq("failed: ... refused means no clear and no effects", ui.liveTurn, { session: "chat-4" });
});

test("silent stream end (T14, P6): the guard is FIELD state, not a DOM read", () => {
  check("silent end: idle ⇒ nothing", streamEndSilent() === null);
  ui.liveTurn = { session: S };
  ui.resync = { id: S, seen: false };
  check("silent end: a resync pending ⇒ recovery owns the state (liveTurn ∧ !resync IS the old dataset-mode test)", streamEndSilent() === null);
  eq("silent end: ... nothing written", [ui.liveTurn?.session, ui.resync?.id], [S, S]);
  ui.resync = null;
  const r = fxSpy();
  applyEffects(streamEndSilent(), r.fx);
  // No args = a stream that never STARTED (no `session` frame). BK-036: a
  // started one's silent end is T15's recoverable state — senderstop.test.js.
  check("silent end: a lone liveTurn on a never-started stream ⇒ hand the composer back", ui.liveTurn === null);
  eq("silent end: row + composer", r.calls, [["statusStop"], ["composer", "send"]]);
});

// ── P1: suspend → detach → reroute ─────────────────────────────────────────

test("socket died mid-run (T15, P1+P7): recovery arms with the STREAM's session; Stop stays armed", () => {
  ui.liveTurn = { session: "A", detached: true };
  ui.sessionId = "B"; // the screen moved on
  const r = fxSpy();
  applyEffects(socketDied("A", true), r.fx);
  eq("detach: resync armed with the stream's session — never the screen's (a screen id would break pendingFor and Stop for the whole turn)", ui.resync, { id: "A", seen: false });
  eq("detach: liveTurn KEPT — Stop stays armed while the server run lives", ui.liveTurn, { session: "A", detached: true });
  eq("detach: warn, the reroute POST, the watchdog arm", r.calls, [["statusWarn", "Reconnecting · still working"], ["netDetach", "A"], ["armWatchdog"]]);
  eq("detach: stopTarget still reaches the detached run", stopTarget(), "A");
  check("detach: a pre-confirmation death is T13's case, not a recovery (§10.1)", socketDied("A", false) === null);
  eq("detach: ... no new resync armed for a run that never started", ui.resync, { id: "A", seen: false });
});

test("reroute's first word (T16, P1 §10.10): seen flips ONCE, for the pending run ON its screen", () => {
  ui.resync = { id: S, seen: false };
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(catchingUp({ type: "delta", session_id: S }), r.fx);
  check("catching: the ONE flip (it also retires the watchdog's premise — its timer reads seen)", ui.resync.seen === true);
  eq("catching: the row reframes once", r.calls, [["statusPhase", "Catching up…", { waiting: true }]]);
  check("catching: a second event does not re-fire", catchingUp({ type: "delta", session_id: S }) === null);
  eq("catching: ... no second effect", r.calls.length, 1);
  ui.resync = { id: S, seen: false };
  ui.sessionId = "B";
  check("catching: an OFF-SCREEN reroute is refused before the flip", catchingUp({ type: "delta", session_id: S }) === null);
  check("catching: ... seen stays false (no 'Catching up…' in the visible session's row, no early watchdog-premise retirement a target)", ui.resync.seen === false);
  ui.resync = { id: "OTHER", seen: false };
  check("catching: needs a resync pending for THAT session", catchingUp({ type: "delta", session_id: S }) === null);
  ui.resync = null;
  check("catching: nothing armed ⇒ nothing to flip", catchingUp({ type: "delta", session_id: S }) === null);
});

test("resume (T20→T27 gap-fill, P1): the ring tail paints into the EXISTING DOM, watermarked from the response's own seq", async () => {
  ui.resync = { id: S, seen: true };
  ui.sessionId = S;
  markPainted(S, delta(3)); // the slot vouches for R1, water 3
  const f = fetcher({ active: true, seq: 9, events: [delta(4), delta(5)] });
  const r = fxSpy();
  const res = await visibleResume(depsWith(f));
  eq("resume: verdict live", res.verdict, "live");
  eq("resume: the /live ask carries OUR stored watermark (two-sinces rule: never a fresh 0)", f.calls, [[S, 3]]);
  eq("resume: one gapFill effect carrying the batch (no client gap flag, no seq arithmetic)", res.effects, [["gapFill", S, [delta(4), delta(5)]]]);
  eq("resume: the watermark rode the response's seq", storedWatermark(S), 9);
  check("resume: the resync is NOT cleared — the run is still live", ui.resync !== null);
  applyEffects(res.effects, r.fx); // dispatch smoke
  ui.sessionId = "B";
  const f2 = fetcher({ active: true, seq: 12, events: [] });
  const res2 = await resyncVerdict(S, false, depsWith(f2));
  eq("resume: off-screen returns live with NO paint effect", res2.effects, []);
  eq("resume: ... the watermark still advanced (it is state, not paint)", storedWatermark(S), 12);
  ui.sessionId = S;
  const f3 = fetcher({ active: true, seq: 20, events: [] });
  const res3 = await resyncVerdict(S, false, depsWith({ ...f3, isReplaying: () => true }));
  eq("resume: mid-replay suppresses the paint effect (the gap-fill race guard)", res3.effects, []);
  check("resume: the isReplaying guard did not change the verdict", res3.verdict, "live");
});

test("verdict tree (T27, P1+P4): offline / missing / stale are side-effect-free", async () => {
  ui.resync = { id: S, seen: false };
  let res = await resyncVerdict(S, false, depsWith({ fetchLive: async () => null }));
  eq("offline: fetch null ⇒ the caller asks again", res.verdict, "offline");
  eq("offline: zero effects", res.effects, []);
  res = await resyncVerdict(S, false, depsWith({ fetchLive: async () => ({ missing: true }) }));
  eq("missing: the 404 sentinel — the watchdog STOPs probing (a store render there is the lossy path: no live join)", res.verdict, "missing");
  eq("missing: zero effects", res.effects, []);
  ui.resync = { id: "OTHER", seen: false }; // recovery moved to another run
  res = await resyncVerdict(S, false, depsWith({ fetchLive: async () => ({ active: true, seq: 1, events: [] }) }));
  eq("stale: neither the pending run nor the cold-open subject ⇒ recovery moved on", res.verdict, "stale");
  eq("stale: zero effects", res.effects, []);
  check("verdict tree: the resync slot survived untouched (refused branches write NOTHING)",
    ui.resync?.id === "OTHER" && ui.liveTurn === null && Object.keys(ui.liveDone).length === 0);
});

// ── P2: the watchdog ───────────────────────────────────────────────────────

test("watchdog (T21, P2 §10.4): a tick RETURNS the verdict and holds NO timer state", async () => {
  check("watchdog: nothing pending ⇒ no tick, no /live ask", (await watchdogTick(depsWith({ fetchLive: neverAsk }))) === null);
  ui.resync = { id: S, seen: true };
  check("watchdog: events flowed (seen) ⇒ premise retired ⇒ no tick", (await watchdogTick(depsWith({ fetchLive: neverAsk }))) === null);
  ui.sessionId = S;
  markPainted(S, delta(2));
  ui.resync = { id: S, seen: false };
  let res = await watchdogTick(depsWith({ fetchLive: async () => ({ active: true, seq: 6, events: [delta(3)] }) }));
  eq("watchdog: a live run ⇒ 'live' + the gapFill effect (the retry POST stays the timer OWNER's)", res.verdict, "live");
  eq("watchdog: effects ride along for the caller", res.effects.map((e) => e[0]), ["gapFill"]);
  ui.resync = { id: S, seen: false };
  res = await watchdogTick(depsWith({ fetchLive: async () => ({ active: false }) }));
  eq("watchdog: the server says over ⇒ 'terminal' (the owner clears its timer on this)", res.verdict, "terminal");
  check("watchdog: the terminal tick ran the heal — the resync is gone", ui.resync === null);
  ui.resync = { id: S, seen: false };
  res = await watchdogTick(depsWith({ fetchLive: async () => ({ missing: true }) }));
  eq("watchdog: 'missing' is the stop-probing signal", res.verdict, "missing");
  ui.resync = { id: S, seen: false };
  res = await watchdogTick(depsWith({ fetchLive: async () => null }));
  eq("watchdog: 'offline' means the next tick retries", res.verdict, "offline");
});

// ── P3: run_done ───────────────────────────────────────────────────────────

test("run_done heal (T17, P3): the event IS the verdict — /live is NEVER asked (run_done can't be served by it)", async () => {
  ui.resync = { id: S, seen: false };
  ui.sessionId = S;
  const asked = [];
  const deps = depsWith({ fetchLive: async (id, since) => { asked.push([id, since]); return { active: true, seq: 99, events: [] }; } });
  const r = fxSpy();
  applyEffects(await runDoneHeal({ type: "run_done", session_id: S, run_id: R1 }, deps), r.fx);
  eq("heal: ZERO /live asks — knownTerminal skips the fetch entirely", asked, []);
  eq("heal: the terminal stamp landed", ui.liveDone[S], R1);
  check("heal: the resync is cleared", ui.resync === null);
  eq("heal: row + composer, then the persisted refetch, picker reload LAST", r.calls,
    [["statusStop"], ["composer", "send"], ["rerenderHeal", S], ["pickerReload"]]);
});

test("heal off-screen (T17, P7): the state heals and the stamp lands, NOTHING renders", async () => {
  ui.resync = { id: S, seen: false };
  ui.sessionId = "B";
  const effects = await runDoneHeal({ type: "run_done", session_id: S, run_id: R1 }, depsWith({}));
  eq("heal: no rerenderHeal for a session that is not on screen", effects.filter((e) => e[0] === "rerenderHeal"), []);
  check("heal: still cleared, still stamped", ui.resync === null && ui.liveDone[S] === R1);
  eq("heal: the picker still reloads (the run's auto-name may have landed)", effects[effects.length - 1], ["pickerReload"]);
});

test("run_done bookkeeping (T18/T19, P3): watched closes the screen; other runs only stamp + reload", () => {
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(runDoneWatched({ type: "run_done", session_id: S, run_id: R1 }), r.fx);
  eq("watched: row stop, close, finalize, reload", r.calls, [["statusStop"], ["closeSegments"], ["finalizeBubble"], ["pickerReload"]]);
  eq("watched: stamped terminal", ui.liveDone[S], R1);
  ui.resync = { id: S, seen: true };
  check("watched: refused while a resync is pending for that run (that is T17)", runDoneWatched({ type: "run_done", session_id: S, run_id: R1 }) === null);
  ui.resync = null;
  check("watched: refused off-screen", runDoneWatched({ type: "run_done", session_id: "off", run_id: R2 }) === null);
  const r2 = fxSpy();
  ui.sessionId = "B";
  applyEffects(runDoneOther({ type: "run_done", session_id: S, run_id: R9 }), r2.fx);
  eq("other: picker freshness only", r2.calls, [["pickerReload"]]);
  eq("other: still stamped — late events of THAT run must never touch a newer slot", ui.liveDone[S], R9);
  const r3 = fxSpy();
  applyEffects(runDoneOther({ type: "run_done" }), r3.fx);
  eq("other: a name-less run_done still reloads the picker", r3.calls, [["pickerReload"]]);
  check("other: ... and stamps NOTHING (no id to stamp — the map stays clean)",
    !Object.prototype.hasOwnProperty.call(ui.liveDone, "undefined") && !Object.prototype.hasOwnProperty.call(ui.liveDone, "null"));
});

// ── P4: cold-open resume and the run-identity cold-join ────────────────────

test("cold-open (T20, P4): a primed screen asks, a settled screen does not", async () => {
  ui.sessionId = S;
  check("cold-open: no resync, no watermark, no slot ⇒ nothing to ask", (await visibleResume(depsWith({ fetchLive: neverAsk }))) === null);
  markPainted(S, delta(2)); // renderSession primed the slot during boot
  const f = fetcher({ active: false });
  const res = await visibleResume(depsWith(f));
  eq("cold-open: a stored watermark is enough to ask", res.verdict, "terminal");
  eq("cold-open: asked with the stored watermark", f.calls, [[S, 2]]);
  eq("cold-open heal with NOTHING armed still returns the composer (a turn the server forgot)", res.effects,
    [["statusStop"], ["composer", "send"], ["rerenderHeal", S]]);
  ui.liveSeq = {};
  slotFor(S); // an empty-tail join left a PROVISIONAL slot: runId null, no watermark
  const f2 = fetcher({ active: true, seq: 4, events: [delta(3, R2)] });
  const res2 = await visibleResume(depsWith(f2));
  eq("cold-open: a provisional slot asks too (no watermark, but a live run to ask about)", res2.verdict, "live");
  eq("cold-join: a batch MEETING a provisional slot cold-joins (identity rule)", res2.effects, [["rerenderCold", S, false]]);
});

test("cold-join (T27, P4 identity): a batch the slot cannot vouch for repairs via the ONE fetch — never a gap fill", async () => {
  markPainted(S, delta(3)); // the slot vouches for R1
  ui.sessionId = S;
  ui.resync = { id: S, seen: true };
  const f = fetcher({ active: true, seq: 2, events: [delta(1, R2), delta(2, R2)] }); // ANOTHER run's head
  const r = fxSpy();
  const res = await resyncVerdict(S, false, depsWith(f));
  applyEffects(res.effects, r.fx);
  eq("cold-join: verdict 'live' — the join IS the catch-up", res.verdict, "live");
  eq("cold-join: displacement proves the OLD run ended — stamped terminal", ui.liveDone[S], R1);
  check("cold-join: the armed resync is abandoned (its heal IS this join)", ui.resync === null);
  eq("cold-join: the scoped clear's row+composer, then the cold render (discard flag OFF for a mismatch)", r.calls,
    [["statusStop"], ["composer", "send"], ["rerenderCold", S, false]]);
  ui.resync = { id: S, seen: true };
  const f2 = fetcher({ active: true, seq: 5, events: [delta(4)] }); // batch names the TERMINAL R1
  const res2 = await resyncVerdict(S, false, depsWith(f2));
  eq("cold-join: a terminal batch renders WITH the discard flag (recovering=terminal)", res2.effects[res2.effects.length - 1], ["rerenderCold", S, true]);
  eq("cold-join: ... verdict still 'live' for the watchdog", res2.verdict, "live");
  ui.resync = { id: S, seen: true };
  const f3 = fetcher({ active: true, seq: 6, events: [delta(5, R2)] });
  const res3 = await resyncVerdict(S, false, depsWith({ ...f3, isReplaying: () => true }));
  eq("cold-join: mid-replay suppresses the render", res3.effects.filter((e) => e[0] === "rerenderCold").length, 0);
  eq("cold-join: ... the displacement stamp still lands", ui.liveDone[S], R1);
  check("cold-join: no gapFill was ever offered (the identity rule never fills blind)", isProvisionalSlot(S) === false && storedWatermark(S) === 3);
});

// ── BK-027: background (cron/watcher) runs in flight across a hide ─────────
// The page closes /api/events while hidden, so a background run's run_done
// can land unseen. The mark (T28 at run_start, cleared in T17–T19) makes the
// screen's run a resume subject; the render is T27's ordinary verdict — /live
// knows no background run, so it answers inactive and the heal is the atomic
// store render (offline ⇒ nothing, a live CHAT run ⇒ gap-fill as ever).

const RUN_DONE = (sid, extra) => ({ type: "run_done", session_id: sid, text: "", ...(extra || {}) });

test("bg run_start (T28): marks the run's session in flight; a name-less run_start marks nothing", () => {
  eq("bg start: returns NO effects (toast + auto-open stay the router's, T24)", bgRunStart({ type: "run_start", session_id: S, title: "Job" }), []);
  eq("bg start: the session is marked, its paint whole so far", ui.bgLive[S], { gap: false });
  check("bg start: a run_start without a session is refused and writes nothing",
    bgRunStart({ type: "run_start" }) === null && Object.keys(ui.bgLive).length === 1);
});

test("bg resume (T20→T27, BK-027): the screen's background run that finished while hidden gets the atomic store render", async () => {
  ui.sessionId = S;
  bgRunStart({ type: "run_start", session_id: S });
  const f = fetcher({ active: false }); // /live knows no background run: inactive
  const res = await visibleResume(depsWith(f));
  check("bg resume: a marked screen IS a resume subject (it used to ask nothing and stay frozen)", res !== null);
  eq("bg resume: asked /live ONCE, with no watermark (a background run has no seq)", f.calls, [[S, null]]);
  eq("bg resume: the verdict is T27's persisted heal", res && res.verdict, "terminal");
  eq("bg resume: ... whose ONE render is the atomic rerenderHeal of THIS screen", res && res.effects.filter((e) => e[0].startsWith("rerender")), [["rerenderHeal", S]]);
  eq("bg resume: the mark STAYS until run_done (the run may still be going) and now carries the hole", ui.bgLive[S], { gap: true });
});

test("run_done on screen stops the status row (BK-038): a background run sends no turn_complete", () => {
  // The orchestrator's bg runs emit deltas/tools/thinking but never turn_complete
  // — run_done is their ONLY terminal, so T18 must stop the row T06 painted, or
  // the last phase ("Working…") hangs until a chat switch's statusStop.
  ui.sessionId = S;
  const r = fxSpy();
  applyEffects(runDoneWatched(RUN_DONE(S)), r.fx);
  check("bg run_done on screen: the row stops", r.calls.some((c) => c[0] === "statusStop"));
  ui.liveTurn = { session: S, detached: false };
  const r2 = fxSpy();
  applyEffects(runDoneWatched(RUN_DONE(S)), r2.fx);
  check("own chat turn still streaming here: its own terminal stops the row, not run_done",
    !r2.calls.some((c) => c[0] === "statusStop"));
  ui.liveTurn = null;
});

test("bg still running at resume (BK-027): its run_done heals once more — the mid-run render lacks the in-flight head", async () => {
  ui.sessionId = S;
  bgRunStart({ type: "run_start", session_id: S });
  await visibleResume(depsWith(fetcher({ active: false })));
  const r = fxSpy();
  applyEffects(runDoneWatched(RUN_DONE(S)), r.fx);
  eq("bg heal: today's close/finalize, then the persisted heal, picker reload LAST (T17's order)", r.calls,
    [["statusStop"], ["closeSegments"], ["finalizeBubble"], ["rerenderHeal", S], ["pickerReload"]]);
  check("bg heal: run_done cleared the mark", !(S in ui.bgLive));
  bgRunStart({ type: "run_start", session_id: S }); // watched live start to finish, never hidden
  const r2 = fxSpy();
  applyEffects(runDoneWatched(RUN_DONE(S)), r2.fx);
  eq("bg heal: a run watched WHOLE costs no fetch — today's T18 exactly", r2.calls,
    [["statusStop"], ["closeSegments"], ["finalizeBubble"], ["pickerReload"]]);
  check("bg heal: ... and its mark is cleared too", !(S in ui.bgLive));
});

test("bg off-screen (BK-027): the screen is another chat — nothing asked, nothing rendered; T19 still clears the mark", async () => {
  ui.sessionId = "B";
  bgRunStart({ type: "run_start", session_id: S });
  check("bg off-screen: a settled screen asks NOTHING (the mark is not the screen's)",
    (await visibleResume(depsWith({ fetchLive: neverAsk }))) === null);
  eq("bg off-screen: the hide still marked the run's hole (opened later mid-run, its run_done heals)", ui.bgLive[S], { gap: true });
  const r = fxSpy();
  applyEffects(runDoneWatched(RUN_DONE(S)) || runDoneOther(RUN_DONE(S)), r.fx);
  eq("bg off-screen: run_done is T19 — picker freshness only, no render", r.calls, [["pickerReload"]]);
  check("bg off-screen: ... and the mark is gone", !(S in ui.bgLive));
});

test("bg several (BK-027): marks are per session — resume renders only the screen's, a run_done clears only its own", async () => {
  const S2 = "unit-run-2";
  bgRunStart({ type: "run_start", session_id: S });
  bgRunStart({ type: "run_start", session_id: S2 });
  ui.sessionId = S2;
  const f = fetcher({ active: false });
  const res = await visibleResume(depsWith(f));
  eq("bg several: ONE ask, for the screen's run only", f.calls, [[S2, null]]);
  eq("bg several: ONE render, of the screen", res.effects.filter((e) => e[0].startsWith("rerender")), [["rerenderHeal", S2]]);
  check("bg several: both runs carry the hole", ui.bgLive[S].gap === true && ui.bgLive[S2].gap === true);
  runDoneOther(RUN_DONE(S));
  check("bg several: S's run_done cleared S alone", !(S in ui.bgLive) && ui.bgLive[S2]?.gap === true);
});

test("bg no double render (BK-027): a chat path already handling the screen gets ONE verdict, and a live chat run still gap-fills", async () => {
  ui.sessionId = S;
  ui.resync = { id: S, seen: false };
  ui.bgLive[S] = { gap: false }; // a stale mark beside the chat's own recovery
  const f = fetcher({ active: false });
  const res = await visibleResume(depsWith(f));
  eq("bg dedupe: resync + mark on the same session ⇒ ONE /live ask", f.calls.length, 1);
  eq("bg dedupe: ⇒ ONE render", res.effects.filter((e) => e[0].startsWith("rerender")), [["rerenderHeal", S]]);
  ui.resync = null;
  markPainted(S, delta(3)); // a chat stream on this screen painted through seq 3
  const f2 = fetcher({ active: true, seq: 4, events: [delta(4)] });
  const res2 = await visibleResume(depsWith(f2));
  eq("bg dedupe: watermark + mark ⇒ ONE ask, at the CHAT's watermark", f2.calls, [[S, 3]]);
  // Still an EXACT list (a rerender here would fail it). BK-036 added the
  // tail: nothing holds Stop for this live chat run (no liveTurn/resync/
  // observed), so T33's backstop arms it — senderstop.test.js owns that rule.
  eq("bg dedupe: a live chat run on a marked screen gap-fills — the mark never buys a mid-call store render (2026-10-17)",
    res2.effects.map((e) => e[0]), ["gapFill", "composer", "observeWatch"]);
});

test("bg + another chat's resync (BK-027): BOTH subjects get their verdict, each exactly once", async () => {
  ui.resync = { id: "Y", seen: false }; // a chat stream died off-screen
  ui.sessionId = S;
  bgRunStart({ type: "run_start", session_id: S });
  const f = fetcher({ active: false });
  const res = await visibleResume(depsWith(f));
  eq("bg + resync: the resync's run first, then the screen's background run", f.calls, [["Y", null], [S, null]]);
  eq("bg + resync: only the SCREEN renders (Y is off screen)", res.effects.filter((e) => e[0].startsWith("rerender")), [["rerenderHeal", S]]);
  check("bg + resync: Y's resync is healed (cleared)", ui.resync === null);
  eq("bg + resync: the verdict reported is the resync's (the first subject)", res.verdict, "terminal");
});

test("bg offline at resume (BK-027): no render (a failed /messages would blank the screen); the mark waits for the next resume", async () => {
  ui.sessionId = S;
  bgRunStart({ type: "run_start", session_id: S });
  const res = await visibleResume(depsWith(fetcher(null)));
  eq("bg offline: verdict offline", res.verdict, "offline");
  eq("bg offline: no effects at all", res.effects, []);
  check("bg offline: the mark survives", ui.bgLive[S]?.gap === true);
});

// ── T22 / D4: the ONE clear, scoped in writes AND effects ──────────────────

test("the ONE clear (T22, D4): a survivor's liveTurn, status row, and Stop button all live on", () => {
  ui.resync = { id: "A", seen: true };
  ui.liveTurn = { session: "B" }; // the user started B while A detached
  ui.sessionId = "B";
  const r = fxSpy();
  applyEffects(abandonRun("A"), r.fx); // A's run_done heal routes here
  check("clear: A's resync cleared", ui.resync === null);
  check("clear: B's liveTurn SURVIVES (today's abandonPendingRun nuked it mid-stream)", ui.liveTurn?.session === "B");
  eq("clear: NO row stop, NO composer hand-back — B's row and Stop belong to B", r.calls, []);
  const r2 = fxSpy();
  applyEffects(abandonRun("C"), r2.fx);
  check("clear: a stranger's clear changes nothing", ui.liveTurn?.session === "B");
  eq("clear: ... and fires nothing", r2.calls, []);
  const r3 = fxSpy();
  applyEffects(abandonRun("B"), r3.fx);
  check("clear: B's own clear retires its liveTurn", ui.liveTurn === null);
  eq("clear: ... and only THEN the row + composer (nothing live remains)", r3.calls, [["statusStop"], ["composer", "send"]]);
});

// ── T04: Stop ──────────────────────────────────────────────────────────────

test("Stop (T04, P5): the live turn first, the detached run as fallback, silence when neither", () => {
  check("stop: nothing live ⇒ no request", stopRequest() === null);
  ui.liveTurn = { session: "B" };
  ui.resync = { id: "A", seen: true };
  const r = fxSpy();
  applyEffects(stopRequest(), r.fx);
  eq("stop: targets the live turn (the fresher claim on Stop)", r.calls, [["netStop", "B"]]);
  ui.liveTurn = null;
  const r2 = fxSpy();
  applyEffects(stopRequest(), r2.fx);
  eq("stop: a detached run is still Stop-able via resync.id (the server run lives)", r2.calls, [["netStop", "A"]]);
  check("stop: state untouched — the terminal event drives, not the click", ui.resync?.id === "A");
  ui.resync = null;
  ui.liveTurn = { session: null }; // a new chat before its session frame
  check("stop: no id yet ⇒ nothing to stop (matching app.js today)", stopRequest() === null);
});

// ── D6 storage + the effect dispatcher ─────────────────────────────────────

test("storage trio (D6): slotFor owns entries, noteRunTerminal owns the stamp", () => {
  const s = slotFor(S);
  eq("storage: a fresh slot is {runId:null, water:0, painted:[]} (the provisional shape, byte-identical to replay's)", s, { runId: null, water: 0, painted: [] });
  check("storage: slotFor is get-or-create (same object back)", slotFor(S) === s);
  dropSlot(S);
  check("storage: dropSlot forgets the entry (marks never outlive their DOM)", slotOf(S) === undefined);
  noteRunTerminal(S, R1);
  check("storage: noteRunTerminal stamps the terminal run (replay's guard sees it)", ui.liveDone[S] === R1 && isTerminalRun(S, R1) === true);
  noteRunTerminal(S, null);
  check("storage: ... and ignores a null run id (the stamp survives)", ui.liveDone[S] === R1);
});

test("effects dispatcher: pure over the fx map, loud about drift", () => {
  const r = fxSpy();
  applyEffects(null, r.fx);
  applyEffects([], r.fx);
  eq("effects: null/empty are clean no-ops", r.calls, []);
  applyEffects([["statusStop"], ["composer", "send"]], r.fx);
  eq("effects: tokens dispatch in order with their args", r.calls, [["statusStop"], ["composer", "send"]]);
  let threw = false;
  try { applyEffects([["noSuchEffect"]], r.fx); } catch (e) { threw = /noSuchEffect/.test(String((e && e.message) || e)); }
  check("effects: an unbound token THROWS — wiring drift is loud, never a silent no-op", threw);
});

test("effects surface: EVERY token the table can emit is in the design's fx dictionary (Step 4 builds exactly this map)", async () => {
  const r = fxSpy();
  const deps = depsWith({});
  const emitted = [];
  const run = (effects) => { emitted.push(...(effects || [])); applyEffects(effects, r.fx); };
  ui.sessionId = S;
  run(submit("t", [], undefined));
  run(sessionAnnounce({ session_id: S }));
  ui.sessionId = "gone";
  run(screenLeave());
  run(stopRequest());
  ui.sessionId = S;
  ui.liveTurn = { session: S }; // un-latch: the tail transitions paint un-suppressed
  run(paintChat({ type: "tool_call" }, S, deps));
  run(titleSeen({ title: "T" }, S));
  run(turnComplete({}));
  run(runFinish({ type: "done", title: "x" }));
  const smoke = [...new Set(emitted.map((e) => e[0]))].sort();
  eq("effects: no token outside the §2.6 fx dictionary (dispatch smoke)", smoke.filter((n) => !FX_NAMES.includes(n)), []);
  check("effects: the smoke covered a dozen+ distinct tokens (got " + smoke.length + ")", smoke.length >= 12);
  // The smoke drives a subset. The WIRING claim is about the whole table, so
  // scan the module source: every effect literal must be a dictionary token,
  // or Step 4 ships a transition whose fx binding doesn't exist — silently
  // until that event fires (applyEffects throws at runtime, no test sees it).
  // BK-034: the table spans runstate.js + resync.js (the verdict half) — scan both.
  const src = (await (await fetch("/runstate.js")).text()) + (await (await fetch("/resync.js")).text());
  const scanned = [...new Set([...src.matchAll(/\["([A-Za-z][A-Za-z0-9]*)"/g)].map((m) => m[1]))].sort();
  check("effects: the source scan found the whole table's surface (a broken scan must not pass by finding nothing; got " + scanned.length + ")", scanned.length >= 23);
  eq("effects: no token OUTSIDE the dictionary, across every transition (source scan)", scanned.filter((n) => !FX_NAMES.includes(n)), []);
});

// D1 (frontend-smoothness step 4): follow.js's readerOpened pauses following
// only while screenRunLive() — any run source naming the ON-SCREEN session.
test("D1 screenRunLive: each run source naming the screen is live; naming another session, latched, or none is not", () => {
  const O = "other-d1";
  const snap = () => JSON.stringify(ui);
  ui.sessionId = S;
  check("D1: no run source at all → not live (an open just opens, as before D1)", screenRunLive() === false);
  const sources = {
    "own stream (screenOwns)": () => { ui.liveTurn = { session: S }; },
    "detached resync": () => { ui.resync = { id: S, seen: true }; },
    "observed run (BK-034)": () => { ui.observed = { id: S, runId: "r" }; },
    "background run mark (T28)": () => { ui.bgLive[S] = { gap: false }; },
  };
  const elsewhere = {
    "own stream": () => { ui.liveTurn = { session: O }; },
    "detached resync": () => { ui.resync = { id: O, seen: true }; },
    "observed run": () => { ui.observed = { id: O, runId: "r" }; },
    "background run mark": () => { ui.bgLive[O] = { gap: false }; },
  };
  const clear = () => { ui.liveTurn = null; ui.resync = null; ui.observed = null; ui.bgLive = {}; };
  for (const [name, arm] of Object.entries(sources)) {
    clear(); arm();
    const before = snap();
    check("D1: " + name + " on the screen's session → live", screenRunLive() === true);
    check("D1: " + name + " — the predicate writes nothing", snap() === before);
  }
  // Every subset of the four sources armed on ANOTHER session: never live.
  const names = Object.keys(elsewhere);
  for (let mask = 1; mask < 1 << names.length; mask++) {
    clear();
    names.forEach((n, i) => { if (mask & (1 << i)) elsewhere[n](); });
    check("D1: sources on another session only (mask " + mask + ") → not live", screenRunLive() === false);
  }
  clear();
  ui.liveTurn = { session: O, detached: true }; // latched: the screen left the stream
  check("D1: a latched own stream (screen moved away) → not live", screenRunLive() === false);
  clear();
  ui.sessionId = null; ui.liveTurn = { session: null }; // new chat, pre-`session` frame
  check("D1: a new chat's own stream before its session frame → live", screenRunLive() === true);
  clear();
  ui.bgLive[S] = { gap: false };
  check("D1: a null screen never matches a session's background mark", screenRunLive() === false);
});
