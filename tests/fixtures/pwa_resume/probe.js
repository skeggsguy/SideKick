/* PWA-resume recovery probe (plan pwa-resume-reconcile).

   Drives the REAL transcript.js/events.js recovery under chrome-headless-shell
   --dump-dom (see run_probe.py): the page is a copy of the real index.html DOM,
   so transcript/follow/greeting/status/thinking evaluate exactly as in the app.

   THE SETTLED DESIGN (wizard, Step 3 checkpoint): the Step 2 flash-free
   run_done refetch is the SOLE heal; an EARLY re-sync is forbidden (mid-call
   the store lacks the in-flight assistant message, and the re-sync clears the
   recovery state that arms the heal — the head is lost permanently). The
   recovered mid-word tail on resume is an accepted temporary artefact.

   A (the blank flash — Step 2's yardstick): paint a mid-stream transcript
   through the real handlers, deliver the terminal run_done through
   window.__sk.handleRunEvent (the exact production recovery call), watch
   #chat-log through the mocked re-fetch: full answer back, no frame empty.
   B (resume + heal): the first recovered /api/events event's mid-answer tail
   IS spliced (the accepted artefact), zero /messages fetches happen before
   run_done (no-premature-resync — the mid-call store mock makes any early
   renderSession visibly wipe the head), then one atomic refetch heals it.
   F/G (thinking-block-vanish): a frozen `.think` strip (F still streaming,
   G already collapsed) must SURVIVE the heal — .think.done with the replay
   label, and exactly one replay cycle in `window.__sk._thinkLog` (the
   passive log the probe arms; a guarded no-op in production) with nothing
   after the buffer flush.
   H (the wizard's fix): the completed turn's events buffered during a
   RECOVERY renderSession's fetch window are DISCARDED, not flushed — a flush
   repaints the stale tail over the fresh replay (zombie .think from
   thinking_delta, tail bubble from delta). DUMP-H2 dumps (not asserts) what
   a flushed stale event does to the DOM via the normal-open (flushing) path.
   L (Step 5, probe_live.js): the four live-resume assertions — resume-mid-
   thinking, no-duplicate-words, partial-head-survives, cold-open-live —
   driven against the mock's live-run ring (mock_fetch.js `liveRun`, timing
   honest: the in-flight answer is ring-only). GREEN since Step 6 landed the
   client resync (webui/replay.js); README.md carries what each pins.
   Every detector is positive-controlled so no green can be vacuous; the
   LOG-* lines carry the full trace (stacks for endThinking/resetTranscript)
   for the wizard whether the scenarios go red or green.

   HEADLESS TRAP: rAF callbacks never fire under --virtual-time-budget
   (measured: 0 ticks in 400ms of virtual time; setTimeout fast-forwards), so
   every "what is on screen" assertion flushes with finalizeAgentBubble() —
   what a real browser does via rAF within ~0.6s. */
"use strict";

import { ui } from "./state.js";
import {
  addUserMsg, dispatchStream, finalizeAgentBubble, renderSession, resetTranscript,
} from "./transcript.js";
import { appendThinking, endThinking, startThinking } from "./thinking.js";
import "./events.js"; // side effect: window.__sk = { handleRunEvent }
import { runLiveResume } from "./probe_live.js"; // scenario L (Step 5 reds)
import { runCompaction } from "./probe_compaction.js"; // scenario M (Step 4 seam)
import { runColdStart } from "./probe_coldstart.js"; // scenario I (Step 2)

const SID = "probe-session-1";
const HEAD_TEXT = "PROBE_HEAD partial answer that was streaming when the socket died";
const HEALED = "HEALED_FULL_ANSWER";
const FROZEN_TEXT = "PROBE_TAIL frozen mid-answer text still on screen at suspension";
const TAIL_FRAGMENT = " PROBE_MIDFRAG tail of the dead-socket stream";
const FETCH_DELAY_MS = 150; // mock latency — must match mock_fetch.js delayMs
const SAMPLE_AT_MS = 40; // inside the fetch window: 0 < 40 < 150
const RESUME_WAIT_MS = 200; // > FETCH_DELAY_MS: an early re-sync's wipe has landed AND completed by the checks

