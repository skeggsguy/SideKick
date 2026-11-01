/* replay.js's dedupe rules, pinned AS THEY EXIST TODAY (pre-BK-019 refactor)
   — plan bk-019-run-lifecycle Step 2, BK-020's first two acceptance criteria.

   This is the "pin current behaviour so the Step 3/4 refactor is guarded"
   set: per-run watermark, painted-interval skipping, the gap suffix, the
   run-identity cold-join triggers, and markRunDone terminal stamping — the
   resume-seq-dedupe / resume-run-identity rules (test_conventions) read off
   replay.js's exported surface. The rules named in each check are the SAME
   rules probe_live drives end-to-end; here they are driven per function,
   no DOM, no fetch.

   A red line here means replay.js's arithmetic changed. If Step 3/4 moved
   the arithmetic (slot storage behind runstate's slotFor), fix the CALL, not
   the expectation: the contract each line names is pinned by test_conventions'
   resume source pins + probe_live, not by this file. */

import { ui } from "./state.js";
import {
  clearLiveRun, markRunDone, isTerminalRun, alreadyPainted, markPainted,
  raiseWatermark, storedWatermark, isProvisionalSlot, batchRunId,
  slotBatchMismatch, joinReason, paintGapFill, flushBuffered, consumeLiveJoin,
} from "./replay.js";
import { test, xtest, check, eq, slotOf } from "./runner.js";

const S = "unit-session";
const R1 = "run-aaaa";
const R2 = "run-bbbb";
const R9 = "run-cccc";

const delta = (seq, runId = R1, extra) =>
  ({ type: "delta", text: "w", seq, run_id: runId, ...(extra || {}) });
const collector = () => {
  const seen = [];
  return { seen, paint: (ev) => seen.push(ev) };
};
const seqsPainted = (c) => c.seen.map((ev) => ev.seq);
const paintedOf = (id) => (slotOf(id) ? slotOf(id).painted : null);

// ── per-run watermark (run_id-scoped; server `seq`s only, never a `since`) ──

test("watermark: rides the seq of every marked event, never retreats", () => {
  markPainted(S, delta(3));
  eq("watermark: rides the seq of every marked event", storedWatermark(S), 3);
  markPainted(S, delta(1));
  eq("watermark: painting a LOWER seq never retreats it", storedWatermark(S), 3);
});

test("watermark: raiseWatermark takes server seqs only — monotone, no slot creation", () => {
  markPainted(S, delta(3));
  raiseWatermark(S, 7);
  eq("watermark: raiseWatermark advances it from a server seq", storedWatermark(S), 7);
  raiseWatermark(S, 2);
  eq("watermark: a lower seq never retreats it", storedWatermark(S), 7);
  raiseWatermark(S, null);
  eq("watermark: a null seq is a no-op", storedWatermark(S), 7);
  // The identity rule's forbidden state: a PROVISIONAL slot holding a
  // watermark. No slot ⇒ nothing attributed yet ⇒ raiseWatermark must not
  // create one (replay.js: "a first-word batch paints and marks instead").
  raiseWatermark("ghost", 5);
  check("watermark: raiseWatermark never creates a slot (a watermark never goes unattributed)",
    slotOf("ghost") === undefined && storedWatermark("ghost") === null);
});

test("watermark: run-scoped — a new run's seq restarts, the old run's numbers reset", () => {
  markPainted(S, delta(4));          // slot vouches for R1, watered at 4
  markPainted(S, delta(2, R2));      // displaced by R2 mid-bookkeeping
  check("watermark: the slot reset marks the REPLACED run terminal (displacement proves it ended)",
    isTerminalRun(S, R1) === true);
  eq("watermark: the new run's watermark is ITS OWN seq (seq space restarted — a stale water would swallow it)",
    storedWatermark(S), 2);
  eq("watermark: the old run's painted intervals are gone", paintedOf(S), [[2, 2]]);
  check("watermark: the old run's painted number is not claimed for the new run",
    alreadyPainted(S, delta(4, R1)) === false);
});

