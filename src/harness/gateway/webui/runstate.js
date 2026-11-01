/* runstate.js — ONE owner of the chat-run lifecycle (BK-019, design
   scratch/plans/bk-019-design.md §2/§3; T01–T27 as revised by §10; BK-027's
   T28/T29). app.js (chat SSE) and events.js (/api/events) route run events
   through these transitions and dispatch the effects on their own fx maps.

   THE FOUR FIELDS (declared in state.js, armed directly by fixtures — that is
   the test seam) and BK-027's ui.bgLive are written ONLY here. The PHASE is
   DERIVED from the four, never stored: derivePhase(resync, liveTurn) is the
   whole truth; resync.js's phaseNow(id) adds the transient "resyncing"
   while a resyncVerdict fetch is in flight. No transition may branch on a
   phase — a phase gate is a second source of truth, the drift class this
   module exists to kill. latched() is a FACET (STREAMING⊥screen)
   that gates PAINTS, never lifecycle; terminality that persists lives per-RUN
   in ui.liveDone, not in a phase.

   The VERDICT half (T27 resyncVerdict and its askers T17/T20/T21) lives in
   resync.js, which imports this module — never the reverse (DAG; BK-034).

   Pure by construction: imports state.js ONLY. Renderers are reached through
   EFFECTS the caller dispatches with applyEffects(effects, fx) on its own fx
   map; replay.js's arithmetic and the /live fetch are INJECTED per call as a
   `deps` bag (D6/D7 — this is what lets the DOM-free jsunit tier drive it).
   An effect is [name, ...args]; names come from the design's fx dictionary.
   A transition whose guard fails returns null and writes NOTHING; one that
   fires returns its effect array (possibly empty) and performs its writes.

   Not controller-owned (plan: non-chat duties stay put, design §5.2/T23–T25):
   run toast + working indicator (T24 run_start rules), EventSource connect/
   disconnect, sessions.js navigation (T23 sessionOpen — screen identity),
   visibilityHidden's socket close, and the tlog plumbing — the load-bearing
   names ("catchingUp", "run_done:refetch", D8) stay the ROUTER's call at the
   T16/T17 sites. Reopen-mid-run lifts the latch (T35 reopenResume, BK-024 —
   D9's freeze retired): the reopened chat streams again. */
import { ui } from "./state.js";

// ── derived predicates (every reader reads ui fresh at call time — fixtures
//    arm the fields directly, so nothing may cache a field's value) ────────

// The pure phase: what the fields say, nothing else.
export function derivePhase(resync, liveTurn) {
  if (resync) return "detached";
  if (liveTurn) return "streaming";
  return "idle";
}

// phaseNow (the transient "resyncing" projection) lives with the verdicts
// in resync.js, beside the in-flight set it reads.

// The screen's stream owns the screen (true pre-`session` for a new chat:
// null === null). The divergence test that arms the latch — never the latch.
export function screenOwns() {
  return !!ui.liveTurn && ui.liveTurn.session === ui.sessionId;
}

// D1 (frontend-smoothness step 4; follow.js readerOpened): more of the
// on-screen session is still coming — ANY run source naming the screen: this
// page's own stream (screenOwns; false once latched away), its detached
// resync, another device's observed run, a background run's T28 mark (a mark
// whose run_done was missed outlives its run — then D1 pauses for nothing).
export function screenRunLive() {
  const id = ui.sessionId;
  return screenOwns() || pendingFor(id)
    || (id != null && (ui.observed?.id === id || !!ui.bgLive[id]));
}

// The for-good stream latch (D3): gates paints (T05/T08–T11), not lifecycle.
export function latched() {
  return ui.liveTurn?.detached === true;
}

export function pendingFor(id) {
  return id != null && ui.resync?.id === id;
}

// BK-034: the observed run (another device's) is the last fallback.
export function stopTarget() {
  return ui.liveTurn?.session ?? ui.resync?.id ?? ui.observed?.id ?? null;
}

// "Screen match" per the design's §3 header: events-stream frames name their
// session; chat-SSE frames carry no session_id (the stream IS the session), so
// their test is the latch.
function screenMatch(ev) {
  return ev.session_id != null ? ev.session_id === ui.sessionId : !latched();
}