const results = [];
let failed = 0;
// `detail` must be newline-free: raw textContent with a \n splits the results
// <pre> block, so a correct read parses as garbage — use counts/dataset/sliced text.
function check(name, pass, detail) {
  results.push(`${pass ? "PASS" : "FAIL"} ${name}${detail ? " | " + detail : ""}`);
  if (!pass) failed += 1;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const msgFetches = () =>
  window.__mock.calls.filter((p) => p.includes("/messages")).length;

// --- thinking-strip recovery helpers (scenarios F/G/H) -----------------------
const THINK_TEXT = "PROBE_THINKING segment"; // byte-identical to the store's
// persisted thinking text, so "preserved across the heal" is assertable
const F_PARTIAL = "PROBE_F partial answer mid-stream at suspension";
const thinkEl = () => document.querySelector("#chat-log .think");
const armLog = () => (window.__sk._thinkLog = []);
const stripLabel = (s) => (s ? s.querySelector(".think-label").textContent : "(gone)");
const stripPreview = (s) => (s ? s.querySelector(".think-preview").textContent : "");
const logLine = (e) => {
  // Engine-tolerant frame naming: V8 stacks are "at name (url)" lines; JSC /
  // iOS Safari (the on-device capture's engine) is "name@url:line:col" — the
  // old startsWith("at ") filter printed an empty " < " on WebKit.
  const frames = e[2]
    ? " < " + e[2].split("\n").map((s) => s.trim())
        .filter((s) => (s.startsWith("at ") && !s.includes("tlog")) ||
                       (/^[A-Za-z_$][\w$.]*@/.test(s) && !s.includes("tlog")))
        .slice(0, 3).join(" < ")
    : "";
  return `${e[0]} ${JSON.stringify(e[1])}${frames}`;
};
const dumpLog = (tag, entries) =>
  entries.forEach((e, i) => results.push(`LOG-${tag} ${i + 1}. ${logLine(e)}`));

async function main() {
  const log = document.querySelector("#chat-log");
  const errs = [];
  window.addEventListener("error", (e) => errs.push(`error: ${e.message}`));
  window.addEventListener("unhandledrejection", (e) => errs.push(`rejection: ${String(e.reason)}`));

  check("hook: window.__sk.handleRunEvent registered by events.js",
    typeof window.__sk?.handleRunEvent === "function");
  check("mock: fetch mock installed before any module fetch",
    typeof window.__mock?.messages === "object");

  // Positive control A — the harness can paint: a normal (pre-suspension)
  // paint leaves #chat-log non-empty through the REAL handler path.
  ui.sessionId = SID;
  addUserMsg("PROBE_HEAD question");
  window.__sk.handleRunEvent({ type: "delta", text: HEAD_TEXT, session_id: SID });
  finalizeAgentBubble(); // flush the paced reveal synchronously
  check("control: normal paint leaves #chat-log non-empty",
    log.children.length > 0 && log.innerHTML.includes("PROBE_HEAD"),
    `${log.children.length} children`);

  // --- scenario A: the forced recovery -------------------------------------
  // The observer fires at microtask checkpoints (points where the browser MAY
  // paint): an empty seen here is a paintable blank frame, not an intra-task
  // detail.
  let emptyFrames = 0;
  const mo = new MutationObserver(() => {
    if (log.children.length === 0) emptyFrames += 1;
  });
  mo.observe(log, { childList: true });
  ui.resync = { id: SID, seen: false }; // the stream died; the run survived
  const beforeChars = log.innerHTML.length;
  window.__mock.settled = true; // the detached run FINISHED: the store committed
  window.__sk.handleRunEvent({ type: "run_done", session_id: SID });
  await sleep(SAMPLE_AT_MS); // inside the mocked re-fetch window
  const sampleEmpty = log.children.length === 0;
  check("blank-flash: #chat-log never empty between recovery and repaint",
    emptyFrames === 0 && !sampleEmpty,
    `t+${SAMPLE_AT_MS}ms children=${log.children.length}; ` +
    `observer empty-checkpoints=${emptyFrames}; pre-recovery html ${beforeChars} chars`);

  check("recovery: run_done cleared the resync state (real events.js branch ran)",
    ui.resync === null, `resync=${JSON.stringify(ui.resync)}`);

  await sleep(400); // fetch resolves at FETCH_DELAY_MS; the replay paints after
  check("recovery: refetched transcript painted", log.innerHTML.includes(HEALED));
  check("recovery: exactly one user bubble (no double paint)",
    log.querySelectorAll(".msg.user").length === 1,
    `${log.querySelectorAll(".msg.user").length} found`);
  check("recovery: replayed thinking + tool segments present",
    log.querySelectorAll(".think").length === 1 && log.querySelectorAll(".pill").length === 1,
    `think=${log.querySelectorAll(".think").length} pill=${log.querySelectorAll(".pill").length}`);

  // Positive control B — the instrument detects empties, or "never empty"
  // would be vacuous. Observer must still be connected.
  const framesBefore = emptyFrames;
  log.innerHTML = "";
  await sleep(0);
  check("instrument: observer detects an emptied #chat-log", emptyFrames > framesBefore);
  mo.disconnect();

  // --- scenario B: resume + heal (the settled contract) --------------------
  // Client back MID-TURN: the first rerouted /api/events event carries a tail
  // starting mid-answer. The honest contract paints it (accepted artefact) and
  // heals ONLY at run_done — the mid-call store lacks the in-flight answer.
  ui.sessionId = SID;
  addUserMsg("PROBE_TAIL question");
  window.__sk.handleRunEvent({ type: "delta", text: FROZEN_TEXT, session_id: SID });
  finalizeAgentBubble(); // rAF is dead headless — flush to model a painted DOM
  check("control: the frozen mid-answer transcript is painted before the kill",
    log.textContent.includes(FROZEN_TEXT),
    `${log.querySelectorAll(".msg.agent").length} agent bubble(s)`);

  let midWordFrames = 0;
  let healEmptyFrames = 0;
  const mo2 = new MutationObserver(() => {
    if (log.textContent.includes(TAIL_FRAGMENT)) midWordFrames += 1;
    if (log.children.length === 0) healEmptyFrames += 1;
  });
  mo2.observe(log, { childList: true, subtree: true, characterData: true });
  const fetchesBeforeResume = msgFetches();
  window.__mock.settled = false; // a NEW answer is mid-call: the store lacks it
  ui.resync = { id: SID, seen: false };
  window.__sk.handleRunEvent({ type: "delta", text: TAIL_FRAGMENT, session_id: SID });
  finalizeAgentBubble(); // reveal the spliced tail, as a real browser would
  await sleep(RESUME_WAIT_MS); // outlast the mocked fetch window entirely
  check("mid-word: the recovered partial tail IS painted on resume (accepted artefact)",
    log.textContent.includes(TAIL_FRAGMENT),
    `spliced-checkpoints=${midWordFrames}`);
  // THE no-premature-resync GUARD: the mid-call store (settled=false) hides
  // the in-flight answer, so ANY renderSession before run_done would register
  // in the fetch counter AND, the fetch window now closed, have visibly
  // replaced the frozen head with the answer-less store. Cannot sneak by.
  check("no-premature-resync: zero /messages fetches while catching up (before run_done)",
    msgFetches() === fetchesBeforeResume,
    `messages fetches ${fetchesBeforeResume} -> ${msgFetches()}`);
  check("no-premature-resync: the frozen head survives the catching-up window",
    log.textContent.includes(FROZEN_TEXT),
    "an early re-sync would have replaced it with the answer-less store");

  // run_done: the model call returned — the store commits, the SOLE heal runs.
  window.__mock.settled = true;
  const fetchesBeforeDone = msgFetches();
  window.__sk.handleRunEvent({ type: "run_done", session_id: SID });
  await sleep(400); // the heal fetch resolves at FETCH_DELAY_MS; the replay paints after
  const fragGone = !log.textContent.includes(TAIL_FRAGMENT);
  check("heal: run_done triggered exactly one /messages fetch",
    msgFetches() === fetchesBeforeDone + 1,
    `messages fetches ${fetchesBeforeDone} -> ${msgFetches()}`);
  check("mid-word healed: complete answer replaced the fragment, atomically",
    fragGone && log.textContent.includes(HEALED) &&
      log.querySelectorAll(".msg.user").length === 1 && healEmptyFrames === 0,
    `fragment gone=${fragGone}; HEALED=${log.textContent.includes(HEALED)}; ` +
    `empty-checkpoints during the heal=${healEmptyFrames}`);

  // Positive control C — B's instruments can fail, or B's guard greens would
  // prove nothing (detector sees a fragment; counter sees a request at all).
  const splicedBefore = midWordFrames;
  log.appendChild(document.createTextNode(TAIL_FRAGMENT));
  await sleep(0);
  check("instrument: mid-word detector sees a spliced fragment",
    midWordFrames > splicedBefore, `${midWordFrames} checkpoints`);
  const counterBefore = msgFetches();
  await fetch(`/api/sessions/${SID}/messages`); // mocked; body unread — counting only
  check("instrument: fetch counter sees a /messages request",
    msgFetches() > counterBefore, `${counterBefore} -> ${msgFetches()}`);
  mo2.disconnect();

  // --- scenario F: still-streaming thinking strip → catching-up tool_call → run_done
  // Mid-stream frozen DOM at suspension: strip STILL STREAMING (thinkBlock
  // set); the first recovered tool_call closes it, run_done fires the sole
  // heal. _thinkLog names every call that could remove the strip.
  resetTranscript();
  addUserMsg("PROBE_HEAD question");
  dispatchStream({ type: "delta", text: F_PARTIAL });
  finalizeAgentBubble(); // the partial answer is on screen at suspension
  startThinking(); // the strip streamed when the socket died
  appendThinking(THINK_TEXT);
  const fPainted = thinkEl();
  if (fPainted) fPainted._t0 -= 5000; // it had been streaming ~5s pre-suspension
  check("F control: frozen DOM carries a streaming .think + the partial answer",
    !!fPainted && fPainted.classList.contains("streaming") &&
      log.textContent.includes(F_PARTIAL),
    fPainted ? fPainted.className : "(no .think painted)");
  ui.resync = { id: SID, seen: false };
  armLog(); // the log window covers the recovery path only
  window.__sk.handleRunEvent({ type: "tool_call", tool: "probe_tool", args: { a: 1 }, session_id: SID });
  check("F catching-up: the streaming strip closed to .think.done (real startToolPill→endThinking)",
    !!thinkEl() && thinkEl().classList.contains("done") && !thinkEl().classList.contains("streaming"),
    thinkEl() ? thinkEl().className : "(strip gone)");
  check("F catching-up: 'Thought for Ns' label present (d)",
    /Thought for \d+s/.test(stripLabel(thinkEl())), stripLabel(thinkEl()));
  check("F catching-up: thinking text preserved on the closed strip",
    stripPreview(thinkEl()).includes(THINK_TEXT), stripPreview(thinkEl()).slice(0, 40));
  window.__mock.settled = true; // the run finished: the store committed
  window.__sk.handleRunEvent({ type: "run_done", session_id: SID });
  await sleep(400); // the heal fetch resolves at FETCH_DELAY_MS; the replay paints after
  const fThinkOk = () =>
    log.querySelectorAll(".think").length === 1 && !!thinkEl() && thinkEl().classList.contains("done");
  const fStrip = thinkEl();
  check("F recovery: .think.done survives the atomic swap (a)", fThinkOk(),
    `count=${log.querySelectorAll(".think").length} class=${fStrip ? fStrip.className : "(gone)"}`);
  // The healed strip is the REPLAYED one: endThinking({replay:true}) labels it
  // "Thoughts" — requiring the element AND that label keeps this non-vacuous:
  // stripLabel(null) is "(gone)", so a bare length>0 check would pass vanished.
  check("F recovery: healed strip carries the replay label",
    !!fStrip && stripLabel(fStrip) === "Thoughts", stripLabel(fStrip));
  check("F recovery: thinking text preserved across the heal",
    stripPreview(fStrip).includes(THINK_TEXT), stripPreview(fStrip).slice(0, 40));
  const fLog = window.__sk._thinkLog;
  dumpLog("F", fLog);
  const fStarts = fLog.filter((e) => e[0] === "startThinking");
  const fReplayEnd = fLog.filter((e) => e[0] === "endThinking" && e[1].replay);
  check("F log(b): exactly one replay startThinking→endThinking(replay) pair",
    fStarts.length === 1 && fReplayEnd.length === 1 &&
      fLog.indexOf(fStarts[0]) < fLog.indexOf(fReplayEnd[0]),
    `startThinking=${fStarts.length} replayEnd=${fReplayEnd.length}`);
  const fCloses = fLog.filter((e) => e[0] === "endThinking" && e[1].hadBlock);
  const fFlushAt = fLog.findIndex((e) => e[0] === "renderSession:flush");
  const fAfterFlush = fFlushAt >= 0
    ? fLog.slice(fFlushAt).filter((e) => e[0] === "endThinking" && e[1].hadBlock).length : -1;
  check("F log(c): segment closes are exactly the catching-up tool_call + the replay (stacks name the callers); none after the buffer flush",
    fCloses.length === 2 &&
      fCloses.filter((e) => !e[1].replay && /startToolPill/.test(e[2] || "")).length === 1 &&
      fCloses.filter((e) => e[1].replay && /renderSession/.test(e[2] || "")).length === 1 &&
      fAfterFlush === 0,
    `closes=${fCloses.length} afterFlush=${fAfterFlush}`);
  check("F log: resetTranscript fired from the renderSession swap (the expected removal)",
    fLog.some((e) => e[0] === "resetTranscript" && /renderSession/.test(e[2] || "")));
  // Positive control — the strip-present detector is live: remove the element
  // and F(a) must go red.
  if (fStrip) fStrip.remove();
  check("F control: (a) goes red when the .think element is removed", fThinkOk() === false);

  // --- scenario G: already-finished run, collapsed strip → run_done direct
  // The strip collapsed to "Thought for Ns" before suspension and the run
  // finished while away: the atomic swap rebuilds it from the committed store,
  // in the store's order, via exactly one replay cycle.
  resetTranscript();
  addUserMsg("PROBE_HEAD question");
  startThinking();
  appendThinking(THINK_TEXT);
  const gCollapsed = thinkEl();
  if (gCollapsed) gCollapsed._t0 -= 5000; // ~5s of streamed thinking
  endThinking(); // collapsed pre-suspension: "Thought for 5s", thinkBlock dropped
  check("G control: frozen DOM carries the collapsed .think.done strip",
    !!thinkEl() && thinkEl().classList.contains("done") &&
      /Thought for \d+s/.test(stripLabel(thinkEl())), stripLabel(thinkEl()));
  dispatchStream({ type: "delta", text: "PROBE_G answer finished while backgrounded" });
  finalizeAgentBubble();
  ui.resync = { id: SID, seen: false };
  armLog(); // fresh window: G's log excludes F's entries
  window.__mock.settled = true; // committed before we came back
  window.__sk.handleRunEvent({ type: "run_done", session_id: SID });
  await sleep(400);
  const gThinkOk = () =>
    log.querySelectorAll(".think").length === 1 && !!thinkEl() && thinkEl().classList.contains("done");
  const gStrip = thinkEl();
  check("G recovery: the strip survives the atomic repaint", gThinkOk(),
    `count=${log.querySelectorAll(".think").length} class=${gStrip ? gStrip.className : "(gone)"}`);
  const kids = [...log.children];
  const iThink = kids.findIndex((c) => c.classList.contains("think"));
  const iPill = kids.findIndex((c) => c.classList.contains("pill-row"));
  const iAgent = kids.findIndex((c) => c.classList.contains("msg") && c.classList.contains("agent"));
  check("G recovery: rebuilt order matches the store (.think < pill-row < answer bubble)",
    iThink !== -1 && iThink < iPill && iPill < iAgent,
    `think=${iThink} pill=${iPill} agent=${iAgent}`);
  check("G recovery: healed strip carries the replay label",
    !!gStrip && stripLabel(gStrip) === "Thoughts", stripLabel(gStrip));
  check("G recovery: thinking text preserved across the heal",
    stripPreview(gStrip).includes(THINK_TEXT), stripPreview(gStrip).slice(0, 40));
  const gLog = window.__sk._thinkLog;
  dumpLog("G", gLog);
  const gStarts = gLog.filter((e) => e[0] === "startThinking");
  const gCloses = gLog.filter((e) => e[0] === "endThinking" && e[1].hadBlock);
  const gFlushAt = gLog.findIndex((e) => e[0] === "renderSession:flush");
  const gAfterFlush = gFlushAt >= 0
    ? gLog.slice(gFlushAt).filter((e) => e[0] === "startThinking" || e[0] === "endThinking").length : -1;
  check("G log: exactly one replay cycle, no spurious extra start/end, none after the buffer flush",
    gStarts.length === 1 && gCloses.length === 1 && gCloses[0][1].replay === true &&
      /renderSession/.test(gCloses[0][2] || "") && gAfterFlush === 0,
    `starts=${gStarts.length} closes=${gCloses.length} afterFlush=${gAfterFlush}`);
  check("G log: run_done went straight to the refetch (no catching-up paint into the frozen DOM)",
    !gLog.some((e) => e[0] === "catchingUp") && gLog.some((e) => e[0] === "run_done:refetch"));
  // Positive control — the detector is live for G's DOM too.
  if (gStrip) gStrip.remove();
  check("G control: the strip-present check goes red when the element is removed", gThinkOk() === false);

  // --- scenario H: buffered stale events are DISCARDED during a recovery ----
  // The wizard-pinned root cause: the completed turn's rerouted events buffer
  // during a RECOVERY renderSession's fetch window; flushing them after the
  // replay corrupts the fresh DOM (zombie .think, tail bubble). The fix (Step
  // 7: an argument, not an ambient flag): the recovery heal calls
  // renderSession(id, true) → the finally clears the buffer WITHOUT dispatching.
  resetTranscript();
  addUserMsg("PROBE_HEAD question");
  dispatchStream({ type: "delta", text: F_PARTIAL });
  finalizeAgentBubble();
  startThinking(); // frozen streaming state, as in F
  appendThinking(THINK_TEXT);
  ui.resync = { id: SID, seen: false };
  armLog();
  window.__mock.settled = true;
  window.__sk.handleRunEvent({ type: "run_done", session_id: SID });
  await sleep(SAMPLE_AT_MS); // inside the fetch window: _buffer holds these
  dispatchStream({ type: "thinking_done" }); // the completed turn's stale tail
  dispatchStream({ type: "thinking_delta", text: "PROBE_STALE zombie thinking" });
  dispatchStream({ type: "delta", text: "PROBE_STALE tail bubble" });
  await sleep(400); // the replay paints; the finally DISCARDS the buffer
  const hThinkOk = () =>
    log.querySelectorAll(".think").length === 1 && !!thinkEl() && thinkEl().classList.contains("done");
  const hStrip = thinkEl();
  check("H recovery: exactly one .think.done survives — discarded events spawned no zombie",
    hThinkOk(), `count=${log.querySelectorAll(".think").length} class=${hStrip ? hStrip.className : "(gone)"}`);
  check("H recovery: healed strip carries the replay label, no stale event reached the DOM",
    !!hStrip && stripLabel(hStrip) === "Thoughts" && !log.textContent.includes("PROBE_STALE"),
    `${stripLabel(hStrip)}; staleText=${log.textContent.includes("PROBE_STALE")}`);
  const hLog = window.__sk._thinkLog;
  dumpLog("H", hLog);
  const hFlush = hLog.find((e) => e[0] === "renderSession:flush");
  check("H log: the recovery flush DISCARDED the buffer (discarded:true, queued>=1)",
    !!hFlush && hFlush[1].discarded === true && hFlush[1].queued >= 1,
    hFlush ? JSON.stringify(hFlush[1]) : "(no flush entry)");
  const hAt = hLog.indexOf(hFlush);
  const hAfter = hAt >= 0
    ? hLog.slice(hAt + 1).filter((e) => e[0] === "endThinking" && e[1].hadBlock).length : -1;
  check("H log: no endThinking{hadBlock:true} after the flush entry (nothing closed a strip post-replay)",
    hAfter === 0, `afterFlush=${hAfter}`);
  if (hStrip) hStrip.remove();
  check("H control: the strip-present check goes red when the element is removed", hThinkOk() === false);

  // --- H2: GUIDE-AMENDMENT-2 EVIDENCE (dump lines, not assertions) ----------
  // What a FLUSHED stale event does to the DOM: same DOM and window injections
  // through a NON-armed renderSession (= the pre-fix recovery render).
  resetTranscript();
  addUserMsg("PROBE_HEAD question");
  startThinking();
  appendThinking(THINK_TEXT);
  armLog();
  renderSession(SID); // no recovering argument → the finally FLUSHES (as before the fix)
  await sleep(SAMPLE_AT_MS);
  dispatchStream({ type: "thinking_done" });
  dispatchStream({ type: "thinking_delta", text: "PROBE_STALE zombie thinking" });
  dispatchStream({ type: "delta", text: "PROBE_STALE tail bubble" });
  await sleep(400); // the render + flush complete (fetch resolves at 150ms)
  finalizeAgentBubble(); // paint the flushed tail bubble (rAF is dead headless)
  const h2Think = [...log.querySelectorAll(".think")];
  results.push("DUMP-H2 flushed-stale DOM: " +
    `think=${h2Think.length} streaming=${log.querySelectorAll(".think.streaming").length}` +
    ` agent=${log.querySelectorAll(".msg.agent").length} pill=${log.querySelectorAll(".pill").length}` +
    ` staleTail=${log.textContent.includes("PROBE_STALE tail bubble")}` +
    ` replayLabel=${stripLabel(h2Think[0])} zombie=${stripLabel(h2Think[1])}` +
    ` classes=[${h2Think.map((t) => t.className).join(",")}]`);
  dumpLog("H2", window.__sk._thinkLog);

  // --- scenario M: the compaction live-UI seam (probe_compaction.js) --------
  // Before L: it needs no app.js boot and no mock.liveRun residue; it
  // restores ui/mock state itself for the scenarios that follow.
  await runCompaction({ check, sleep });

  // --- scenario L: the Step-5 live-resume assertions (green since Step 6) ----
  // (probe_live.js — resume-mid-thinking, no-duplicate-words, partial-head-
  // survives, cold-open-live). RED until Steps 6-7 land the client resync;
  // its cold-open boot runs before scenario I's, each boot's fetches settled
  // before the next (the multi-boot recipe).
  await runLiveResume({ check, sleep });

  // Scenario I (cold-start restore) — LAST: it re-boots app.js several times
  // and each boot rebuilds the shared modules' view of the world, so nothing
  // may run after it. (scenario L's cold-open is the only other app.js
  // evaluation, and it runs here first with its fetches fully settled.)
  await runColdStart({ check, msgFetches, sleep });
  check("no page errors during the probe", errs.length === 0, errs.join("; "));
}

await main().catch((e) => {
  results.push(`FAIL probe-error: ${e && e.stack ? String(e.stack).split("\n")[0] : String(e)}`);
  failed += 1;
}).finally(() => {
  const pre = document.createElement("pre");
  pre.id = "probe-results";
  pre.textContent =
    "PROBE-RESULTS-BEGIN\n" + results.join("\n") +
    `\nPROBE-RESULT: ${failed === 0 ? "PASS" : "FAIL"} (${failed} failing assertions)` +
    "\nPROBE-RESULTS-END\n";
  document.body.appendChild(pre);
});
