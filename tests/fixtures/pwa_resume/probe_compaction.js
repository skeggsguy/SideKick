/* Compaction live-UI scenarios (plan compaction-checkpoint-bookmark, Step 4).

   Drives the REAL handlers through the production /api/events sink
   (window.__sk.handleRunEvent — app.js's chat-SSE path feeds the SAME
   status.applyEvent + transcript.dispatchStream pair; its terminal handlers
   are the same statusStop() calls pinned here via stopped/error). What
   these pin:

     compaction-live     `compaction_start` shows the pulsing "Compacting…"
                         phase; a `compaction` frame paints the blue seam as
                         ONE #chat-log child directly ABOVE the current
                         turn's first row (below the earlier answer), carrying
                         data-cp for Step 5's reload dedupe; the next phase
                         relabels off it; and an ORPHANED start (cleric: the
                         run cancelled mid-summarizer, no compaction frame)
                         is cleared by stopped AND by error.
     compaction-gapfill  the events emitted while hidden reached NO sink —
                         the seam exists only if replay.js's RENDERED
                         whitelist lets paintGapFill through (the seam's
                         most-likely-missed spot); compaction_start must
                         paint no transcript node; an already-painted seq
                         re-delivered on the live sink paints no second seam;
                         and the run_done heal (abandonPendingRun) clears a
                         spinner orphaned across the cancel.
     compaction-join     renderSession's /messages live join (consumeLiveJoin)
                         paints the seam too — the OTHER replay seam, above
                         the persisted turn head.
     compaction-reload   Step 5: the seam survives a refresh. renderSession
                         anchors seams from /messages `compactions` (store
                         idxes, rows carry `idx`): R1 anchored position +
                         data-cp/data-anchor; R2 no compactions → no seam;
                         R3 the DEDUPE — one compaction offered twice
                         (compactions + join tail) paints one seam at the
                         ANCHORED row, and the tail still paints; R4 a seam
                         anchored past the last row flushes above the live
                         tail (race snapshot); R5 two compactions in one
                         payload paint two seams at their own anchors.

   Runs inside probe.js's page after scenario H2, before probe_live's boots:
   no app.js boot, no localStorage, and every piece of shared mock/ui state
   is restored to what probe_live and probe_coldstart expect. */
"use strict";

import { ui } from "./state.js";
import {
  addUserMsg, dispatchStream, finalizeAgentBubble, renderSession, resetTranscript,
} from "./transcript.js";
import { stop as statusStop } from "./status.js";

const SETTLE_MS = 350; // > mock delayMs 150, with slack (probe_live recipe)

const row = () => document.querySelector("#chat-status");
const label = () => document.querySelector("#chat-status-label").textContent;

// Suspension: the production visibilitychange path (probe_live owns its own
// copy; the helper there is module-private, and this scenario must run
// BEFORE probe_live, so this one carries its own).
const setVis = (v) =>
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => v });
const suspend = () => { setVis("hidden"); document.dispatchEvent(new Event("visibilitychange")); };
const resume = () => { setVis("visible"); document.dispatchEvent(new Event("visibilitychange")); };

// Ring write stamped exactly like chat_run.py's emit() (seq per RUN, paired
// run_id) — the same helper shape probe_live.js uses.
const emit = (lr, ev) => {
  const stamped = { ...ev, seq: ++lr.lastSeq, run_id: lr.runId };
  lr.events.push(stamped);
  return stamped;
};
const deliver = (sid, ev) => window.__sk.handleRunEvent({ ...ev, session_id: sid });
const userTurn = (text) => ({ role: "user", text, attachments: [] });