// ── slot storage (D6: this module owns the MAPS — keys, entries, the terminal
//    stamp; each slot's run identity + dedupe math stay replay.js's) ────────

export function slotFor(id) {
  return (ui.liveSeq[id] = ui.liveSeq[id] || { runId: null, water: 0, painted: [] });
}

export function dropSlot(id) {
  delete ui.liveSeq[id];
}

// The identity rule's terminal stamp (replay.js's markRunDone delegates here
// in Step 4): run_done names the run that ended; displacement proves an older
// one ended. Late events of a stamped run never touch the slot.
export function noteRunTerminal(id, runId) {
  if (runId == null) return;
  ui.liveDone[id] = runId;
}

// ── effect dispatch (pure over the injected fx map; a missing binding is
//    wiring drift and must be loud, never a silent no-op) ──────────────────

export function applyEffects(effects, fx) {
  for (const [name, ...args] of effects || []) {
    const fn = fx ? fx[name] : null;
    if (typeof fn !== "function") throw new Error("runstate: no fx binding for effect '" + name + "'");
    fn(...args);
  }
}

// ── T01–T11: the healthy stream ───────────────────────────────────────────

// T01 submit (mode=send; the router has checked attachments). `model` feeds
// the status lane. D5: every terminal/failure transition clears liveTurn, so
// a leaked STREAMING witness cannot outlive a run that never started.
export function submit(text, atts, model) {
  ui.liveTurn = { session: ui.sessionId };
  return [["composer", "stop"], ["statusLane", model], ["statusStart"], ["paintUser", text, atts]];
}

// T02 `session` frame (chat): the frame names the RUN's session — the liveTurn
// write is UNCONDITIONAL (§10.2: gated on screen ownership, a switch between
// submit and the frame arms T15 with resync.id null, killing Stop/recovery).
// ui.sessionId/picker are screen identity — only while the screen owns.
// ctx.started (per-stream confirmation) is the caller's, NOT liveTurn's (§10.1).
export function sessionAnnounce(ev) {
  const owns = screenOwns(); // BEFORE the write
  if (ui.liveTurn) ui.liveTurn.session = ev.session_id;
  if (!owns) return [];
  ui.sessionId = ev.session_id;
  return [["pickerOpenNew", ev.session_id]];
}

// T03 screenLeave (the router calls it before every chat event, as today's
// divergence check): the for-good latch arms once; the row belongs to whatever
// session is on screen now.
export function screenLeave() {
  if (!ui.liveTurn || latched() || ui.sessionId === ui.liveTurn.session) return null;
  ui.liveTurn.detached = true;
  return [["statusStop"]];
}

// T35 reopenResume (BK-024; renderSession's entry hook): back on the latched
// stream's session — un-latch. The render is buffering, so the next events
// flush onto the replay; those dropped while latched are in the /messages join.
export function reopenResume(id) {
  if (latched() && id != null && id === ui.liveTurn.session) ui.liveTurn.detached = false;
  return null;
}

// T04 Stop click: no UI change — the terminal event drives; the request goes
// to the live turn, falling back to the detached run.
export function stopRequest() {
  const id = stopTarget();
  return id == null ? null : [["netStop", id]];
}

// The shared paint tail (T05/T06): status row + transcript, approvals poll on
// tool_call. Status-only frames (model_wait, compaction_start, notices) ride
// this tail too (§10.11) — a content-only reading would silently stop the pill.
function paintTail(ev) {
  const effects = [["statusEvent", ev]];
  if (ev.type === "tool_call") effects.push(["pollApprovalsSoon"]);
  effects.push(["paint", ev]);
  return effects;
}

// T05 the chat-SSE tail. A latched stream paints nothing and marks nothing:
// its session's DOM is gone; reopening re-renders and un-latches (T35). The
// approvals poll still runs: an off-screen run's tool_call refreshes the card.
// Mid-replay the paint is BUFFERED: the flush marks it (marked now, it skips it).
export function paintChat(ev, streamSession, deps) {
  if (latched()) return ev.type === "tool_call" ? [["pollApprovalsSoon"]] : null;
  if (deps.alreadyPainted(streamSession, ev)) return null;
  if (!deps.isReplaying()) deps.markPainted(streamSession, ev);
  return paintTail(ev);
}

