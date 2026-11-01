/* Live-resume scenarios (plan pwa-accurate-resume, Step 5; GREEN since the
   Step 6 client resync) — THE FOUR RESYNC ASSERTIONS. Run by probe.js after
   scenario H2, before the cold-start boots (scenario I stays last).

   These went green in Step 6 (webui/replay.js: seq slots, /live gap-fill,
   /live-predicate resume) and now pin the behaviour the seq contract (Steps
   3-4, tests/test_chat_recovery.py) makes possible. The mock (mock_fetch.js
   `liveRun`) models the server HONESTLY, timing included: while the run is active the persisted
   store NEVER contains the in-flight assistant step — a tool-loop run's
   rows cover only the model calls that already RETURNED — so the ring tail
   (`live.events` / GET /live?since=N) is the in-flight answer's only
   source. A client that heals by re-rendering from the store mid-call
   visibly loses the head; a client that replays a batch without seq-dedupe
   visibly doubles words. Every scenario drives a MULTI-STEP or live-tail
   run, so a single-call-only fix cannot pass them all.

   The four (README.md carries the full contract wording for Step 6):

     resume-mid-thinking   suspension mid-thinking: after resume the FULL
                           thinking-so-far is on screen — including the
                           deltas emitted while hidden, recoverable ONLY
                           from the ring — in the SAME block, and the live
                           tail continues. Never a reset, never a vanish.
     no-duplicate-words    a gap-fill batch overlapping what the live tail
                           painted mid-fetch paints nothing twice AND still
                           paints the word the live stream skipped — WD5 is
                           seq 5 while WD6 (seq 6) already painted live, so
                           "already painted" is the test, never a scalar
                           seq <= last-seen cutoff (which drops WD5).
     partial-head-survives the off-the-capped-page run reads as "finished
                           while away" to the session-row verdict — the
                           resume must branch on /live's ACTIVE PREDICATE
                           before trusting that, or a mid-call store render
                           wipes the partial head exactly like the reverted
                           2026-10-17 re-sync.
     cold-open-live        a fresh boot MID-ANSWER shows the live reply
                           (render `messages`, then replay `live.events`),
                           never just the persisted half; and the next
                           resume gap-fills from the CLIENT watermark —
                           `live.seq`, never `live.since`.

   Plus the six run-identity assertions (the identity rule, replay.js
   header): second-device-run and second-device-short-run (a batch naming
   another run — or an EMPTY batch whose seq restarted below my watermark —
   cold-joins; the new run's head is invisible to the old watermark),
   empty-tail-join-provisional (an empty-tail join holds no watermark and a
   new run's head cold-joins in), terminal-join-attribution (a named tail
   whose run ended during the fetch still attributes the slot — never a
   provisional slot holding live.seq), late-old-run-title (a terminal run's
   late event relabels but never resets the newer slot), and missing-route
   (/live 404 = the {missing:true} sentinel, not offline, not finished).
   README.md carries the full wording.

   Headless recipes inherited from probe.js/probe_coldstart.js: rAF is dead
   under --virtual-time-budget (flush with finalizeAgentBubble()), boots are
   `import("./app.js?coldboot=N")` with the SW stubbed, and every fetch is
   the delayed mock. Suspension is REAL to the app: document.visibilityState
   is overridden and a `visibilitychange` event dispatched, so production's
   own handlers make the decisions under test. */
"use strict";

import { ui } from "./state.js";
import { fetchLive } from "./replay.js";
import { addUserMsg, finalizeAgentBubble, renderSession, resetTranscript } from "./transcript.js";

const FETCH_SETTLE_MS = 350; // > mock delayMs 150, with slack

// --- suspension: the production visibilitychange path ------------------------
const setVis = (v) =>
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => v });
const suspend = () => {
  setVis("hidden");
  document.dispatchEvent(new Event("visibilitychange"));
};
const resume = () => {
  setVis("visible");
  document.dispatchEvent(new Event("visibilitychange"));
};

// --- the mock run: emit = the server writes the ring at emit -----------------
const startRun = ({ sid, runId, persistedSeq, messagesMid, messagesFull }) => {
  window.__mock.liveRun = {
    sid,
    runId,
    active: true,
    persistedSeq,
    lastSeq: 0,
    events: [],
    messagesMid,
    messagesFull,
  };
};
// Server-side emit: stamp once (seq per RUN restarts at 1, paired run_id),
// record in the ring. The returned stamped event is what a live SINK would
// have carried — deliver() hands a copy to the client's /api/events view.
const emit = (ev) => {
  const lr = window.__mock.liveRun;
  const stamped = { ...ev, seq: ++lr.lastSeq, run_id: lr.runId };
  lr.events.push(stamped);
  return stamped;
};
// The /api/events view of an event (the reroute tags session_id; the ring
// copy never carries it — faithful to chat_run.py's two sink shapes).
let DELIVER_SID = null;
const deliver = (ev) => window.__sk.handleRunEvent({ ...ev, session_id: DELIVER_SID });

const countIn = (hay, needle) => hay.split(needle).length - 1;
const stripPreview = (s) => (s ? s.querySelector(".think-preview").textContent : "");
const page50 = () =>
  Array.from({ length: 50 }, (_, i) => ({ id: `pad${i}`, title: `Pad ${i}`, channel: "web" }));
const userTurn = (text) => ({ role: "user", text, attachments: [] });