export async function runCompaction({ check, sleep }) {
  const log = document.querySelector("#chat-log");
  const seams = () => log.querySelectorAll(".compaction-seam");
  const kids = () => [...log.children];

  // =========================================================================
  // compaction-live — the mid-turn UI through the production sink.
  // =========================================================================
  const SID = "probe-cp-live";
  resetTranscript();
  statusStop();
  ui.sessionId = SID;
  ui.resync = null;
  ui.liveTurn = null;
  addUserMsg("PROBE_CP earlier question");
  dispatchStream({ type: "delta", text: "PROBE_CP earlier answer" });
  finalizeAgentBubble(); // rAF is dead headless — the earlier turn is history
  addUserMsg("PROBE_CP current question"); // the turn now compacting

  deliver(SID, { type: "compaction_start" });
  const spinning = () => !!row() && !row().hidden && label() === "Compacting…";
  check("compaction-live: compaction_start shows the Compacting phase on the pulsing status row",
    spinning() && !!row().querySelector(".cs-dot"),
    `hidden=${row() ? row().hidden : "n/a"} label="${label()}"`);

  deliver(SID, {
    type: "compaction", checkpoint_id: "cp-probe-1", anchor_idx: 42,
    seq: 2, run_id: "probe-run-cp1",
  });
  const turnUser = [...log.querySelectorAll(".msg.user")].pop();
  const earlierAgent = log.querySelector(".msg.agent");
  const seamOk = () => {
    const s = log.querySelector(".compaction-seam");
    if (!s || seams().length !== 1 || s.parentElement !== log) return false;
    const k = kids();
    return s.nextElementSibling === turnUser && k.indexOf(s) === k.indexOf(turnUser) - 1 &&
      k.indexOf(earlierAgent) !== -1 && k.indexOf(earlierAgent) < k.indexOf(s) &&
      s.getAttribute("role") === "separator";
  };
  check("compaction-live: the seam is ONE #chat-log child directly above the current turn's user row, below the earlier answer",
    seamOk(),
    `seams=${seams().length} seamIdx=${kids().indexOf(log.querySelector(".compaction-seam"))} ` +
    `userIdx=${kids().indexOf(turnUser)}`);
  check("compaction-live: the seam carries the checkpoint id (Step 5's reload-dedupe handle)",
    seams().length === 1 && seams()[0].dataset.cp === "cp-probe-1",
    seams()[0] ? seams()[0].dataset.cp : "(gone)");
  check("compaction-live: the seam also carries data-anchor (the dedupe key that survives an empty checkpoint_id)",
    seams().length === 1 && seams()[0].dataset.anchor === "42",
    seams()[0] ? seams()[0].dataset.anchor : "(gone)");

  // The RULE, not just the node: a seam that is an empty 0-height div (the
  // compaction.css link dropped from index.html, or its class renamed on one
  // side) is an invisible line in production while every DOM assertion above
  // still passes. The colour is compared to the status dot, which carries the
  // same var(--accent) the plan names.
  const accentBg = getComputedStyle(row().querySelector(".cs-dot")).backgroundColor;
  const seamBg = seams()[0] ? getComputedStyle(seams()[0]).backgroundColor : "none";
  const seamH = seams()[0] ? parseFloat(getComputedStyle(seams()[0]).height) : 0;
  check("compaction-live: compaction.css paints the seam — a 2px rule in var(--accent), not an invisible empty div",
    seams().length === 1 && seamH >= 2 && seamBg === accentBg && seamBg !== "rgba(0, 0, 0, 0)",
    `seams=${seams().length} h=${seamH} bg=${seamBg} accent=${accentBg}`);

  // The phase is transient, not sticky: the next real phase relabels over it.
  deliver(SID, { type: "model_wait" });
  check("compaction-live: the next phase relabels off Compacting",
    label() === "Waiting for model…", `label="${label()}"`);

  // Orphans (cleric's note): compaction_start then a TERMINAL frame — no
  // compaction ever arrives; the terminal must hand the status row back.
  deliver(SID, { type: "compaction_start" });
  const spinA = spinning();
  deliver(SID, { type: "stopped" });
  check("compaction-orphan: stopped clears an orphaned Compacting phase",
    spinA && row().hidden, `wasSpinning=${spinA} hidden=${row().hidden}`);
  deliver(SID, { type: "compaction_start" });
  const spinB = spinning();
  deliver(SID, { type: "error", error: "summarizer died" });
  check("compaction-orphan: error clears an orphaned Compacting phase",
    spinB && row().hidden, `wasSpinning=${spinB} hidden=${row().hidden}`);

  // Positive control — the seam detector must be able to go red.
  const liveSeam = log.querySelector(".compaction-seam");
  if (liveSeam) liveSeam.remove();
  check("compaction-live control: the seam detector goes red when the node is removed",
    seamOk() === false);

  // =========================================================================
  // compaction-gapfill — the /live gap-fill seam (RENDERED whitelist). The
  // start + compaction emitted while hidden reached NO sink; the seam exists
  // only if paintGapFill is let through for `compaction`.
  // =========================================================================
  const SIDG = "probe-cp-gap";
  resetTranscript();
  statusStop();
  ui.sessionId = SIDG;
  ui.resync = null;
  window.__mock.sessions = [{ id: SIDG, title: "Cp gap run", channel: "web", active: true }];
  const lr = {
    sid: SIDG, runId: "probe-run-cpg", active: true, persistedSeq: 0, lastSeq: 0,
    events: [],
    messagesMid: [userTurn("PROBE_CP gap question")],
    messagesFull: [
      userTurn("PROBE_CP gap question"),
      {
        role: "assistant", text: "CPG done", tool_calls: [], tool_results: [],
        content_blocks: [{ type: "text", text: "CPG done" }],
      },
    ],
  };
  window.__mock.liveRun = lr;
  addUserMsg("PROBE_CP gap question");
  deliver(SIDG, emit(lr, { type: "delta", text: "CPG head" })); // seq 1, live, painted
  finalizeAgentBubble();
  ui.resync = { id: SIDG, seen: false }; // the socket died with the suspension
  suspend();
  emit(lr, { type: "compaction_start" }); // seq 2 — ring only
  const cp3 = emit(lr, { type: "compaction", checkpoint_id: "cp-probe-gap", anchor_idx: 9 }); // seq 3
  resume(); // resyncRun: /live?since=1 -> gap-fill paints the whitelisted seq 3
  await sleep(SETTLE_MS);
  const gapUser = log.querySelector(".msg.user");
  const gapOk = () => {
    const s = log.querySelector(".compaction-seam");
    return seams().length === 1 && !!s && s.nextElementSibling === gapUser;
  };
  check("compaction-gapfill: the /live gap-fill paints the seam (RENDERED whitelist) and compaction_start paints no transcript node",
    gapOk(), `seams=${seams().length} user=${!!gapUser}`);
  // The live sink re-delivering the seq the gap-fill already painted must
  // not double the seam (same dedupe as no-duplicate-words, seam edition).
  deliver(SIDG, cp3);
  check("compaction-gapfill: a re-delivered compaction (already-painted seq) paints no second seam",
    seams().length === 1, `seams=${seams().length}`);
  // Orphan across the cancel: spinner armed live, the run then ends with no
  // compaction frame — the run_done heal's abandonPendingRun clears it.
  deliver(SIDG, emit(lr, { type: "compaction_start" })); // seq 4 — no seq 5 ever
  const spinC = spinning();
  lr.active = false; // cancelled mid-summarizer: the run is over, store committed
  deliver(SIDG, { type: "run_done", run_id: "probe-run-cpg", seq: 4 });
  await sleep(SETTLE_MS); // the persisted heal renders (seam loss is Step 5's reload job)
  check("compaction-orphan: the run_done heal clears an orphaned Compacting phase (abandonPendingRun)",
    spinC && row().hidden, `wasSpinning=${spinC} hidden=${row().hidden}`);

  // =========================================================================
  // compaction-join — the OTHER replay seam: renderSession's /messages live
  // join (consumeLiveJoin). A mid-turn reload must show the seam too, above
  // the persisted half's user row.
  // =========================================================================
  const SIDJ = "probe-cp-join";
  resetTranscript();
  statusStop();
  ui.sessionId = SIDJ;
  ui.resync = null;
  window.__mock.sessions = [{ id: SIDJ, title: "Cp join run", channel: "web", active: true }];
  const lrj = {
    sid: SIDJ, runId: "probe-run-cpj", active: true, persistedSeq: 1, lastSeq: 0,
    events: [],
    messagesMid: [userTurn("PROBE_CP join question")],
    messagesFull: [],
  };
  window.__mock.liveRun = lrj;
  emit(lrj, { type: "compaction_start" }); // seq 1 — at/below the join boundary
  emit(lrj, { type: "compaction", checkpoint_id: "cp-probe-join", anchor_idx: 5 }); // seq 2 — the tail
  await renderSession(SIDJ);
  const joinUser = log.querySelector(".msg.user");
  const joinOk = () => {
    const s = log.querySelector(".compaction-seam");
    return seams().length === 1 && !!s && s.nextElementSibling === joinUser;
  };
  check("compaction-join: the /messages live join paints the seam above the persisted turn head too",
    joinOk(), `seams=${seams().length} user=${!!joinUser}`);
  const joinSeam = log.querySelector(".compaction-seam");
  if (joinSeam) joinSeam.remove();
  check("compaction-join control: the join detector goes red when the seam is removed",
    joinOk() === false);

  // =========================================================================
  // compaction-reload (Step 5) — the seam survives a page refresh.
  // renderSession arms anchored seams from the /messages `compactions`
  // payload (anchor_idx = STORE idx, the same space every row's `idx`
  // names), feeds the persisted-row loop each row's idx (dropSeamsTo), and
  // the live join flushes the remainder above the tail (flushSeams). The
  // mid-turn case offers ONE compaction to BOTH painters — compactions AND
  // the ring tail consumeLiveJoin paints through paintSeam; the data-anchor
  // / data-cp identity dedupe in compaction.js keeps it a single line at
  // the ANCHORED position (a live join alone can only approximate).
  // =========================================================================
  const reloadRows = (n) => [
    { role: "user", text: "PROBE_CP r question one", attachments: [], idx: 0 },
    {
      role: "assistant", text: "PROBE_CP r answer one", attachments: [], idx: 1,
      content_blocks: [{ type: "text", text: "PROBE_CP r answer one" }],
    },
    { role: "user", text: "PROBE_CP r question two", attachments: [], idx: 2 },
  ].slice(0, n);
  const reloadCp = (anchor, id) => ({
    checkpoint_id: id, anchor_idx: anchor,
    created_at: "2026-10-21T09:13:49+00:00", est_before: 90000, est_after: 21000,
  });

  // R1 — reload of a settled chat; the seam anchors above row idx 2 (the
  // second question), below the pre-cut answer.
  const SIDR = "probe-cp-reload";
  resetTranscript();
  statusStop();
  ui.sessionId = SIDR;
  ui.resync = null;
  window.__mock.liveRun = {
    sid: SIDR, runId: "probe-run-r1", active: false, persistedSeq: 0, lastSeq: 0,
    events: [], messagesMid: [], messagesFull: reloadRows(3),
    compactions: [reloadCp(2, "cp-reload-1")],
  };
  await renderSession(SIDR);
  const rUsers = [...log.querySelectorAll(".msg.user")];
  const reloadOk = () => {
    const s = log.querySelector(".compaction-seam");
    if (!s || seams().length !== 1 || s.parentElement !== log) return false;
    const k = kids();
    const ans = k.findIndex((c) => c.textContent.includes("PROBE_CP r answer one"));
    return s.nextElementSibling === rUsers[1] && k.indexOf(s) === k.indexOf(rUsers[1]) - 1 &&
      ans !== -1 && k.indexOf(s) > ans && s.getAttribute("role") === "separator";
  };
  check("compaction-reload: a refreshed chat paints ONE seam anchored above the anchor row (store idx 2), below the pre-cut answer",
    reloadOk(), `seams=${seams().length} users=${rUsers.length}`);
  const rSeam = log.querySelector(".compaction-seam");
  check("compaction-reload: the anchored seam carries both dedupe identities (data-cp + data-anchor)",
    !!rSeam && rSeam.dataset.cp === "cp-reload-1" && rSeam.dataset.anchor === "2",
    rSeam ? `cp=${rSeam.dataset.cp} anchor=${rSeam.dataset.anchor}` : "(gone)");
  if (rSeam) rSeam.remove();
  check("compaction-reload control: the reload detector goes red when the seam is removed",
    reloadOk() === false);

  // R2 — the same session WITHOUT compaction data renders no seam.
  const SIDN = "probe-cp-noseam";
  resetTranscript();
  ui.sessionId = SIDN;
  window.__mock.liveRun = {
    sid: SIDN, runId: "probe-run-r2", active: false, persistedSeq: 0, lastSeq: 0,
    events: [], messagesMid: [], messagesFull: reloadRows(3), compactions: [],
  };
  await renderSession(SIDN);
  check("compaction-reload: a session WITHOUT compaction data renders no seam",
    seams().length === 0 && log.querySelectorAll(".msg.user").length === 2,
    `seams=${seams().length}`);

  // R5 — a session that compacted TWICE (the real long-session shape: ~one
  // per 70k of growth): BOTH seams paint, each above its OWN anchor row, in
  // anchor order. Pins the payload as a LIST drained per row — a painter
  // that only ever consumed one entry would pass R1 alone.
  const SID2 = "probe-cp-reload-two";
  resetTranscript();
  ui.sessionId = SID2;
  const twoRows = [
    { role: "user", text: "PROBE_CP two q1", attachments: [], idx: 0 },
    { role: "assistant", text: "PROBE_CP two a1", attachments: [], idx: 1,
      content_blocks: [{ type: "text", text: "PROBE_CP two a1" }] },
    { role: "user", text: "PROBE_CP two q2", attachments: [], idx: 2 },
    { role: "assistant", text: "PROBE_CP two a2", attachments: [], idx: 3,
      content_blocks: [{ type: "text", text: "PROBE_CP two a2" }] },
  ];
  window.__mock.liveRun = {
    sid: SID2, runId: "probe-run-r5", active: false, persistedSeq: 0, lastSeq: 0,
    events: [], messagesMid: [], messagesFull: twoRows,
    compactions: [reloadCp(1, "cp-two-1"), reloadCp(3, "cp-two-2")],
  };
  await renderSession(SID2);
  const multiOk = () => {
    const k = kids();
    const ms = [...log.querySelectorAll(".compaction-seam")];
    const a = [...log.querySelectorAll(".msg.agent")];
    return ms.length === 2 && ms[0].nextElementSibling === a[0] &&
      ms[1].nextElementSibling === a[1] && k.indexOf(ms[0]) < k.indexOf(ms[1]) &&
      ms[0].dataset.anchor === "1" && ms[1].dataset.anchor === "3";
  };
  check("compaction-reload: a session that compacted TWICE paints both seams, each above its own anchor row, in anchor order",
    multiOk(),
    `seams=${seams().length} anchors=${[...seams()].map((s) => s.dataset.anchor).join(",")} ` +
    `agents=${log.querySelectorAll(".msg.agent").length}`);
  const multiSecond = [...seams()][1];
  if (multiSecond) multiSecond.remove();
  check("compaction-reload control: the two-seam detector goes red when the second seam is removed",
    multiOk() === false);

  // R3 — THE dedupe: a mid-turn reload where the compaction sits in
  // `compactions` AND in the ring tail (seq 2 > since). Anchor 1 = above
  // the answer, so a dedupe that skipped the WRONG offer (reload seam
  // dropped, join's before-the-last-user approximation painted) is red on
  // position even where a count would not catch every shape.
  const SIDM = "probe-cp-midturn";
  resetTranscript();
  ui.sessionId = SIDM;
  const lrm = {
    sid: SIDM, runId: "probe-run-m2", active: true, persistedSeq: 1, lastSeq: 0,
    events: [], messagesMid: reloadRows(3), messagesFull: [],
    compactions: [reloadCp(1, "cp-reload-3")],
  };
  window.__mock.liveRun = lrm;
  emit(lrm, { type: "delta", text: "CPM head" }); // seq 1 — at/below the join boundary
  emit(lrm, { type: "compaction", checkpoint_id: "cp-reload-3", anchor_idx: 1 }); // seq 2 — tail
  emit(lrm, { type: "delta", text: "CPM TAILWORD" }); // seq 3 — tail
  await renderSession(SIDM);
  finalizeAgentBubble(); // rAF dead: flush the paced tail bubble
  const dedupeOk = () => {
    if (seams().length !== 1) return false;
    const s = log.querySelector(".compaction-seam");
    const k = kids();
    return !!s && s.dataset.cp === "cp-reload-3" && k.indexOf(s) === 1 &&
      k.indexOf(s) + 1 === k.indexOf(k.find((c) => c.textContent.includes("PROBE_CP r answer one"))) &&
      log.textContent.includes("CPM TAILWORD");
  };
  check("compaction-reload dedupe: a compaction in BOTH compactions and the join tail paints ONE seam, at the anchored row (not the live approximation), and the tail still paints",
    dedupeOk(),
    `seams=${seams().length} seamIdx=${kids().indexOf(log.querySelector(".compaction-seam"))} ` +
    `tail=${log.textContent.includes("CPM TAILWORD")}`);

  // R4 — the flush path: a seam anchored past the last persisted row (the
  // race snapshot where the compaction event postdates the transcript read)
  // parks ABOVE the live tail.
  const SIDT = "probe-cp-trailing";
  resetTranscript();
  ui.sessionId = SIDT;
  const lrt = {
    sid: SIDT, runId: "probe-run-m3", active: true, persistedSeq: 1, lastSeq: 0,
    events: [], messagesMid: reloadRows(2), // idx 2 not in this snapshot
    messagesFull: [], compactions: [reloadCp(2, "cp-reload-4")],
  };
  window.__mock.liveRun = lrt;
  emit(lrt, { type: "delta", text: "CRT head" }); // seq 1 — persisted half's watermark
  emit(lrt, { type: "delta", text: "CRT TAILWORD" }); // seq 2 — tail, paints after the seam
  await renderSession(SIDT);
  finalizeAgentBubble();
  const tailBubble = [...log.querySelectorAll(".msg.agent")].pop();
  const tKids = kids();
  check("compaction-reload flush: a seam anchored past the last row parks above the live tail, and the tail paints after it",
    seams().length === 1 &&
      tKids.indexOf(log.querySelector(".compaction-seam")) === 2 &&
      tKids.indexOf(tailBubble) === 3 && log.textContent.includes("CRT TAILWORD"),
    `seams=${seams().length} seamIdx=${tKids.indexOf(log.querySelector(".compaction-seam"))} ` +
    `tailIdx=${tKids.indexOf(tailBubble)}`);

  // --- leave shared state exactly as probe_live / probe_coldstart expect ----
  resetTranscript();
  statusStop();
  ui.sessionId = null;
  ui.resync = null;
  window.__mock.liveRun = null;
  window.__mock.sessions = [];
  delete document.visibilityState; // back to the engine's own answer (visible)
}