// T06 the same set rerouted to /api/events — only for the session on screen.
// BK-034: a seq-stamped (chat-run) event other than `done` also proves a live
// run on screen — the third page that opened the chat before the run began.
export function paintRerouted(ev, deps) {
  if (ev.session_id !== ui.sessionId || deps.alreadyPainted(ev.session_id, ev)) return null;
  if (!deps.isReplaying()) deps.markPainted(ev.session_id, ev); // T05's mid-replay rule
  const arm = ev.seq != null && ev.type !== "done" ? observe(ev.session_id, ev.run_id ?? null, true) : [];
  return [...paintTail(ev), ...arm];
}

// T07 `title` (either transport; works POST-terminal — the per-run liveDone
// guard lives in replay's slot refusal, and this relabel never touches a slot).
// `runSession` is the RUN's session (chat: the stream's ctx.session; events:
// ev.session_id) — never ui.sessionId, or a post-switch title relabels the
// wrong option (§10.6).
export function titleSeen(ev, runSession) {
  return ev.title ? [["pickerRelabel", runSession, ev.title]] : [];
}

// T08 `turn_complete`: answer landed; naming may still run. Latched chat
// frames are screen-mismatched by definition — today's detached statusStop
// would stop the VISIBLE session's row; the design's screen-match guard fixes it.
export function turnComplete(ev) {
  if (!screenMatch(ev)) return null;
  return [["statusStop"], ["closeSegments"]];
}

// T09 `done` (chat). Terminality persists in liveDone (D2), not here: the
// fields land back at idle. Transcript effects only while the stream paints;
// the state clear is NOT suppressed. Row + composer hand back (latched
// included): today's `done` left the composer on stop and the stream-end tail
// (dataset-mode check) fired statusStop+send for latched and un-latched alike
// — with the tail field-gated (T14 reads liveTurn, which this clear nulls),
// the hand-back must ride HERE or `done` never frees Stop. The tail's
// `!ui.resync` conjunct is KEPT: the composer is the only Stop affordance, a
// chat stream can only open while the composer reads send, and a background
// run's stopped/error hands it back beside an armed resync — so without this
// guard a `done` in another chat would disarm Stop over a live detached run
// (the D4/T22 rule: hand back only when nothing live remains).
export function runFinish(ev) {
  const paints = !latched(); // read BEFORE the clear
  const lone = !ui.resync && !ui.observed; // BK-034: an observed run keeps Stop too
  ui.liveTurn = null;
  const effects = [];
  if (ev.title) effects.push(["pickerReload"]);
  if (paints) effects.push(["closeSegments"], ["finalizeBubble"]);
  if (lone) effects.push(["statusStop"], ["composer", "send"]);
  return effects;
}

// T10/T11's shared clear. BK-036 (e): an events frame ends ITS session's
// turn only — this page's own turn latched on another session keeps its
// liveTurn, and with it Stop (D4: hand back only when nothing lives).
function endTurn(ev) {
  if (ev.session_id != null) observeEnd(ev.session_id, ev.run_id);
  if (ev.session_id == null || ui.liveTurn?.session === ev.session_id) ui.liveTurn = null;
  return ui.liveTurn ? [] : sendUnlessObserved();
}

// T10 `stopped` (chat: — / events: screen match).
export function runStopped(ev) {
  const paints = screenMatch(ev);
  if (ev.session_id != null && !paints) return null;
  const effects = [["statusStop"], ...endTurn(ev)];
  if (paints) effects.push(["closeSegments"], ["finalizeBubble"], ["systemMsg", "Stopped"]);
  return effects;
}

// T11 `error` (chat: — / events: screen match).
export function runError(ev) {
  const paints = screenMatch(ev);
  if (ev.session_id != null && !paints) return null;
  const effects = [["statusStop"], ...endTurn(ev)];
  if (paints) effects.push(["errorMsg", ev.error]);
  return effects;
}

// ── T12–T16: failed sends, the drop, the reroute's first word ─────────────

