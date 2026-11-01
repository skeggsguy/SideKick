/* replay.js — seq bookkeeping for the resumed screen: the per-session dedupe
   slot, the /live gap-fill, and the join of the /messages live tail.

   pwa-accurate-resume Step 6. The resume paths (events.js) and the paint
   paths (transcript.js, app.js) share ONE question: "has this event already
   reached the screen?" Two independent paints of one event double-paint its
   word, and the resume paths fetch while the live sinks may be painting the
   same window right now — the A/B/E/G/H reroute race, and /live's own
   overlap with events the chat SSE already delivered. The answer is a slot:

     ui.liveSeq[id] = { runId, water, painted: [[lo, hi], ...] }

   `painted` is the merged set of event seqs whose VISIBLE effect is on the
   screen — merged on mark so the membership test stays short even after a
   thousand deltas. `water` is the REQUEST watermark ("send me everything
   above this"), advanced only by `seq` values the server issued: a /live
   response's watermark, a sink event's number, or the /messages live block's
   own `seq`.

   Seq space is RUN-LOCAL: seq restarts at 1 for each new run on the session
   (routes/streaming.py), so a watermark belongs to its run_id. Chat runs are
   serial per session (the 409 guard), so at most one slot is live per session.

   THE IDENTITY RULE — one sentence, three call sites: a slot vouches for
   exactly ONE run, and batch resync may only paint what the slot vouches for.
   - A /live batch naming the slot's run: gap-fill, deduped as below.
   - A batch naming a DIFFERENT run (the run I was watching ended; another
     started — possibly on another device), a batch meeting a PROVISIONAL
     slot (an empty-tail join, whose run the payload never named), or a batch
     naming a TERMINAL run (run_done passed it): the numbers restarted and my
     head belongs to the old run — gap-fill would either skip the new run's
     head (my watermark is out of its space) or repaint over my own. The
     sanctioned repair is the one-fetch live join: runstate T27's verdictColdJoin →
      the `rerenderCold` effect (renderSession) — re-derives rows + tail and
      re-primes the slot.
   - A slot reset MARKS THE REPLACED RUN TERMINAL (syncRun, the choke point —
     a run change means the old run is over, seen run_done or not); a
     TERMINAL run's events never touch the slot: the carried "stopped detached
     run publishes `title` after run_done" case must relabel the picker but
     must NOT reset the newer run's marks/watermark. Sinks still paint their
     own events (incremental truth); only the slot is guarded.

   THE WATERMARK IS THE SERVER's `seq`, NEVER a `since` (the plan's
   invariant): `since` is a JOIN boundary — the run's persisted_seq, where
   the persisted half ends and the ring tail begins. A client that sent
   since=N and watermarked at N refetches the whole tail forever. `seq` is
   the run's last issued number: the response's own "caught up to here".
   The two-sinces rule lives in gateway/live.py; the client obeys it from
   the field names, and `since` reaches this module only as the head-marking
   boundary in consumeLiveJoin — never as a watermark.

   A seq-less event (ev.seq == null) is a background-stream event — the
   server stamps run events with seq, background ones carry none — and
   passes through every gate here: they are idempotent renders keyed by
   type/notice, and deduping them would drop second occurrences (the
   thinking-pause notice) that are meant to repaint.

   Why the scalar "seq <= watermark means seen" shortcut is NOT the dedupe:
   it is sound only while the watermark advances event-by-event in order.
   The chat SSE can paint events ABOVE the /messages batch's join boundary
   before the fetch returns — the painted set reaches past `since` while the
   rows only cover up to it — and the gap fill arrives with the live sink
   racing it. Intervals close both holes with no new state anywhere else.

   Both painters arrive as the injected `paint` (transcript's `paintStream`):
   replay.js must not import transcript.js back (transcript imports this
   module — the reverse import would be a cycle), and gap fill must paint
   THROUGH the live painter, never queue inside its replay buffer. */
import { ui } from "./state.js";
// Step 5 (compaction-checkpoint-bookmark): the trailing reload seams flush
// here, ABOVE the live tail, before either early return. No cycle: compaction
// imports only dom.js.
import { flushSeams } from "./compaction.js";
// BK-019 D6: map ownership (entry create/delete, the terminal stamp) lives in
// runstate.js's storage trio; replay keeps the arithmetic on slot CONTENT.
import { slotFor, dropSlot, noteRunTerminal } from "./runstate.js";

// ── replay render helpers (moved from transcript.js, verbatim) ─────────
// These back the persisted-row replay loop in transcript.js; they live here
// because this module is where Step 6's seq/join machinery grew and
// transcript.js is at its size ceiling.