test("watermark: the /messages live join waters at the response seq, NEVER at since", () => {
  const c = collector();
  consumeLiveJoin(S, { seq: 40, since: 10, events: [delta(11)] }, c.paint);
  // The two-sinces rule (gateway/live.py) obeyed from the field names:
  // since is the JOIN boundary; watermarking at it would refetch the tail forever.
  eq("watermark: since=N is a join boundary, not a watermark", storedWatermark(S), 40);
  check("watermark: the persisted head (1..since) is marked painted — a later resync won't repaint the rows",
    alreadyPainted(S, delta(5)) === true);
  check("watermark: the join-tail event is re-marked for the next resync",
    alreadyPainted(S, delta(11)) === true);
  check("watermark: above the tail nothing is marked yet",
    alreadyPainted(S, delta(12)) === false);
  eq("watermark: only the tail event reached the screen (the head stands as rows)",
    seqsPainted(c), [11]);
});

// ── painted-interval skipping (NOT a scalar seq <= watermark cutoff) ────────

test("painted: interval membership decides, not the scalar seq <= watermark cutoff", () => {
  markPainted(S, delta(1));
  markPainted(S, delta(2));
  markPainted(S, delta(4));
  check("painted: a watermark of 4 IS a scalar-cutoff state (the shortcut would call 3 seen)",
    storedWatermark(S) === 4);
  check("painted: a seq BELOW the watermark but OUTSIDE the painted intervals is not seen (the shortcut ate the word live skipped)",
    alreadyPainted(S, delta(3)) === false);
  check("painted: seqs inside the intervals are seen (1, 2, 4)",
    [1, 2, 4].every((n) => alreadyPainted(S, delta(n))) === true);
  check("painted: a seq above everything is not seen",
    alreadyPainted(S, delta(5)) === false);
});

test("painted: intervals merge on adjacency, stay split across a gap", () => {
  markPainted(S, delta(1));
  markPainted(S, delta(3));
  eq("painted: a one-seq gap keeps two intervals", paintedOf(S), [[1, 1], [3, 3]]);
  markPainted(S, delta(2));
  eq("painted: filling the gap merges to one interval (the membership test stays short)",
    paintedOf(S), [[1, 3]]);
  markPainted(S, delta(4));
  eq("painted: the next number extends the interval", paintedOf(S), [[1, 4]]);
  markPainted(S, delta(6));
  eq("painted: skipping a number keeps a fresh interval", paintedOf(S), [[1, 4], [6, 6]]);
});

test("painted: a seq-less event passes every gate (idempotent background renders)", () => {
  markPainted(S, delta(2));
  check("painted: alreadyPainted answers no for a seq-less event",
    alreadyPainted(S, { type: "model_wait" }) === false);
  markPainted(S, { type: "model_wait" });
  eq("painted: marking a seq-less event changes no interval", paintedOf(S), [[2, 2]]);
  eq("painted: ... and moves no watermark", storedWatermark(S), 2);
  markPainted("ghost", { type: "delta" });
  check("painted: ... and creates no slot", slotOf("ghost") === undefined);
});

test("painted: seq space is RUN-LOCAL — another run's number inside my intervals is not mine", () => {
  for (const n of [1, 2, 3, 4, 5]) markPainted(S, delta(n));
  check("painted: my run's painted seq 3 is seen", alreadyPainted(S, delta(3)) === true);
  check("painted: run B's seq 3 is NOT seen (the cross-run comparison is meaningless; markPainted resets on the way in)",
    alreadyPainted(S, delta(3, R2)) === false);
});

// ── gap → the server's suffix is painted whole (the CLIENT half) ────────────
// The server owns gap/oldest_seq (gateway/live.py + event_ring.py: the suffix
// starts at oldest_seq) and pins it in tests/test_chat_recovery.py
// (test_live_reports_gap_when_the_head_overflowed_the_ring). Nothing in webui
// reads either field (design §6.3, verified by grep), so the only client-side
// obligation left is this module's: the returned batch paints WHOLE through
// painted-interval dedupe — a scalar seq <= watermark cutoff would eat the
// live-skipped seqs sitting below my watermark. This test pins that half and
// says so in its name; it does not, and cannot, exercise the server's choice
// of oldest_seq.