// A TOOL-LOOP store mid-call-2: call 1's rows (text + tool_use + the tool
// row) are persisted; call 2's answer is in flight — absent from the store
// (TIMING!), present only in the ring tail. Mirrors ToolLoopAgent in
// tests/test_chat_recovery.py (that file IS the contract).
const loopMessagesMid = (q) => [
  userTurn(q),
  {
    role: "assistant",
    text: "PLAN1 step",
    tool_calls: [{ id: "tc1", name: "probe_tool", arguments: { a: 1 } }],
    content_blocks: [
      { type: "text", text: "PLAN1 step" },
      { type: "tool_use", id: "tc1", name: "probe_tool", input: { a: 1 } },
    ],
  },
  { role: "tool", tool_results: [{ tool_call_id: "tc1", content: "ok", is_error: false }] },
];
const loopMessagesFull = (q, tail) => [
  ...loopMessagesMid(q),
  {
    role: "assistant",
    text: tail,
    tool_calls: [],
    tool_results: [],
    content_blocks: [
      { type: "thinking", thinking: "THK full" },
      { type: "text", text: tail },
    ],
  },
];

export async function runLiveResume({ check, sleep }) {
  const log = document.querySelector("#chat-log");

  // --- controls: the instruments these four checks lean on -------------------
  // The /live mock route answers, filters by since, and RECORDS the call —
  // else the watermark pins below would be vacuously red on a missing route.
  window.__mock.liveCalls.length = 0;
  startRun({
    sid: "probe-live-control", runId: "probe-run-ctl", persistedSeq: 2,
    messagesMid: [userTurn("q")], messagesFull: [userTurn("q")],
  });
  emit({ type: "delta", text: "CTL1 " });
  emit({ type: "delta", text: "CTL2 " });
  emit({ type: "delta", text: "CTL3 " });
  const ctl = await (await fetch("/api/chat/probe-live-control/live?since=2")).json();
  const ctlOff = await (await fetch("/api/chat/other-session/live?since=0")).json();
  // -5 must read as since=0 (event_ring.replay clamps), not as a gap.
  const ctlNeg = await (await fetch("/api/chat/probe-live-control/live?since=-5")).json();
  // ...and the run is THIS session's: another session's transcript must not
  // receive its `live` (the real route reads the path's session from the store).
  const ctlOther = await (await fetch("/api/sessions/other-session/messages")).json();
  check("live-mock control: /live answers active, filters since>2, records the call, clamps since, is session-scoped",
    ctl.active === true && ctl.seq === 3 && ctl.gap === false &&
      ctl.events.length === 1 && ctl.events[0].seq === 3 &&
      ctl.events[0].run_id === "probe-run-ctl" &&
      window.__mock.liveCalls.length === 3 && window.__mock.liveCalls[0].since === 2 &&
      window.__mock.liveCalls[2].since === 0 &&
      ctlNeg.active === true && ctlNeg.gap === false && ctlNeg.events.length === 3 &&
      ctlOff.active === false && ctlOther.live === null,
    JSON.stringify(ctl && { a: ctl.active, s: ctl.seq, n: ctl.events.length }) +
      ` neg=${JSON.stringify(ctlNeg && { g: ctlNeg.gap, n: ctlNeg.events.length })}` +
      ` other.live=${JSON.stringify(ctlOther.live)}`);
  // The word-counter must see a doubled token, or "each marker appears
  // exactly once" would be vacuous.
  const dbl = "CTLX-dup";
  log.appendChild(document.createTextNode(`${dbl} ${dbl}`));
  check("live-mock control: the marker counter sees a doubled word",
    countIn(log.textContent, dbl) === 2, `count=${countIn(log.textContent, dbl)}`);
  log.innerHTML = "";
  window.__mock.liveRun = null;
  window.__mock.liveCalls.length = 0;

  // =========================================================================
  // resume-mid-thinking — suspension MID-THINKING.
  // The strip streamed A-deltas pre-suspension; B-deltas were emitted while
  // hidden (the /api/events socket is CLOSED when hidden — those events are
  // gone from every sink, recoverable only from the ring); after resume the
  // run keeps streaming C + the answer. The full thinking-so-far must be on
  // screen in ONE block: a gap-fill that misses B leaves a hole (RED today);
  // a re-render that rebuilds the transcript loses A (the wiped-DOM +
  // stale-watermark trap — see README).
  // =========================================================================
  const SID_T = "probe-live-think";
  DELIVER_SID = SID_T;
  resetTranscript();
  ui.sessionId = SID_T;
  ui.resync = null;
  window.__mock.sessions = [{ id: SID_T, title: "Live think run", channel: "web", active: true }];
  startRun({
    sid: SID_T, runId: "probe-run-think", persistedSeq: 0,
    messagesMid: [userTurn("PROBE_LIVE think question")],
    messagesFull: [userTurn("PROBE_LIVE think question"), {
      role: "assistant", text: "RK-ANS1 answer after resume", tool_calls: [], tool_results: [],
      content_blocks: [
        { type: "thinking", thinking: "RK-A1 RK-A2 RK-B1 RK-B2 RK-C1 " },
        { type: "text", text: "RK-ANS1 answer after resume" },
      ],
    }],
  });
  addUserMsg("PROBE_LIVE think question");
  deliver(emit({ type: "thinking_delta", text: "RK-A1 " })); // seq 1, painted live
  deliver(emit({ type: "thinking_delta", text: "RK-A2 " })); // seq 2 — client watermark 2
  ui.resync = { id: SID_T, seen: false }; // the socket died with the suspension
  suspend();
  emit({ type: "thinking_delta", text: "RK-B1 " }); // seq 3 — EMITTED WHILE HIDDEN:
  emit({ type: "thinking_delta", text: "RK-B2 " }); // seq 4 — no sink ever held them
  const msgsBeforeT = window.__mock.calls.filter((p) => p.indexOf("/messages") !== -1).length;
  resume(); // Step 6: the resync fetches /live?since=2 and fills B into the OPEN strip
  await sleep(FETCH_SETTLE_MS);
  deliver(emit({ type: "thinking_delta", text: "RK-C1 " })); // seq 5: live tail continues
  deliver(emit({ type: "delta", text: "RK-ANS1 answer after resume" })); // seq 6 closes the strip
  finalizeAgentBubble(); // rAF is dead headless
  // An ACTIVE resume must not refetch /messages (the MiB-scale mid-call store
  // render — the re-pull rule). Without this the four greens stay green even
  // if the resume re-renders from the store + live join, losing the branch
  // the step exists for.
  const noStoreRefetchT = () =>
    window.__mock.calls.filter((p) => p.indexOf("/messages") !== -1).length === msgsBeforeT;
  const l1Pred = () => {
    const strips = log.querySelectorAll(".think");
    if (strips.length !== 1) return false; // never reset (2 = respawn), never vanished (0)
    const prev = stripPreview(strips[0]); // single-line thinking → the full text
    const order = ["RK-A2", "RK-B1", "RK-B2", "RK-C1"].map((m) => prev.indexOf(m));
    return order.every((i) => i >= 0) && order.every((i, k) => k === 0 || i > order[k - 1]) &&
      log.textContent.includes("RK-ANS1") &&
      ["RK-A1", "RK-A2", "RK-B1", "RK-B2", "RK-C1", "RK-ANS1"]
        .every((m) => countIn(log.textContent, m) === 1) &&
      noStoreRefetchT();
  };
  check("resume-mid-thinking: full thinking-so-far survives suspension and the live tail continues (one block, gap-filled, nothing doubled, no mid-call store refetch)",
    l1Pred(),
    `strips=${log.querySelectorAll(".think").length} preview=${stripPreview(log.querySelector(".think")).slice(0, 60) || "(gone)"} storeRefetch=${!noStoreRefetchT()}`);
  log.innerHTML = "";
  check("resume-mid-thinking control: the on-screen detector goes red on an emptied transcript",
    l1Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // no-duplicate-words — the gap-fill batch overlaps the live tail.
  // Resume fetches /live?since=3; while that fetch is in flight the queue
  // event WD4 (in the batch snapshot too) and a freshly-emitted live event
  // WD6 (NOT in it) paint through /api/events. The batch then lands ON TOP:
  // dedupe must drop WD4 and still paint WD5 — a seq BELOW the high-water
  // mark WD6 set, because the live stream skipped it. WD5 proves the
  // gap-fill ran; WD4/WD6 once-each proves it deduped. A scalar
  // `seq <= last-seen` cutoff drops WD5 (a lost word until run_done): only an
  // already-painted test — or buffering live events across the resync —
  // keeps both once (README "no-duplicate-words").
  // =========================================================================
  const SID_W = "probe-live-words";
  DELIVER_SID = SID_W;
  resetTranscript();
  ui.sessionId = SID_W;
  window.__mock.sessions = [{ id: SID_W, title: "Live words run", channel: "web", active: true }];
  startRun({
    sid: SID_W, runId: "probe-run-words", persistedSeq: 0,
    messagesMid: [userTurn("PROBE_LIVE words question")],
    messagesFull: [userTurn("PROBE_LIVE words question"), {
      role: "assistant", text: "WD1 WD2 WD3 WD4 WD5 WD6", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "WD1 WD2 WD3 WD4 WD5 WD6" }],
    }],
  });
  addUserMsg("PROBE_LIVE words question");
  deliver(emit({ type: "delta", text: "WD1 " })); // seq 1..3 painted live:
  deliver(emit({ type: "delta", text: "WD2 " })); // client watermark 3 (Step 6).
  deliver(emit({ type: "delta", text: "WD3 " }));
  finalizeAgentBubble();
  ui.resync = { id: SID_W, seen: false };
  suspend();
  // WD4 reaches no live sink while hidden, so it is in the /live snapshot
  // taken at resume AND delivered here on the reopened /api/events during the
  // fetch. WD5 reaches NO sink at all: the batch is its only source.
  const e4 = emit({ type: "delta", text: "WD4 " }); // seq 4
  emit({ type: "delta", text: "WD5 " });            // seq 5 — ring only
  resume();
  await sleep(40); // Step 6's /live?since=3 is in flight (150ms mock latency)
  deliver(e4); // WD4 on the reopened /api/events during the fetch
  deliver(emit({ type: "delta", text: "WD6 " })); // seq 6: live, socket, during the fetch
  await sleep(FETCH_SETTLE_MS);
  finalizeAgentBubble();
  const words = ["WD1", "WD2", "WD3", "WD4", "WD5", "WD6"];
  const l2Counts = () => words.map((w) => countIn(log.textContent, w)).join(",");
  check("no-duplicate-words: gap-fill overlapping the live tail paints no event twice",
    words.every((w) => countIn(log.textContent, w) === 1),
    `counts[${words.join(",")}]=[${l2Counts()}]; ` +
    `live calls after resume: ${JSON.stringify(window.__mock.liveCalls.slice(0).slice(-2))}`);
  log.innerHTML = "";
  check("no-duplicate-words control: the once-each detector goes red on an emptied transcript",
    words.every((w) => countIn(log.textContent, w) === 1) === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // partial-head-survives — the capped-page "finished while away" verdict.
  // A TOOL-LOOP run mid-call-2: the client painted the persisted step (the
  // pill) AND the in-flight head live; then the socket died. The session row
  // is OFF the capped 50-page, so the visibility handler's row-membership
  // verdict says finished — and today that triggers a store-based recovery
  // render which wipes the on-screen head (the store lacks the in-flight
  // step: the 2026-10-17 lossy path, structurally). The resume must branch
  // on /live's ACTIVE predicate (the run is live!) and fill the ring tail
  // instead of re-rendering from the store. HEAD1/HEAD2 must survive once,
  // the persisted pill must stay painted once (never double-painted from a
  // replay that forgot persisted rows carry no seq), and the tail must
  // continue.
  // =========================================================================
  const SID_H = "probe-live-head";
  DELIVER_SID = SID_H;
  resetTranscript();
  ui.sessionId = SID_H;
  window.__mock.sessions = page50(); // the run's row is NOT on the page (≈950-row live DB)
  startRun({
    sid: SID_H, runId: "probe-run-head", persistedSeq: 4,
    messagesMid: loopMessagesMid("PROBE_LIVE head question"),
    messagesFull: loopMessagesFull("PROBE_LIVE head question", "HEAD1 HEAD2 HEAD3 TAIL1 TAIL2"),
  });
  addUserMsg("PROBE_LIVE head question");
  deliver(emit({ type: "model_wait" }));                                   // seq 1
  deliver(emit({ type: "delta", text: "PLAN1 step" }));                    // seq 2  \ persisted
  deliver(emit({ type: "tool_call", tool: "probe_tool", args: { a: 1 } })); // seq 3  | (persistedSeq 4)
  deliver(emit({ type: "tool_result", tool: "probe_tool", is_error: false, digest: "ok" })); // seq 4 /
  deliver(emit({ type: "model_wait" }));                                   // seq 5  \ in flight:
  deliver(emit({ type: "thinking_delta", text: "THK-P1 " }));              // seq 6  | absent from
  deliver(emit({ type: "delta", text: "HEAD1 HEAD2 partial head, live when the socket died" })); // seq 7 / the store
  finalizeAgentBubble();
  ui.resync = { id: SID_H, seen: false };
  suspend();
  emit({ type: "delta", text: " HEAD3" });  // seq 8, 9: emitted while hidden —
  emit({ type: "delta", text: " TAIL1" });  // ring-only, fillable via /live only
  const msgsBeforeH = window.__mock.calls.filter((p) => p.indexOf("/messages") !== -1).length;
  resume(); // today: off-page row → treated finished → store render WIPES the head
  await sleep(FETCH_SETTLE_MS); // the (wrongly) armed /messages fetch has landed
  deliver(emit({ type: "delta", text: " TAIL2 answer finished after resume" })); // seq 10
  finalizeAgentBubble();
  const l3Pred = () => {
    const txt = log.textContent;
    return txt.includes("HEAD1") && txt.includes("HEAD2") &&
      countIn(txt, "HEAD1") === 1 && countIn(txt, "PLAN1") === 1 &&
      txt.includes("TAIL2") && countIn(txt, "TAIL2") === 1 &&
      log.querySelectorAll(".pill").length === 1 && !log.querySelector(".msg.error") &&
      // ACTIVE ⇒ gap-fill into the existing DOM, never a mid-call
      // /messages store render (the re-pull rule; a render + live join can
      // also reconstruct the head, so the DOM assertions alone miss it).
      window.__mock.calls.filter((p) => p.indexOf("/messages") !== -1).length === msgsBeforeH;
  };
  check("partial-head-survives: a resume never wipes the on-screen partial answer head (branch on /live active, not the capped page's row membership, and no mid-call store refetch)",
    l3Pred(),
    `HEAD1=${log.textContent.includes("HEAD1")} HEAD2=${log.textContent.includes("HEAD2")} ` +
    `PLAN1=${countIn(log.textContent, "PLAN1")} pills=${log.querySelectorAll(".pill").length} ` +
    `TAIL2=${log.textContent.includes("TAIL2")} storeRefetch=${window.__mock.calls.filter((p) => p.indexOf("/messages") !== -1).length !== msgsBeforeH}`);
  log.innerHTML = "";
  check("partial-head-survives control: the head detector goes red on an emptied transcript",
    l3Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // reopen-dedupe — a NORMAL reopen mid-run. An event emitted BEFORE the
  // /messages fetch is sampled but DELIVERED on the reopened /api/events
  // DURING the fetch is BOTH in the join's live tail and in the replay
  // buffer. The flush must skip it: joining and then flushing it repaints the
  // word. (no-duplicate-words pins the /live gap-fill dedupe; this pins
  // renderSession's join + flush seam — a reopen, not a suspension.)
  // =========================================================================
  const SID_R = "probe-live-reopen";
  DELIVER_SID = SID_R;
  resetTranscript();
  ui.sessionId = SID_R;
  window.__mock.sessions = [{ id: SID_R, title: "Reopen run", channel: "web", active: true }];
  startRun({
    sid: SID_R, runId: "probe-run-reopen", persistedSeq: 0,
    messagesMid: [userTurn("PROBE_LIVE reopen question")],
    messagesFull: [userTurn("PROBE_LIVE reopen question"), {
      role: "assistant", text: "RP1 RP2", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "RP1 RP2" }],
    }],
  });
  addUserMsg("PROBE_LIVE reopen question");
  deliver(emit({ type: "delta", text: "RP1 " })); // seq 1, painted live on the old DOM
  const rp2 = emit({ type: "delta", text: "RP2 " }); // seq 2 — ALSO in the join below
  const reopen = renderSession(SID_R); // /messages samples live [1,2] now
  await sleep(40);                     // render still awaiting (mock delayMs 150)
  deliver(rp2); // reached the sink during the fetch: buffered, and in the tail
  await reopen;
  await sleep(20);
  finalizeAgentBubble();
  const l5Pred = () => {
    const txt = log.textContent;
    return countIn(txt, "RP1") === 1 && countIn(txt, "RP2") === 1;
  };
  check("reopen-dedupe: a normal reopen does not repaint an event the /messages live join already painted",
    l5Pred(), `RP1=${countIn(log.textContent, "RP1")} RP2=${countIn(log.textContent, "RP2")}`);
  log.innerHTML = "";
  check("reopen-dedupe control: the once-each detector goes red on an emptied transcript",
    l5Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // seq-restart-new-run-paints — a SECOND run on the same session restarts at
  // seq 1. The slot still holds run one's painted interval [1..2] and the
  // sink gate tests alreadyPainted BEFORE markPainted's run-change reset, so
  // unless the membership test is run_id-aware the new run's first tokens are
  // swallowed (the normal chat-send path never resets the slot between turns:
  // no resetTranscript, no reopen). Reachable on every second message.
  // =========================================================================
  const SID_N = "probe-live-run2";
  DELIVER_SID = SID_N;
  resetTranscript();
  ui.sessionId = SID_N;
  window.__mock.sessions = [{ id: SID_N, title: "Run two", channel: "web", active: true }];
  startRun({
    sid: SID_N, runId: "probe-run-one", persistedSeq: 0,
    messagesMid: [userTurn("q")], messagesFull: [],
  });
  addUserMsg("PROBE_LIVE run-two question");
  deliver(emit({ type: "delta", text: "R1A " })); // run one, seq 1
  deliver(emit({ type: "delta", text: "R1B " })); // run one, seq 2 — slot [1..2]
  finalizeAgentBubble();
  // The turn ends; the next send is a NEW run, its seq space restarting at 1.
  const lr2 = window.__mock.liveRun;
  lr2.runId = "probe-run-two";
  lr2.lastSeq = 0;
  lr2.events = [];
  deliver(emit({ type: "delta", text: "R2A " })); // run two, seq 1 — must paint
  finalizeAgentBubble();
  const l6Pred = () => {
    const txt = log.textContent;
    return countIn(txt, "R1A") === 1 && countIn(txt, "R1B") === 1 &&
      countIn(txt, "R2A") === 1;
  };
  check("seq-restart-new-run-paints: a new run's seq 1.. paints despite the previous run's painted intervals",
    l6Pred(),
    `R1A=${countIn(log.textContent, "R1A")} R1B=${countIn(log.textContent, "R1B")} ` +
    `R2A=${countIn(log.textContent, "R2A")}`);
  log.innerHTML = "";
  check("seq-restart-new-run-paints control: the new-run detector goes red without R2A",
    l6Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // second-device-run — a SECOND run started ELSEWHERE while I was hidden.
  // Run A's head painted live, the stream died, and while hidden: A ended
  // (its run_done died with the socket — this client never learns A is over),
  // the store committed, and run B started on another device. My resume asks
  // /live at A's watermark: the batch names run B, and seq restarted — E1
  // (B's seq 1) is INVISIBLE to since=1, exactly the skip the identity rule
  // forbids gap-filling through. The repair is the one-fetch live JOIN:
  // rows (A whole) + tail (B 1..2), slot re-primed on B, resync cleared
  // (A's heal IS this join). Then the carried post-run_done `title` publisher
  // fires for run A: it must relabel the picker WITHOUT resetting B's slot —
  // proven behaviorally by the next resume asking at B's watermark (since=2)
  // and painting E3 once: a reset slot would ask unnumbered and repaint
  // E1/E2 over the joined DOM.
  // =========================================================================
  const SID_D = "probe-live-2dev";
  DELIVER_SID = SID_D;
  resetTranscript();
  ui.sessionId = SID_D;
  window.__mock.sessions = [{ id: SID_D, title: "Two-device chat", channel: "web", active: true }];
  startRun({
    sid: SID_D, runId: "probe-run-A", persistedSeq: 0,
    messagesMid: [userTurn("PROBE_LIVE 2dev question")],
    messagesFull: [], // never answered after the run: the join lands MID-B, live
  });
  addUserMsg("PROBE_LIVE 2dev question");
  deliver(emit({ type: "delta", text: "D1 head of run A" })); // A seq 1, painted live
  finalizeAgentBubble();
  ui.resync = { id: SID_D, seen: false };
  suspend();
  const lrD = window.__mock.liveRun; // hidden: A ends (run_done never arrives),
  lrD.messagesMid = [ // the store commits, and B starts ELSEWHERE:
    userTurn("PROBE_LIVE 2dev question"),
    {
      role: "assistant", text: "D1 head of run A", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "D1 head of run A" }],
    },
  ];
  lrD.runId = "probe-run-B";
  lrD.lastSeq = 0;
  lrD.events = [];
  emit({ type: "delta", text: "E1 run B head " }); // B seq 1 — invisible to since=1
  emit({ type: "delta", text: "E2 run B tail" }); // B seq 2 — the batch's only event
  const callsBeforeD = window.__mock.liveCalls.length;
  const msgBeforeD = window.__mock.calls.filter((x) => x.includes("/messages")).length;
  resume(); // resume: /live?since=1 names run B -> cold-JOIN (two fetches deep)
  await sleep(700); // /live (150) + the join's /messages (150), with slack
  finalizeAgentBubble();
  const joinCalls = window.__mock.liveCalls.slice(callsBeforeD);
  const m1Pred = () => {
    const txt = log.textContent;
    return countIn(txt, "D1") === 1 && countIn(txt, "E1") === 1 && countIn(txt, "E2") === 1 &&
      joinCalls.some((c) => c.sid === SID_D && c.since === 1) && // asked at A's number
      window.__mock.calls.filter((x) => x.includes("/messages")).length > msgBeforeD &&
      ui.resync === null && window.__mock.liveRun.active === true;
  };
  check("second-device-run: a /live batch naming a different run cold-joins — the new run's head (invisible to the old watermark) paints once, the old head once, resync clears",
    m1Pred(),
    `D1=${countIn(log.textContent, "D1")} E1=${countIn(log.textContent, "E1")} ` +
    `E2=${countIn(log.textContent, "E2")} join calls=${JSON.stringify(joinCalls)} ` +
    `resync=${JSON.stringify(ui.resync)}`);
  // Face 2: the carried post-run_done title publisher, run A's name. The
  // relabel must be read BEFORE any loadSessions rebuild (resume refreshes
  // the picker from the store) — captured now, it proves handleRunEvent
  // PROCESSED the event (the terminal guard skipped only the slot, not the
  // event). The store rename mirrors what the server did when it published.
  deliver({ type: "title", title: "Renamed late", seq: 7, run_id: "probe-run-A" });
  const optLate = document.querySelector(`#session-select option[value="${SID_D}"]`);
  const relabeled = optLate !== null && optLate.textContent === "Renamed late";
  window.__mock.sessions = [{ id: SID_D, title: "Renamed late", channel: "web", active: true }];
  emit({ type: "delta", text: " E3 after the late title" }); // B seq 3, hidden window
  const callsBeforeLate = window.__mock.liveCalls.length;
  const msgsBeforeLate = window.__mock.calls.filter((x) => x.includes("/messages")).length;
  suspend();
  await sleep(20);
  resume(); // must ask /live?since=2 (B's watermark survived the title)
  await sleep(FETCH_SETTLE_MS);
  finalizeAgentBubble();
  const titleCalls = window.__mock.liveCalls.slice(callsBeforeLate);
  const m2Pred = () => {
    const txt = log.textContent;
    return relabeled === true &&
      titleCalls.some((c) => c.sid === SID_D && c.since === 2) && // B's number, not the title's 7
      window.__mock.calls.filter((x) => x.includes("/messages")).length === msgsBeforeLate &&
      countIn(txt, "D1") === 1 && countIn(txt, "E1") === 1 &&
      countIn(txt, "E2") === 1 && countIn(txt, "E3") === 1;
  };
  check("late-old-run-title: a terminal run's title relabels the picker without resetting the newer run's slot (next resume keeps B's watermark; nothing repaints)",
    m2Pred(),
    `relabeled=${relabeled} resume=${JSON.stringify(titleCalls)} D1=${countIn(log.textContent, "D1")} ` +
    `E1=${countIn(log.textContent, "E1")} E2=${countIn(log.textContent, "E2")} ` +
    `E3=${countIn(log.textContent, "E3")}`);
  log.innerHTML = "";
  check("second-device-run control: both detectors go red on an emptied transcript",
    m1Pred() === false && m2Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // second-device-short-run — face 1's HARD half: the new run is SHORTER than
  // my watermark, so /live?since=<old water> returns an EMPTY batch. Nothing
  // in the batch names a run; the response's own `seq` is the identity
  // witness — seq is monotonic PER RUN, so seq < my watermark cannot be my
  // run. Without that read the client calls itself "caught up" and the new
  // head stays invisible until run_done (which a detached run may never send).
  // =========================================================================
  const SID_S = "probe-live-short";
  DELIVER_SID = SID_S;
  resetTranscript();
  ui.sessionId = SID_S;
  window.__mock.sessions = [{ id: SID_S, title: "Short new run", channel: "web", active: true }];
  startRun({
    sid: SID_S, runId: "probe-run-sA", persistedSeq: 0,
    messagesMid: [userTurn("PROBE_LIVE short question")],
    messagesFull: [],
  });
  addUserMsg("PROBE_LIVE short question");
  deliver(emit({ type: "delta", text: "S1 " })); // run A seq 1
  deliver(emit({ type: "delta", text: "S2 " })); // A seq 2
  deliver(emit({ type: "delta", text: "S3 " })); // A seq 3 — client watermark 3
  finalizeAgentBubble();
  ui.resync = { id: SID_S, seen: false };
  suspend();
  const lrS = window.__mock.liveRun; // hidden: A ends (no run_done reaches us),
  lrS.messagesMid = [ // the store commits A, and B starts ELSEWHERE:
    userTurn("PROBE_LIVE short question"),
    {
      role: "assistant", text: "S1 S2 S3", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "S1 S2 S3" }],
    },
    userTurn("short follow-up"),
  ];
  lrS.runId = "probe-run-sB";
  lrS.lastSeq = 0;
  lrS.events = [];
  lrS.persistedSeq = 0;
  emit({ type: "delta", text: "T1 run B head " }); // B seq 1 — below 3
  emit({ type: "delta", text: "T2 run B tail" }); // B seq 2 — still below 3
  const msgBeforeS = window.__mock.calls.filter((x) => x.includes("/messages")).length;
  resume(); // /live?since=3 -> EMPTY active batch (seq 2); must still cold-join
  await sleep(700);
  finalizeAgentBubble();
  const m3Pred = () => {
    const txt = log.textContent;
    return countIn(txt, "S1") === 1 && countIn(txt, "S2") === 1 &&
      countIn(txt, "S3") === 1 && countIn(txt, "T1") === 1 && countIn(txt, "T2") === 1 &&
      window.__mock.calls.filter((x) => x.includes("/messages")).length > msgBeforeS &&
      ui.resync === null;
  };
  check("second-device-short-run: an EMPTY /live batch whose seq is BELOW my watermark cold-joins (a short new run's head is invisible to the old number)",
    m3Pred(),
    `S1=${countIn(log.textContent, "S1")} T1=${countIn(log.textContent, "T1")} ` +
    `T2=${countIn(log.textContent, "T2")} resync=${JSON.stringify(ui.resync)}`);
  log.innerHTML = "";
  check("second-device-short-run control: the detector goes red on an emptied transcript",
    m3Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // empty-tail-join-provisional — face 3: a mid-run cold open where every
  // emitted event is PERSISTED (live.events == []) names NO run. The join
  // must leave a PROVISIONAL slot (no marks, no watermark): unattributed head
  // marks would answer "painted" for a NEW run's seq 1..N. Behavioural proof:
  // a resume with a provisional slot asks the WHOLE window (no since — it has
  // no watermark), the batch names another run, and the cold join paints that
  // run's head. Pre-rule, the head-marked slot asked since=3, got an empty
  // batch, and the new head stayed invisible.
  // =========================================================================
  const SID_P = "probe-live-prov";
  DELIVER_SID = SID_P;
  resetTranscript();
  ui.sessionId = SID_P;
  window.__mock.sessions = [{ id: SID_P, title: "Provisional chat", channel: "web", active: true }];
  startRun({
    sid: SID_P, runId: "probe-run-pA", persistedSeq: 3,
    messagesMid: [userTurn("PROBE_LIVE prov question"), {
      role: "assistant", text: "P1 P2 P3", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "P1 P2 P3" }],
    }],
    messagesFull: [],
  });
  emit({ type: "delta", text: "P1 " }); // seq 1..3 — ALL persisted (tail empty)
  emit({ type: "delta", text: "P2 " });
  emit({ type: "delta", text: "P3 " });
  await renderSession(SID_P); // mid-run cold open: the join names no run
  await sleep(20);
  finalizeAgentBubble();
  // The carried post-run_done `title` publisher for run pA, arriving while the
  // slot is still PROVISIONAL. The terminal note must stop it (markPainted):
  // a provisional slot that takes a dead run's seq as a WATERMARK is the
  // forbidden state — the next resume would ask since=7 and skip B entirely.
  deliver({ type: "run_done", run_id: "probe-run-pA", seq: 4 });
  deliver({ type: "title", title: "Late pA title", seq: 7, run_id: "probe-run-pA" });
  const lrP = window.__mock.liveRun; // hidden: run B starts ELSEWHERE
  lrP.runId = "probe-run-pB";
  lrP.lastSeq = 0;
  lrP.events = [];
  lrP.persistedSeq = 0;
  lrP.messagesMid = [
    userTurn("PROBE_LIVE prov question"),
    {
      role: "assistant", text: "P1 P2 P3", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "P1 P2 P3" }],
    },
    userTurn("second question"),
  ];
  emit({ type: "delta", text: "Q1 run B head " }); // seq 1
  emit({ type: "delta", text: "Q2 run B tail" }); // seq 2
  const callsP = window.__mock.liveCalls.length;
  suspend();
  await sleep(20);
  resume();
  await sleep(700); // /live (150) + the cold join's /messages (150), with slack
  finalizeAgentBubble();
  const provCalls = window.__mock.liveCalls.slice(callsP);
  const m4Pred = () => {
    const txt = log.textContent;
    return provCalls.some((c) => c.sid === SID_P && c.since === 0) &&
      countIn(txt, "P1") === 1 && countIn(txt, "P2") === 1 && countIn(txt, "P3") === 1 &&
      countIn(txt, "Q1") === 1 && countIn(txt, "Q2") === 1;
  };
  check("empty-tail-join-provisional: an empty-tail join holds no watermark (the next resume asks the whole window) and a new run's head cold-joins in",
    m4Pred(),
    `P1=${countIn(log.textContent, "P1")} Q1=${countIn(log.textContent, "Q1")} ` +
    `Q2=${countIn(log.textContent, "Q2")} live calls=${JSON.stringify(provCalls)}`);
  log.innerHTML = "";
  check("empty-tail-join-provisional control: the detector goes red on an emptied transcript",
    m4Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // =========================================================================
  // terminal-join-attribution — face 3's SECOND seam: a NAMED tail whose run
  // ends DURING the /messages fetch. syncRun refuses terminal runs (a late
  // event must never reset a newer slot), but here the payload is the only
  // witness — it must attribute the fresh slot. Otherwise the join leaves a
  // PROVISIONAL slot (runId null) that then takes `live.seq` as a watermark:
  // the identity rule's forbidden state, reached without the empty-tail
  // branch. Asserted on the slot itself; the water is the payload's run's, so
  // a later batch is judged by run_id (and the seq witness), never unattributed.
  // =========================================================================
  const SID_Q = "probe-live-termjoin";
  DELIVER_SID = SID_Q;
  resetTranscript();
  ui.sessionId = SID_Q;
  window.__mock.sessions = [{ id: SID_Q, title: "Terminal join", channel: "web", active: true }];
  startRun({
    sid: SID_Q, runId: "probe-run-qA", persistedSeq: 1,
    messagesMid: [userTurn("PROBE_LIVE term question"), {
      role: "assistant", text: "R0 row", tool_calls: [], tool_results: [],
      content_blocks: [{ type: "text", text: "R0 row" }],
    }],
    messagesFull: [],
  });
  emit({ type: "delta", text: "R0 row" }); // seq 1, persisted (the row)
  emit({ type: "delta", text: " R1 tail" }); // seq 2, in the live tail
  const termJoin = renderSession(SID_Q); // in flight...
  await sleep(20);
  deliver({ type: "run_done", run_id: "probe-run-qA", seq: 3 }); // ...the run ends during it
  await termJoin;
  await sleep(20);
  finalizeAgentBubble();
  const sQ = ui.liveSeq && ui.liveSeq[SID_Q];
  const m5Pred = () => {
    const txt = log.textContent;
    return !!(sQ && sQ.runId === "probe-run-qA" && sQ.water > 0) &&
      countIn(txt, "R0") === 1 && countIn(txt, "R1") === 1;
  };
  check("terminal-join-attribution: a join whose named run ended DURING the fetch attributes the slot (runId + its water), never a provisional slot holding live.seq",
    m5Pred(),
    `slot=${JSON.stringify(sQ)} R0=${countIn(log.textContent, "R0")} R1=${countIn(log.textContent, "R1")}`);
  log.innerHTML = "";
  check("terminal-join-attribution control: the detector goes red on an emptied transcript",
    m5Pred() === false);
  ui.resync = null;
  window.__mock.liveRun = null;

  // missing-route (deploy-day version skew): an agentd that PREDATES /live
  // answers 404. fetchLive must read that as the {missing:true} SENTINEL —
  // deterministic, stop-probing — never as null (offline: keep retrying)
  // and never as active:false (which would trigger a store render: the
  // lossy path on a server this old, it has no live join either). The
  // watchdog branch that consumes the sentinel is pinned in
  // test_conventions.py; the 10s-timer loop itself is too long for this
  // probe's virtual budget.
  window.__mock.liveMissing = true;
  const missingRes = await fetchLive("probe-live-2dev", 3);
  window.__mock.liveMissing = false;
  check("missing-route: /live 404 reads as the {missing:true} sentinel (not offline, not active:false — the watchdog stops probing, never store-renders)",
    !!missingRes && missingRes.missing === true && missingRes.active === undefined,
    `res=${JSON.stringify(missingRes)}`);

  // =========================================================================
  // cold-open-live — a fresh boot MID-ANSWER.
  // Boot restores the stored chat while a TOOL-LOOP run is mid-call-2: the
  // ONE /messages fetch carries {messages, live} — the persisted step rows
  // plus the ring tail joined at the server watermark. The screen must show
  // the LIVE reply: paint the rows (pill once — replay-from-0 is impossible
  // by construction, the tail holds no persisted events), then replay
  // live.events unpaced. ANSW1/ANSW2 are on no other source. Then the run
  // keeps streaming while the tab backgrounds again and comes back: the
  // next resume must gap-fill from the watermark the client EARNED from
  // this fetch — live.seq (7), never live.since (4) — proving the two
  // sinces stay distinct end-to-end. ANSW3 (emitted while hidden) is only
  // reachable through that /live call.
  // =========================================================================
  const SID_C = "probe-live-cold";
  DELIVER_SID = SID_C;
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { register: () => new Promise(() => {}), addEventListener: () => {} },
  });
  resetTranscript();
  ui.sessionId = null; // the hash-less boot decides from lastChat
  localStorage.setItem("lastChat", SID_C);
  window.__mock.sessions = [{ id: SID_C, title: "Mid-loop chat", channel: "web", active: true }];
  startRun({
    sid: SID_C, runId: "probe-run-loop", persistedSeq: 4,
    messagesMid: loopMessagesMid("PROBE_LIVE cold question"),
    messagesFull: loopMessagesFull("PROBE_LIVE cold question", "ANSW1 ANSW2 ANSW3"),
  });
  emit({ type: "model_wait" });                                     // seq 1
  emit({ type: "delta", text: "PLAN1 step" });                      // seq 2  \ persisted
  emit({ type: "tool_call", tool: "probe_tool", args: { a: 1 } });   // seq 3  | persistedSeq 4
  emit({ type: "tool_result", tool: "probe_tool", is_error: false, digest: "ok" }); // seq 4 /
  emit({ type: "thinking_delta", text: "THK-L1 " });                 // seq 5 \
  emit({ type: "delta", text: "ANSW1 the live reply " });            // seq 6  } the in-flight
  emit({ type: "delta", text: "ANSW2 still being written" });        // seq 7 / answer
  await import("./app.js?coldopen=1"); // a cold boot: production boot reopens lastChat
  await sleep(450); // sessions (0ms) + /messages (150ms) + replay, with slack
  emit({ type: "delta", text: "ANSW3 post-resume tail" }); // seq 8: while hidden again
  const callsBeforeResume = window.__mock.liveCalls.length;
  suspend();
  await sleep(20);
  resume(); // Step 6: /live?since=7 → [seq 8] → ANSW3 painted
  await sleep(FETCH_SETTLE_MS);
  finalizeAgentBubble();
  const resumeCalls = window.__mock.liveCalls.slice(callsBeforeResume);
  const l4Pred = () => {
    const txt = log.textContent;
    return txt.includes("ANSW1") && txt.includes("ANSW2") && txt.includes("ANSW3") &&
      countIn(txt, "ANSW1") === 1 &&
      log.querySelectorAll(".pill").length === 1 &&
      log.querySelectorAll(".msg.user").length === 1 &&
      resumeCalls.some((c) => c.sid === SID_C && c.since === 7);
  };
  check("cold-open-live: a fresh boot mid-answer paints the live reply and the next resume gap-fills from live.seq (never live.since)",
    l4Pred(),
    `ANSW1=${log.textContent.includes("ANSW1")} ANSW2=${log.textContent.includes("ANSW2")} ` +
    `ANSW3=${log.textContent.includes("ANSW3")} pills=${log.querySelectorAll(".pill").length} ` +
    `resume /live calls=${JSON.stringify(resumeCalls.map((c) => c.since))}`);
  log.innerHTML = "";
  check("cold-open-live control: the live-reply detector goes red on an emptied transcript",
    l4Pred() === false);

  // --- leave the page exactly as scenario I (cold-start) expects -------------
  localStorage.removeItem("lastChat");
  window.__mock.liveRun = null;
  window.__mock.sessions = [];
  window.__mock.settled = true; // any later refetch sees a committed store
  ui.resync = null;
  ui.sessionId = null;
  delete document.visibilityState; // back to the engine's own answer (visible)
}