// The agent prepends a <memory>…</memory> block to the first user turn; don't
// show it verbatim in history.
export function stripMemoryBlock(text) {
  return text.replace(/^<memory>[\s\S]*?<\/memory>\s*/, "");
}

// History stores call_tool verbatim (provider round-trip), but the live stream
// shows the INNER tool (tool_dispatch unwraps before on_tool_call). Mirror
// unwrap_call_tool so replay shows the same pill; a malformed wrapper stays
// "call_tool", as it did live.
export function shownCall(name, args) {
  if (name !== "call_tool" || !args) return [name, args];
  const inner = args.name, innerArgs = args.arguments;
  if (typeof inner !== "string" || !inner || inner === "call_tool" || inner === "load_tools"
      || !innerArgs || typeof innerArgs !== "object" || Array.isArray(innerArgs)) {
    return [name, args];
  }
  const clean = {};
  for (const [k, v] of Object.entries(innerArgs)) if (v !== null) clean[k] = v;
  return [inner, clean];
}

// ── the slot ────────────────────────────────────────────────────────────
// ui.liveSeq[id]  = { runId, water, painted }   (null runId = PROVISIONAL:
//                   created by an empty-tail join, whose payload named no
//                   run; provisional slots hold NO marks and NO watermark —
//                   unattributed marks would swallow a new run's seq 1..N,
//                   which is token loss, while the row-duplicate they used
//                   to guard is transient. A batch meeting one cold-joins.)
// ui.liveDone[id] = runId of the last run known OVER (see the identity rule).
//
// BK-019 D6: the MAPS belong to runstate.js (slotFor/dropSlot/noteRunTerminal
// are the only sites that create, delete, or stamp entries); this module keeps
// every invariant computed on slot CONTENT (the arithmetic below reads and
// writes the slot objects through slotFor). Dependency direction replay→
// runstate; runstate imports state.js only, so no cycle.

function slot(id) {
  return slotFor(id);
}

// The transcript a slot's marks describe is gone — forget the slot for the
// session being reset. Called from resetTranscript BEFORE any re-derivation:
// renderSession's own path re-marks what its fresh DOM paints (the live join
// marks the persisted head, the flush loop marks buffered events), so marks
// never outlive the DOM that earned them — and never leak from a previous
// run through a reset either. The terminal-run note SURVIVES the reset: it
// guards against exactly the late events that arrive after the heal.
export function clearLiveRun() {
  if (ui.sessionId) dropSlot(ui.sessionId);
}

// The run on this session is over: seen via run_done, or displaced by a new
// run (serial runs — a successor proves its predecessor finished). One entry
// per session: the guard exists for the LATE events of the run that just
// ended (the post-run_done `title` publisher), and two runs later the reroute
// that could carry one is long gone.
export function markRunDone(id, runId) {
  noteRunTerminal(id, runId); // BK-019 D6: the stamp lives in runstate's map
}

export function isTerminalRun(id, runId) {
  return runId != null && !!(ui.liveDone && ui.liveDone[id] === runId);
}

// Adopt `runId`, resetting the numbers when it is a different run — and
// marking the DISPLACED run terminal on the way (a run change means the old
// run is over, run_done seen or not: this is the choke point that catches
// runs whose run_done died with the socket). A terminal run's event may
// touch nothing here — that is the late-title guard for every sink and
// painter at once.
function syncRun(id, s, runId) {
  if (runId == null || runId === s.runId) return;
  if (isTerminalRun(id, runId)) return;
  if (s.runId != null) markRunDone(id, s.runId);
  s.runId = runId;
  s.water = 0;
  s.painted = [];
}

// Already on the screen? Membership in the merged painted set. Seq-less
// events (background stream) answer "no" and pass through the gates. An event
// from a DIFFERENT run answers "no" even when its number falls inside the old
// run's intervals — seq space is run-local, so the comparison is meaningless;
// markPainted then resets the slot on the way in.
export function alreadyPainted(id, ev) {
  const s = ui.liveSeq && ui.liveSeq[id];
  if (!s || !ev || ev.seq == null) return false;
  if (ev.run_id != null && s.runId != null && ev.run_id !== s.runId) return false;
  for (const [lo, hi] of s.painted) if (ev.seq >= lo && ev.seq <= hi) return true;
  return false;
}