// T12 !resp.ok (409 busy etc.). D5: clear the armed-but-never-started turn.
export function sendRejected(detail) {
  ui.liveTurn = null;
  return [["errorMsg", detail], ["statusStop"], ["composer", ui.resync || ui.observed ? "stop" : "send"]];
}

// T13 fetch throw BEFORE the server confirmed the run (§10.1: the per-stream
// `started` flag is the transport's — liveTurn.session cannot stand in for it,
// T01 arms it at submit). turnStarted ⇒ the router takes T15 instead.
export function sendFailed(message, turnStarted) {
  if (turnStarted) return null;
  ui.liveTurn = null; // D5
  return [["errorMsg", message], ["statusStop"], ...sendUnlessObserved()];
}

// T14 reader closed with no terminal event. Guard over FIELDS (§10.5): every
// terminal (T09 carries the old tail's hand-back) clears liveTurn, so
// liveTurn ∧ !resync IS the old dataset test, minus the DOM read. BK-036: once
// started, a clean end proves nothing (iOS/a proxy closes a suspended stream
// error-free while the run goes on — read as a restart it dropped Stop for a
// whole tool call): T15's state, /live decides. Never started: hand back.
export function streamEndSilent(streamSession, turnStarted) {
  if (!ui.liveTurn || ui.resync) return null;
  if (turnStarted) return socketDied(streamSession, true);
  ui.liveTurn = null;
  return [["statusStop"], ...sendUnlessObserved()];
}

// T15 transport death AFTER the run started: a recoverable state, not an
// error line — resync arms with the STREAM's session (ctx.session, === 
// liveTurn.session once T02 ran), never the screen's; liveTurn is KEPT so
// Stop stays armed. The /detach POST + watchdog arm are effects; the timer
// itself lives with its owner (events.js).
export function socketDied(streamSession, turnStarted) {
  if (!turnStarted) return null; // the router splits on the transport flag (§10.1)
  ui.resync = { id: streamSession, seen: false };
  return [["statusWarn", "Reconnecting · still working"], ["netDetach", streamSession], ["armWatchdog"]];
}

// T16 the FIRST rerouted event for the pending run flips `seen` ONCE — the
// status row's one-time "Catching up…" — and thereby retires the watchdog's
// premise (its timer reads seen). Screen match is a guard (§10.10: without it
// an off-screen reroute paints the visible session's row and retires another
// resync's premise early). The router keeps the tlog("catchingUp", …) call (D8).
export function catchingUp(ev) {
  if (ev.session_id !== ui.sessionId) return null;
  if (!pendingFor(ev.session_id) || ui.resync.seen) return null;
  ui.resync.seen = true;
  return [["statusPhase", "Catching up…", { waiting: true }]];
}

// ── T18–T22: run_done, the ONE clear (T17/T20/T21 + T27 live in resync.js) ──

// T18 run_done on screen, recovery not pending (+ T29's heal). BK-038: a bg run's ONLY row stop.
export function runDoneWatched(ev) {
  if (pendingFor(ev.session_id) || ev.session_id !== ui.sessionId) return null;
  if (ev.session_id) noteRunTerminal(ev.session_id, ev.run_id);
  const heal = ui.bgLive[ev.session_id]?.gap ? [["rerenderHeal", ev.session_id]] : [];
  delete ui.bgLive[ev.session_id];
  const end = observeEnd(ev.session_id, ev.run_id) || []; // BK-034: the observed run finished
  const row = ui.liveTurn?.session === ev.session_id ? [] : [["statusStop"]];
  return [...row, ["closeSegments"], ["finalizeBubble"], ...heal, ["pickerReload"], ...end];
}

// T19 run_done for any other run: the stamp + picker freshness only.
export function runDoneOther(ev) {
  if (pendingFor(ev.session_id) || ev.session_id === ui.sessionId) return null;
  if (ev.session_id) noteRunTerminal(ev.session_id, ev.run_id);
  delete ui.bgLive[ev.session_id]; // T29
  return [["pickerReload"]];
}

// T28 background run_start (toast/auto-open stay T24's; T29 = the T17–T19 clear).
export function bgRunStart(ev) {
  if (ev.session_id == null) return null;
  ui.bgLive[ev.session_id] = { gap: false };
  return [];
}