test("gap: the client paints the server's suffix whole (interval dedupe only — oldest_seq is the SERVER's choice)", () => {
  markPainted(S, delta(4));
  raiseWatermark(S, 5); // watermark ABOVE what was painted: the race shape
  const c = collector();
  const n = paintGapFill(S, [1, 2, 3, 4, 5, 6, 7].map((k) => delta(k)), c.paint);
  eq("gap: only the PAINTED interval (4) skips — no seq<=watermark cutoff eats 1..3",
    seqsPainted(c), [1, 2, 3, 5, 6, 7]);
  eq("gap: paintGapFill returns what it painted", n, 6);
  eq("gap: the painted intervals close up behind the suffix", paintedOf(S), [[1, 7]]);
  eq("gap: the watermark rides the painted suffix (server seqs only)", storedWatermark(S), 7);
});

test("gap: the live sink's race is skipped — already-painted head, the rest lands", () => {
  for (const n of [1, 2, 3, 4, 5]) markPainted(S, delta(n)); // the sink painted while the fetch flew
  const c = collector();
  const n = paintGapFill(S, [4, 5, 6, 7, 8].map((k) => delta(k)), c.paint);
  eq("gap: the overlap is skipped, the new tail lands", seqsPainted(c), [6, 7, 8]);
  eq("gap: the return count matches", n, 3);
  eq("gap: the watermark caught up to the suffix end", storedWatermark(S), 8);
  eq("gap: an empty batch is a clean no-op and creates no slot for another session",
    paintGapFill("ghost", [], c.paint) === 0 && slotOf("ghost") === undefined, true);
});

test("gap: bookkeeping types are skipped UNMARKED (their sink runs the duties once)", () => {
  markPainted(S, delta(4));
  const c = collector();
  const n = paintGapFill(S, [
    { type: "stopped", seq: 5, run_id: R1 },
    { type: "title", seq: 6, run_id: R1 },
    { type: "turn_complete", seq: 7, run_id: R1 },
    { type: "error", seq: 8, run_id: R1 },
    { type: "compaction_start", seq: 9, run_id: R1 },
  ], c.paint);
  eq("gap: no non-rendered type paints", n, 0);
  eq("gap: ... and none is marked (the live sink still owes its side effects)",
    paintedOf(S), [[4, 4]]);
  eq("gap: ... nor does the watermark move", storedWatermark(S), 4);
});

test("gap: an event naming another session is not this screen's", () => {
  markPainted(S, delta(4));
  const c = collector();
  const n = paintGapFill(S, [
    delta(5, R1, { session_id: "other-session" }),
    delta(6),
  ], c.paint);
  eq("gap: the foreign-session event is skipped; the own-session event lands",
    seqsPainted(c), [6]);
  check("gap: the return count excludes the foreign event", n === 1);
});

// ── the run-identity rule: joinReason is the ONE cold-join verdict ──────────
// A slot vouches for exactly ONE run. Every non-null verdict routes to the
// one-fetch messages+live join (events.js coldJoinRun today, runstate
// verdictColdJoin after Step 3).

test("cold-join: a batch naming a DIFFERENT run is a mismatch (numbers restarted)", () => {
  markPainted(S, delta(1)); // the slot vouches for R1
  eq("cold-join: batch names R2 while the slot holds R1",
    joinReason(S, [delta(1, R2), delta(2, R2)], 2), "mismatch");
  eq("cold-join: the batch's run is the FIRST named one (rings are per-run; pre-run events name none)",
    batchRunId([{ type: "run_start" }, delta(3)]), R1);
  check("cold-join: a same-run batch is vouched for (null verdict — paint the gap)",
    joinReason(S, [delta(2)], 3) === null);
  check("cold-join: no slot — the batch is the first word: paint and attribute",
    joinReason("fresh", [delta(1, R9)], 1) === null && slotBatchMismatch("fresh", R9) === false);
});