// Record an event's VISIBLE effect as painted — called at every site that
// paints, so the answer above tracks the screen, not a counter. Painting it
// accounts for its number: the watermark rides along, from `seq` only. A
// terminal run's event never touches the slot (its picker relabel etc. still
// run — only the bookkeeping is guarded).
export function markPainted(id, ev) {
  if (ev.seq == null || isTerminalRun(id, ev.run_id)) return;
  const s = slot(id);
  syncRun(id, s, ev.run_id);
  const p = s.painted;
  let lo = ev.seq, hi = ev.seq;
  for (let i = p.length - 1; i >= 0; i--) {
    if (p[i][1] + 1 < lo || p[i][0] - 1 > hi) continue;  // disjoint: keep
    lo = Math.min(lo, p[i][0]);
    hi = Math.max(hi, p[i][1]);
    p.splice(i, 1);
  }
  let at = p.length;
  while (at > 0 && p[at - 1][0] > lo) at--;
  p.splice(at, 0, [lo, hi]);
  if (ev.seq > s.water) s.water = ev.seq;
}

// Advance the request watermark from a `seq` the server itself issued — a
// /live response's watermark. The invariant lives at this seam: `since`
// never reaches this function (see header). NO slot means nothing has been
// attributed yet (a first-word batch paints and marks instead) — creating a
// slot here would be a PROVISIONAL one holding a watermark, the identity
// rule's forbidden state.
export function raiseWatermark(id, seq) {
  if (seq == null) return;
  const s = ui.liveSeq && ui.liveSeq[id];
  if (s && seq > s.water) s.water = seq;
}

// The stored request watermark, or null where no run has ever been seen for
// this session — the difference between the cheap since=N gap fill and the
// unnumbered whole-window ask (which the painted set still dedupes).
export function storedWatermark(id) {
  const s = ui.liveSeq && ui.liveSeq[id];
  return s && s.water > 0 ? s.water : null;
}

// A provisional slot (empty-tail join): it holds no marks and no watermark,
// so it must not be trusted for gap-fill, and a resume with one pending must
// still happen (the visibility handler asks) — the answer is a cold-join.
export function isProvisionalSlot(id) {
  const s = ui.liveSeq && ui.liveSeq[id];
  return !!s && s.runId == null;
}

// The run a /live batch belongs to (rings are per-run, so one name serves
// for the batch; pre-run events name none).
export function batchRunId(events) {
  for (const ev of events || []) if (ev.run_id != null) return ev.run_id;
  return null;
}

// Can this slot vouch for a batch of `runId`'s events? No slot: the batch is
// the first word — paint and attribute. Same run: paint the gap. Provisional
// or different run: numbers restarted or the run is unknown — the slot can
// judge nothing, and the caller must cold-join instead (identity rule).
export function slotBatchMismatch(id, runId) {
  const s = ui.liveSeq && ui.liveSeq[id];
  if (!s || runId == null) return false;
  return s.runId == null || s.runId !== runId;
}

// THE identity verdict for a /live batch, one place: null = the slot vouches
// for it (paint the gap, deduped); "terminal" = the batch's run already ended
// (run_done passed it while the fetch flew) — recovery-armed join; "mismatch"
// = it names another run or the slot is provisional — the numbers restarted
// or the run is unknown, so gap-fill would skip or double, and the caller
// must cold-join (runstate T27 verdictColdJoin: the one sanctioned fetch).
// An EMPTY batch names no run, but it is still an identity witness: `seq` is
// monotonic PER RUN, so a live response whose seq is BELOW my watermark
// cannot be my run (my water came from that run's own seqs) — a SHORT new run
// (its whole head below my number) would otherwise read as "caught up" and
// stay invisible until run_done.
export function joinReason(id, events, seq) {
  const bRun = batchRunId(events);
  if (bRun == null) {
    const water = storedWatermark(id);
    return seq != null && water != null && seq < water ? "mismatch" : null;
  }
  if (isTerminalRun(id, bRun)) return "terminal";
  return slotBatchMismatch(id, bRun) ? "mismatch" : null;
}

// ── the /live read ──────────────────────────────────────────────────────

// GET /api/chat/{id}/live[?since=N] — the resync read (routes/streaming.py):
// {active: true, seq, oldest_seq, gap, events} while the run lives, every
// event with seq > since stamped {seq, run_id} exactly as the live sinks
// carried it; {active: false} and nothing else once it is gone. A client
// that HOLDS a watermark comes here with it — re-fetching /messages
// mid-answer is the MiB-scale path the docstring forbids. Null on network
// failure: callers keep their state and ask again. A 404 is NOT a failure to
// retry — it is an agentd that predates the route (version skew on deploy
// day): the {missing: true} sentinel tells the watchdog to STOP probing (the
// run_done heal still works on that server; a store render would not — an
// agentd this old has no live join either, so it is the lossy path).
export async function fetchLive(id, since) {
  try {
    const r = await fetch(
      "/api/chat/" + encodeURIComponent(id) + "/live" +
      (since != null && since > 0 ? `?since=${since}` : "")
    );
    if (r.status === 404) return { missing: true };
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  }
}