// The bgLive writes resync.js's T17/T20 make — HERE, so this module stays the
// one writer of the field: T29's clear, and T20's "hidden ⇒ every mark has a hole".
export function bgLiveClear(id) { delete ui.bgLive[id]; }
export function bgLiveGapAll() { for (const sid in ui.bgLive) ui.bgLive[sid].gap = true; }

// T22 the ONE owner of the clear, SCOPED to its run (D4): resync clears iff
// it is this run's; liveTurn iff it belongs to this run — and the status row
// and composer (global singletons; the composer is the only Stop affordance)
// are touched ONLY when the scoped clear leaves nothing live (the old
// unconditional clear let detached run A's run_done clobber B's turn + row).
export function abandonRun(id) {
  if (ui.resync && ui.resync.id === id) ui.resync = null;
  if (ui.liveTurn && ui.liveTurn.session === id) ui.liveTurn = null;
  if (ui.observed && ui.observed.id === id) ui.observed = null; // BK-034
  return (!ui.liveTurn && !ui.resync && !ui.observed) ? [["statusStop"], ["composer", "send"]] : [];
}

// ── T30/T31 (BK-034): Stop for a run this page did NOT start ──────────────
// The composer is the ONLY Stop affordance, and before BK-034 it armed only
// from this page's own send (T01) or its own resync — so a phone that opened
// a reply the iPad started painted the stream and never showed Stop.
// ui.observed is the third source: armed from the screen's live join (T31)
// or a rerouted run event (T06), cleared by the run's terminal (T10/T11/T18/
// T22 — the T27 heal clears through T22) or by the screen leaving (T30).
// While armed, every OWN-stream hand-back (T09–T14) withholds Send: the
// observed run still needs its Stop (D4: hand back only when nothing lives).

function sendUnlessObserved() {
  return ui.observed ? [] : [["composer", "send"]];
}

// Arm. Own runs never observe — their Stop is liveTurn's/resync's — and a
// run already stamped over (run_done passed it) is not live. `quiet`: a
// rerouted event re-arming the SAME session adopts the run's id with no
// effects (setSendMode per delta would re-measure the composer per token);
// a join (once per render) always re-states Stop and the watch.
function observe(id, runId, quiet) {
  if (id == null || ui.liveTurn?.session === id || ui.resync?.id === id) return [];
  if (runId != null && ui.liveDone[id] === runId) return [];
  const had = ui.observed?.id === id;
  ui.observed = { id, runId: runId ?? (had ? ui.observed.runId : null) };
  return had && quiet ? [] : [["composer", "stop"], ["observeWatch"]];
}

// The shared clear: only the observed run's own terminal (a frame naming an
// EARLIER run of the session — serial runs — must not disarm a newer one).
function observeEnd(id, runId) {
  const o = ui.observed;
  if (!o || o.id !== id || (runId != null && o.runId != null && runId !== o.runId)) return null;
  ui.observed = null;
  return ui.liveTurn || ui.resync ? [] : [["composer", "send"]];
}

// T33 (BK-036 backstop, resync.js's gap fill): T27's "live" for the screen is
// the server's word a chat run lives here — arm Stop if no source holds it.
export function observeLive(id, runId) {
  return id != null && id === ui.sessionId ? observe(id, runId, true) : [];
}

// T30 the screen is moving to `id` (renderSession's entry; events.js's
// openLiveSession): an observed run of ANOTHER session is no longer on screen.
export function observeLeave(id) {
  const o = ui.observed;
  if (!o || o.id === id) return null;
  return observeEnd(o.id, null);
}

// T31 the swap's /messages live join (renderSession, after consumeLiveJoin):
// `live` non-null = a chat run is live on this session (gateway/live.py's
// one predicate); null = none (clear); undefined = the fetch failed or the
// server predates the field — unknown, change nothing. (Its row: T34.)
export function observeJoin(id, live) {
  if (id == null || id !== ui.sessionId || live === undefined) return null;
  if (!live) return observeEnd(id, null);
  const tail = live.events || [];
  return observe(id, tail.length ? tail[tail.length - 1].run_id ?? null : null, false);
}