test("cold-join: a PROVISIONAL (empty-tail join) slot vouches for nothing", () => {
  const c = collector();
  consumeLiveJoin(S, { seq: 50, since: 0, events: [] }, c.paint); // empty tail names no run
  check("cold-join: the empty-tail join leaves a provisional slot",
    isProvisionalSlot(S) === true);
  eq("cold-join: a provisional slot holds NO watermark (live.seq went unused — the forbidden state)",
    storedWatermark(S), null);
  eq("cold-join: ... and NO marks (unattributed marks would swallow a new run's 1..N)",
    paintedOf(S), []);
  eq("cold-join: a batch meeting the provisional slot cold-joins",
    joinReason(S, [delta(1, R9)], 1), "mismatch");
});

test("cold-join: a batch naming a TERMINAL run answers terminal", () => {
  markPainted(S, delta(1));
  markRunDone(S, R1); // run_done passed the run while the /live fetch flew
  eq("cold-join: the batch's run already ended — recovery-armed join, not gap fill",
    joinReason(S, [delta(2)], 2), "terminal");
});

test("cold-join: an EMPTY batch whose seq is below my watermark cannot be my run", () => {
  for (const n of [1, 2, 3, 4, 5]) markPainted(S, delta(n)); // my water: 5, from R1's own seqs
  // `seq` is monotonic PER RUN, so a live answer below my number means the
  // numbers restarted — a SHORT new run (head entirely below my water) read
  // as "caught up" would stay invisible until run_done.
  eq("cold-join: short new run (whole head below my number) ⇒ mismatch",
    joinReason(S, [], 3), "mismatch");
  eq("cold-join: seq AT my watermark reads caught-up, not mismatch (no inferred gap)",
    joinReason(S, [], 5), null);
  eq("cold-join: seq above it is simply ahead", joinReason(S, [], 6), null);
  eq("cold-join: a seq-less answer judges nothing", joinReason(S, [], null), null);
  eq("cold-join: with no watermark there is nothing to be below",
    joinReason("ghost", [], 3), null);
});

// ── markRunDone's terminal stamping ─────────────────────────────────────────

test("terminal: markRunDone stamps the run — one entry per session, nulls ignored", () => {
  markRunDone(S, R1);
  check("terminal: the stamped run is terminal", isTerminalRun(S, R1) === true);
  check("terminal: another run of the same session is not", isTerminalRun(S, R2) === false);
  check("terminal: another session's stamp does not leak", isTerminalRun("other", R1) === false);
  check("terminal: a null run id is never terminal", isTerminalRun(S, null) === false);
  markRunDone(S, null);
  check("terminal: markRunDone(…, null) is ignored — the stamp survives",
    isTerminalRun(S, R1) === true);
  markRunDone(S, R2);
  check("terminal: one entry per session — the newest stamp wins (two runs later the reroute that could carry a late event is long gone)",
    isTerminalRun(S, R2) === true && isTerminalRun(S, R1) === false);
});

test("terminal: a terminal run's events never touch the slot (the late-title guard)", () => {
  for (const n of [1, 2, 3, 4, 5]) markPainted(S, delta(n));
  markRunDone(S, R1);
  markPainted(S, delta(9)); // the stopped detached run publishing `title` after run_done
  eq("terminal: its late seq adds no interval (it may relabel the picker, never reset the slot)",
    paintedOf(S), [[1, 5]]);
  eq("terminal: ... and moves no watermark", storedWatermark(S), 5);

  // The other face: the slot now holds a NEWER run — the OLD terminal run's
  // late event must not displace or reset it (syncRun's terminal refusal).
  ui.liveSeq = {};
  ui.liveDone = {};
  markPainted(S, delta(1, R2));
  markRunDone(S, R1);
  markPainted(S, delta(3, R1));
  eq("terminal: the newer run's slot keeps its run identity", slotOf(S).runId, R2);
  eq("terminal: ... its watermark (the late event's 3 never landed)", storedWatermark(S), 1);
  eq("terminal: ... and its intervals", paintedOf(S), [[1, 1]]);
});

test("terminal: clearLiveRun forgets the slot but keeps the terminal note", () => {
  ui.sessionId = S;
  markPainted(S, delta(2));
  markRunDone(S, R1);
  markPainted("keep-me", delta(1)); // another session's slot
  clearLiveRun();
  check("terminal: the screen's slot is gone (marks never outlive the DOM that earned them)",
    slotOf(S) === undefined && storedWatermark(S) === null);
  check("terminal: the terminal note SURVIVES the reset — it guards exactly the late events that arrive after the heal",
    isTerminalRun(S, R1) === true);
  check("terminal: only the screen's session is reset", slotOf("keep-me") !== undefined);
});