// The event types that put something on screen. Bookkeeping types (stopped,
// error, turn_complete, title, compaction_start) belong to whichever sink
// sees them; a spliced replay must not fire their side-effects twice, so
// both painters below skip them UNMARKED — if the live sink still has the
// event coming, its duties run there exactly once; if it already ran them,
// the run is over and the heal refetches.
const RENDERED = {
  delta: 1, thinking_delta: 1, thinking_done: 1,
  tool_call: 1, tool_result: 1, truncated: 1,
  // The compaction seam is transcript content (compaction.js paints it);
  // its start frame stays out — it only drives the status row.
  compaction: 1,
};

// Paint a /live answer onto a screen that already has a head. Already-painted
// seqs — the live sink raced this fetch — are skipped: that is the whole
// point of the painted intervals. Whatever stays open (bubble, thinking
// strip) is closed by the run's own run_done on the stream, or by the heal.
export function paintGapFill(id, events, paint) {
  let painted = 0;
  for (const ev of events || []) {
    if (ev.session_id && ev.session_id !== id) continue;
    if (!RENDERED[ev.type] || alreadyPainted(id, ev)) continue;
    paint(ev);
    markPainted(id, ev);
    painted++;
  }
  return painted;
}

// renderSession's flush: events buffered while the replay owned the DOM land
// on the REBUILT transcript. Mark each painted so the next resync skips it —
// but SKIP any the /messages live join already painted: a rerouted event
// emitted before that fetch's join and delivered to the sink during it is
// BOTH in the join's tail and in the buffer, and join-then-flush would paint
// it twice. Paint through the injected buffered dispatcher (transcript's
// dispatchStream; raw and buffered are the same at flush time).
export function flushBuffered(id, events, dispatch) {
  let painted = 0;
  for (const ev of events || []) {
    if (alreadyPainted(id, ev)) continue;
    markPainted(id, ev);
    dispatch(ev);
    painted++;
  }
  return painted;
}

// ── the /messages live join ─────────────────────────────────────────────

// renderSession's tail step (called SYNCHRONOUSLY inside the atomic swap,
// after the persisted rows and their endThinking): a /messages fetch that
// carried `live` ends at the join, not at the persisted half. `live.events`
// is the UNPERSISTED tail from the server's `since` boundary onward
// (gateway/live.py's persisted_tail_projection), painted through the injected
// live painter — this IS the replay; it must not queue behind the replay
// buffer it lives inside. (Deltas stay paced: the tail is live text whose
// bubble the stream continues into, not history — see DECISION 3's no
// finalize. Only the persisted rows are unpaced.)
//
// The head problem: the chat SSE painted this run's first events before
// loadSession's fetch returned, and the swap wiped those bubbles. The
// persisted rows stand in for the run's first `since` seqs — mark them, or
// a later /live resync re-paints them over the rows. Above `since` the
// screen is fresh: every renderable tail event paints (an already-painted
// mark from the pre-wipe DOM must NOT skip one — the DOM that earned it is
// gone), and each mark re-records it for the next resync. No finalize: the
// tail is mid-answer; its open bubble and thinking strip are the LIVE state
// the stream continues into, and the run's own run_done closes them.
export function consumeLiveJoin(id, live, paint) {
  flushSeams(); // trailing Step-5 reload seams: anchored past the last row, above the tail
  if (!live) return;
  const tail = live.events || [];
  const lastRun = tail.length ? tail[tail.length - 1].run_id : null;
  if (lastRun == null) {
    // Empty tail: the payload names NO run, so nothing here can be
    // attributed — leave a PROVISIONAL slot (created empty: no marks, no
    // watermark). Unattributed head marks would swallow a NEW run's seq 1..N
    // (token loss, the identity rule's worst failure); the flush-race row
    // duplicate they might have prevented is transient and heals at
    // run_done. The next batch meeting this slot cold-joins and attributes.
    slot(id);
    return;
  }
  const s = slot(id);
  syncRun(id, s, lastRun);
  // syncRun refuses a run already marked terminal — but HERE the payload is
  // the only witness (this join was sampled from the server), so it still
  // attributes the FRESH slot rather than leaving a provisional one that takes
  // `live.seq` as a watermark (face 3's forbidden state, second seam: a run
  // that ends DURING the fetch). An attributed slot is the payload's own run,
  // so the water below is that run's, and `alreadyPainted` still compares ids.
  if (s.runId == null) s.runId = lastRun;
  // The run's last issued number — never `since`.
  if (live.seq > s.water) s.water = live.seq;
  for (let n = 1; n <= (live.since || 0); n++) markPainted(id, { seq: n, run_id: lastRun });
  for (const ev of tail) {
    if (!RENDERED[ev.type]) continue;
    paint(ev);
    markPainted(id, ev);
  }
}