test("terminal: a join payload from a run that ended DURING the fetch still attributes", () => {
  markRunDone(S, R1); // run_done landed while the /messages fetch flew
  const c = collector();
  consumeLiveJoin(S, { seq: 9, since: 2, events: [delta(3)] }, c.paint);
  // The payload is the only witness for this fresh slot: an attributed slot
  // is the payload's own run, so its watermark is that run's — the forbidden
  // state is a PROVISIONAL slot carrying one.
  check("terminal: the fresh slot is attributed, not left provisional (syncRun refused, the join attributes)",
    slotOf(S).runId === R1 && isProvisionalSlot(S) === false);
  eq("terminal: the attributed slot takes the payload's seq as its watermark",
    storedWatermark(S), 9);
  eq("terminal: the terminal guard still refuses the MARKS (a terminal run's events touch nothing)",
    paintedOf(S), []);
  eq("terminal: the tail event still reaches the screen (paint is not a slot touch)",
    seqsPainted(c), [3]);
});

// ── join-then-flush interaction (reopen dedupe) ──────────────────────────────

test("join: what the /messages join painted, the flush skips (one paint per seq)", () => {
  const c = collector();
  consumeLiveJoin(S, { seq: 8, since: 3, events: [delta(4), delta(5)] }, c.paint);
  eq("join: head (1..since) plus the tail land marked", paintedOf(S), [[1, 5]]);
  const flushed = [];
  const n = flushBuffered(S, [delta(4), delta(5), delta(6), delta(7)],
    (ev) => flushed.push(ev.seq));
  // The rerouted event emitted before the join's fetch and delivered during
  // it is BOTH in the join's tail and in the buffer — join-then-flush would
  // paint it twice without this skip.
  eq("join: the buffered events the join already painted are skipped at flush",
    flushed, [6, 7]);
  check("join: the flush count matches", n === 2);
});

// ── the tier's own contract (these must stay green; the control must go red) ─

test("tier: an awaited async test finishes before the results block is written", async () => {
  // Step 3's transitions are promise-driven. If the page were dumped before an
  // await chain completed, the block would still read "(running)" (harness
  // error, exit 2) — this test is the proof that awaits and (virtual) timers
  // land before the dump, rather than an untested assumption. The timer is a
  // real 50 ms delay: it completes only because --virtual-time-budget
  // fast-forwards it, which is WHY the budget is kept for this tier.
  let timerFired = false;
  await Promise.resolve(); // microtask hop
  await new Promise((r) => setTimeout(() => { timerFired = true; r(); }, 50)); // virtual-time hop
  check("tier: await crosses a microtask and a fast-forwarded timer before the results block",
    timerFired === true);
});

test("tier: the rAF admission rule is enforced, not prose", () => {
  // suite.html replaces requestAnimationFrame with a thrower (rAF callbacks
  // never fire under --virtual-time-budget — gotchas). If that stub ever goes
  // away, an rAF-paced module would report a silent no-op PASS; this test reds
  // instead.
  let blocked = false;
  try {
    window.requestAnimationFrame(function () {});
  } catch (e) {
    blocked = /jsunit tier/.test(String((e && e.message) || ""));
  }
  check("tier: requestAnimationFrame throws (rAF-paced code belongs to the probe tier)",
    blocked);
});

// The permanent positive control (runner.xtest): EVERY reporting primitive the
// suite leans on must be able to go red. check() is guarded by the false check;
// eq() by the deliberately-unequal pair — an eq whose comparison rotted to
// always-equal would otherwise green every eq in this file silently (planted
// and confirmed red 2026-10-29 at the Step-2 review).
xtest("control: a false assertion is caught by the tier", () => {
  check("control: storedWatermark answers null with no slot (a false expectation must go red)",
    storedWatermark("nothing-here") !== null, "no slot ⇒ storedWatermark is null");
  eq("control: eq can go red (an always-equal comparison would green every eq here)",
    1, 2);
});
