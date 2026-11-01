/* Live background-run stream (/api/events): the run toast, run_start/run_done
   bookkeeping, the connection dot, and the visibility rules that decide when
   the socket is held at all.

   BK-019 (design scratch/plans/bk-019-design.md §5.2): the recovery VERDICT
   TREE lives in runstate.js (T27 resyncVerdict — /live's `active` is the only
   liveness predicate, asked exactly once per verdict). This module ROUTES
   run events through the named transitions, supplies runstate its deps
   (replay.js's arithmetic + the /live fetch) and its fx (the renderer map
   below), applies the returned effects, and keeps what is NOT run-lifecycle:
   the toast, the working indicator, EventSource plumbing, the detach-watchdog
   TIMER (tries + the /detach retry ride the timer owner, §10.4), and the tlog
   names the probes assert verbatim (D8).

   The stream is held ONLY while the tab is visible: the daemon reads
   broadcaster.subscriber_count > 0 as "someone is looking at the app" and
   suppresses the approval push (the modal covers it). A backgrounded tab
   keeping the socket open would silently eat pushes. */
"use strict";

import { $, el, api } from "./dom.js";
import { ui } from "./state.js";
import {
  addErrorMsg, addSystemMsg, addUserMsg, closeSegments, dispatchStream,
  finalizeAgentBubble, isReplaying, paintStream, renderSession, resetTranscript, wireScreen,
} from "./transcript.js";
import { hideGreeting } from "./greeting.js";
import {
  alreadyPainted, fetchLive, isProvisionalSlot, joinReason, markPainted,
  paintGapFill, raiseWatermark, storedWatermark,
} from "./replay.js";
import {
  applyEffects, bgRunStart, catchingUp, observeJoin, observeLeave, pendingFor, paintRerouted,
  reopenResume, runDoneOther, runDoneWatched, runError, runStopped, titleSeen, turnComplete,
} from "./runstate.js";
// The verdict half (T17/T20/T21 → T27) — split from runstate.js (BK-034).
import { rowJoin, runDoneHeal, visibleResume, watchdogTick } from "./resync.js";
import { watchObserved } from "./watch.js"; // BK-034: the observe watch's timer
import {
  applyEvent as statusEvent, phase as statusPhase, setLane as statusLane,
  start as statusStart, stop as statusStop, warn as statusWarn, join as statusJoin,
} from "./status.js";
import { pollApprovalsSoon } from "./approvals.js";
import { loadSessions, rememberLastChat, showNewChatOption, updateRenameBtn } from "./sessions.js";
import { setSendMode } from "./sendmode.js";
import { chatWorkPending, stopChatWork, workOff, workOn } from "./working.js";

function chatTabActive() {
  const b = $('[data-tab="chat"]');
  return b && b.classList.contains("active");
}

// Passive recovery diagnostic (thinking-block-vanish): armed by the probe or a
// Safari console via `window.__sk._thinkLog = []`. Zero production overhead —
// the array does not exist, so nothing is built or pushed. Same local helper
// as thinking.js / transcript.js; entries are [name, state, stack|null, ms].
const tlog = (name, state, withStack) => {
  const L = window.__sk && window.__sk._thinkLog;
  if (!L) return;
  // BK-023: entry = [name, state, stack|null, event-time ms]. Stamped AT PUSH —
  // the ?recoverdebug pump drains every 2 s, so drain-time stamps cannot tell a
  // paced stream from a catch-up burst. Stack stays at index 2 (probes read
  // positionally); the null pad keeps the stamp always e[3].
  L.push([name, state, withStack ? new Error().stack : null, Date.now()]);
};

function openLiveSession(id) {
  ui.sessionId = id;
  applyEffects(observeLeave(id), RUN_FX); // BK-034 T30: no renderSession on this path
  rememberLastChat(id); // what is on screen IS the last chat — toast taps and
  // auto-opened runs included, or a relaunch reopens the PREVIOUS chat.
  $('[data-tab="chat"]').click();
  resetTranscript();
  hideGreeting();
  loadSessions().then(() => { $("#session-select").value = id; updateRenameBtn(); });
}

// A background run started while the user is in a conversation: never steal the
// screen — offer a tappable toast instead. Single element, latest run wins.
let runToastTimer = null;
function dismissRunToast() {
  clearTimeout(runToastTimer);
  const t = document.getElementById("run-toast");
  if (t) t.remove();
}
function showRunToast(ev) {
  dismissRunToast();
  const toast = el("div", "run-toast", `▶ ${ev.title || "Background run"} — tap to view`);
  toast.id = "run-toast";
  toast.setAttribute("role", "button");
  toast.tabIndex = 0;
  const open = () => { dismissRunToast(); openLiveSession(ev.session_id); };
  toast.onclick = open;
  toast.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  document.body.appendChild(toast);
  runToastTimer = setTimeout(dismissRunToast, 8000);
}

// The replay arithmetic + the /live question, INJECTED into runstate's
// transitions and verdict driver (design §2.6: runstate imports state.js ONLY
// — this is what keeps the DOM-free unit tier able to drive the same tree).
// One bag, two shapes: the six verdict keys T27 names, plus the dedupe pair
// the paint transitions (T05/T06) read off their `deps`.
const RUN_DEPS = { fetchLive, joinReason, storedWatermark, isProvisionalSlot,
  raiseWatermark, isReplaying, alreadyPainted, markPainted };

// Bookkeeping types routed by NAME (never through the paint tail): they ride
// the dedupe gate, and the sink that sees one must MARK it — replay.js's
// painters skip these types UNMARKED precisely because the sink claims them
// ("belongs to whichever sink sees them", replay.js RENDERED note).
const BOOKKEEPING = { title: 1, stopped: 1, turn_complete: 1, error: 1 };

// BK-019 fx dictionary (design §5.1) bound for this transport. The map is
// TOTAL: every token runstate's table can emit is bound here, including the
// chat-SSE-only ones (paintUser/statusStart/statusLane/netStop/armWatchdog/…)
// that no /api/events transition emits today — applyEffects THROWS on an
// unbound token, so dispatch totality is the contract, and
// tests/test_conventions.py checks these keys against runstate.js's source
// (never the paths a test happens to drive).
const RUN_FX = {
  statusStart, statusPhase, statusWarn, statusStop, statusEvent, statusLane, statusJoin,
  composer: setSendMode,
  paint: dispatchStream,
  paintUser: addUserMsg,
  closeSegments,
  finalizeBubble: finalizeAgentBubble,
  errorMsg: addErrorMsg,
  systemMsg: addSystemMsg,
  // The persisted refetch: POST-run heal only (verdictHeal). Carries the
  // stale-buffer discard (recovering) AND BK-017 read preservation — both
  // stated at the call, never ambient.
  rerenderHeal: (id) => renderSession(id, true, true),
  // The ONE sanctioned messages+live join (verdictColdJoin): its discard is
  // the batch's OWN terminal verdict (a terminal batch's rerouted tail races
  // the fetch); preserveRead keeps a scrolled-up reader put.
  rerenderCold: (id, terminal) => renderSession(id, terminal, true),
  gapFill: (id, events) => paintGapFill(id, events, paintStream),
  pickerReload: () => loadSessions(),
  pickerRelabel: (sid, title) => {
    // Titles are model output → textContent (XSS rule), never innerHTML.
    const o = $(`#session-select option[value="${sid}"]`);
    if (o) o.textContent = title;
  },
  pickerOpenNew: showNewChatOption,
  netStop: (id) => api(`/api/chat/${id}/stop`, { method: "POST" }).catch(() => {}),
  netDetach: (id) => api(`/api/chat/${id}/detach`, { method: "POST" }).catch(() => {}),
  armWatchdog: () => armDetachWatchdog(),
  observeWatch: () => armObserveWatch(),
  pollApprovalsSoon,
};

// BK-034: renderSession's screen-swap hook (T35 + T30 at entry; after the live
// join, BK-041's T34 row + T31) — on this fx map; transcript.js imports no router.
wireScreen({
  leave: (id) => applyEffects([...reopenResume(id) || [], ...observeLeave(id) || []], RUN_FX),
  join: (id, live, bg) => applyEffects([...rowJoin(id, live, bg) || [], ...observeJoin(id, live) || []], RUN_FX),
});
// The observe watch (watch.js owns the timer; this transport owns the bags).
export function armObserveWatch() { watchObserved(RUN_DEPS, RUN_FX); }

function handleRunEvent(ev) {
  if (ev.type === "run_start") {
    // T24 stays here (plan: run toast / working indicator are non-chat
    // duties): workOn + the auto-open rule + picker freshness. T28 marks the
    // run in flight (BK-027: a run_done missed while hidden heals at resume).
    applyEffects(bgRunStart(ev), RUN_FX);
    workOn();
    loadSessions();
    if (ev.session_id) {
      // Auto-open only when the user isn't engaged: they explicitly hit "Fire
      // now" (expecting), or they're idle on the empty new-chat screen.
      // chatWorkPending covers the window between chat submit and the stream's
      // `session` event assigning the session id.
      if (ui.expecting || (chatTabActive() && !ui.sessionId && !chatWorkPending())) {
        ui.expecting = false;
        openLiveSession(ev.session_id);
      } else {
        showRunToast(ev);
      }
    }
    return;
  }
  if (ev.type === "run_done") {
    workOff();
    if (pendingFor(ev.session_id)) {
      // T17: the event ITSELF is the terminal verdict — resyncVerdict runs
      // knownTerminal (run_done can NEVER be served by /live, so no re-ask)
      // and takes the persisted heal: it closes the head-gap on the recovered
      // bubble and any events dropped while hidden, then reloads the picker
      // (the run's auto-name may have landed). Race-free: nothing streams
      // after run_done (the post-run_done `title` relabel is picker-only and
      // already stamped terminal).
      if (ev.session_id === ui.sessionId)
        tlog("run_done:refetch", { session: ev.session_id });
      runDoneHeal(ev, RUN_DEPS).then((effects) => applyEffects(effects, RUN_FX));
      return;
    }
    // T18 (the run ON SCREEN: close + finalize + reload) / T19 (any other
    // run: reload only) — the guards are complementary, exactly one fires,
    // and both stamp the run terminal (the identity rule's observed source:
    // run_done names the run that ended, so its late events — a stopped
    // detached run's `title` — can never reset a newer slot).
    applyEffects(runDoneWatched(ev) || runDoneOther(ev), RUN_FX);
    return;
  }
  if (ev.session_id !== ui.sessionId) return;
  // Step-6 dedupe (replay.js): skip what this DOM already painted — kills the
  // word-doubling class whatever its cause (duplicate subscription, replay
  // overlap). seq-LESS background events pass through: "no seq" ≠ "old".
  if (alreadyPainted(ui.sessionId, ev)) return;
  // Detached chat runs reroute their events here (tagged session_id) — so the
  // status row, the picker relabel and the Stop outcome must all be handled
  // for this stream too. The FIRST rerouted event reframes the row as
  // "Catching up…" (T16's ONE flip; it also retires the detach watchdog's
  // premise) — and it runs AFTER the dedupe gate exactly as today: a
  // duplicate of an already-painted event must not consume the flip. The tlog
  // name stays the router's call (D8 — probe G asserts it verbatim).
  const catching = catchingUp(ev);
  if (catching) {
    tlog("catchingUp", { type: ev.type, session: ev.session_id });
    applyEffects(catching, RUN_FX);
  }
  if (BOOKKEEPING[ev.type]) markPainted(ui.sessionId, ev);
  if (ev.type === "title") { applyEffects(titleSeen(ev, ev.session_id), RUN_FX); return; }
  if (ev.type === "stopped") { applyEffects(runStopped(ev), RUN_FX); return; }
  if (ev.type === "turn_complete") {
    stopChatWork(); // the working indicator: a non-chat duty, stays in the router
    applyEffects(turnComplete(ev), RUN_FX);
    return;
  }
  if (ev.type === "error") { applyEffects(runError(ev), RUN_FX); return; }
  // T06 tail (statusEvent/paint, approvals poll on tool_call): the transition
  // claims the event for the dedupe (markPainted) as it paints.
  applyEffects(paintRerouted(ev, RUN_DEPS), RUN_FX);
}

// `chatWorkPending` (working.js) covers the window between chat submit and
// the stream's `session` event assigning the session id — run_start during
// that gap must not auto-open, the chat itself is about to own the screen.

// Test-only hook: the headless-Playwright recipe injects synthetic run events
// via page.evaluate(window.__sk.handleRunEvent, …) — module scope hides the
// function otherwise. Not part of the app's API.
// Setting window.__sk._thinkLog = [] arms the passive recovery diagnostic the
// thinking/transcript/events modules log state transitions into (see the
// local tlog helpers) — for the headless probe, a Safari console, or the
// console-less on-device capture below (?recoverdebug=1 → armRecoverDebug).
window.__sk = { handleRunEvent };

/* On-device capture of the recovery diagnostic (thinking-block-vanish): a
   passive _thinkLog is useless without a Safari console, so `?recoverdebug=1`
   in the URL arms it AND surfaces it. app.js boot calls this and pumps the log
   into the <pre>; the visibilitychange handler re-ensures it (a resume-reload
   re-runs boot, but the recovery window must not depend on boot-vs-event
   order). INERT without the param — one regex test, nothing else. The launch
   URL is the ONLY possible source: Home-Screen apps are storage-isolated from
   Safari (webkit 181849), so no tab-written flag could ever arm one. The <pre>
   lives inside #chat-log so it scrolls with the transcript, and since
   resetTranscript() wipes innerHTML, a wiped capture is re-created here —
   the pump in app.js re-queries through this function every tick.
   Returns the live <pre> when armed, else null. */
export function armRecoverDebug() {
  if (!/[?&]recoverdebug=1(&|$)/.test(location.search)) return null;
  if (!window.__sk._thinkLog) window.__sk._thinkLog = [];
  let pre = $("#recover-debug");
  if (!pre) {
    pre = el("pre");
    pre.id = "recover-debug";
    pre.setAttribute("aria-hidden", "true"); // #chat-log is aria-live — never narrate the trace
  }
  pre.hidden = false;
  // A default <pre> is white-space:pre, and a stack line here is a long URL —
  // on a phone that widens #chat-log into a sideways scroller (the card swing,
  // test_card_titles_cannot_widen_a_cards_pane) and the trace is unreadable. Wrap, cap the height, and scroll it
  // internally (also what makes app.js's scrollTop follow the trace).
  pre.style.whiteSpace = "pre-wrap";
  pre.style.wordBreak = "break-word";
  pre.style.maxHeight = "40vh";
  pre.style.overflowY = "auto";
  if (!pre.isConnected) $("#chat-log").appendChild(pre); // re-attach after a wipe
  return pre;
}

// Zombie-stream watchdog. The /detach POST that arms recovery can itself be
// lost in the same network failure that killed the chat stream — and if the
// server never noticed the disconnect, the run finishes as a zombie: no
// reroute, no run_done, nothing arrives. This timer owns only that retry (and
// the version-skew stop); the VERDICT every tick is runstate's watchdogTick
// (T21) — the SAME entry point (T27 resyncVerdict) the resume and the
// run_done heal run through, so no second decision tree can drift (§10.4:
// tries and the retry POST stay with the timer, never inside a transition).
// Self-clears once events flow again (resync.seen) or the heal completes.
let detachTimer = null;
export function armDetachWatchdog() {
  clearInterval(detachTimer);
  let tries = 0;
  detachTimer = setInterval(async () => {
    const sid = ui.resync?.id;
    try {
      const res = await watchdogTick(RUN_DEPS); // null: premise retired (seen)
      if (!res) {
        clearInterval(detachTimer);
        detachTimer = null;
        return;
      }
      applyEffects(res.effects, RUN_FX);
      const verdict = res.verdict;
      if (verdict === "terminal") {
        clearInterval(detachTimer);
        detachTimer = null; // the verdict ran the heal; nothing left to chase
        return;
      }
      if (tries < 6 && (verdict === "live" || verdict === "missing")) {
        // Keep retrying the POST that arms the reroute — the ring is the
        // resync's source even when the reroute never armed (this whole
        // watchdog's premise). On "missing" (an agentd predating /live) the
        // reroute it predates nothing: that run_done stays the healer, so
        // once the retries are spent STOP — a permanent probe against a
        // server that will never answer is the worst skew behaviour, and a
        // store render would be the lossy path (no live join either there).
        tries += 1;
        await api(`/api/chat/${sid}/detach`, { method: "POST" });
      } else if (verdict === "missing") {
        clearInterval(detachTimer);
        detachTimer = null;
      }
      // "offline": the next tick retries. "stale": recovery moved on under
      // us; the guard inside the next tick ends the timer.
    } catch {
      // Still unreachable — the next tick retries.
    }
  }, 10000);
}

// Duplicate-subscription diagnostic (2026-10-13 word doubling): every stream
// names its page and which EventSource object opened it. The browser's own
// auto-reconnect reuses the URL (same n); a NEW EventSource gets a new n — so
// agentd's log tells an orphan EventSource from a duplicated request from two
// documents. The server is exactly-once: doubled words = one client holding
// TWO subscriptions — diagnose with nettop + lsof :8787.
const PAGE_ID = (() => {
  try {
    if (crypto.randomUUID) return crypto.randomUUID().replace(/-/g, "").slice(0, 8);
  } catch { /* insecure context or old engine — fall through */ }
  return Math.random().toString(36).slice(2, 10).padEnd(8, "0");
})();
let esSeq = 0;

// BK-032: `eventsSource` is the ONE instance a page holds. iOS re-opened CLOSED
// ones (agentd.log page 51db055b: old n back ~3 s — the engine's retry delay —
// after each wake; no desktop engine did it in orphan.py), and a revenant runs these
// handlers with nothing left to close it. So each handler asks `current()`
// first: a stale instance is closed again, its event dropped (orphan.py).
let eventsSource = null;
function connectEvents() {
  if (eventsSource) return;
  const n = ++esSeq;
  const es = new EventSource(`/api/events?page=${PAGE_ID}&n=${n}`);
  eventsSource = es;
  const current = () => {
    if (es === eventsSource) return true;
    es.close();
    tlog("events:orphan", { n, held: esSeq }); // ?recoverdebug capture
    return false;
  };
  es.onmessage = (e) => {
    if (!current()) return;
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleRunEvent(ev);
  };
  // Amber dot while the /api/events stream is down (auto-reconnect in flight).
  // Only surface it while visible — a hidden tab deliberately holds no socket.
  es.onopen = () => {
    if (!current()) return;
    $("#conn-dot").hidden = true;
    // A reconnect is often the moment the network came back — the /detach
    // that failed during the outage gets one more chance here.
    const pend = ui.resync;
    if (pend && !pend.seen)
      api(`/api/chat/${pend.id}/detach`, { method: "POST" }).catch(() => {});
  };
  es.onerror = () => {
    if (!current()) return;
    if (document.visibilityState === "visible") $("#conn-dot").hidden = false;
  };
  // EventSource auto-reconnects; the working-indicator clamp + watchdog cover
  // any run_start/run_done we miss across a reconnect.
}
function disconnectEvents() {
  $("#conn-dot").hidden = true; // an intentional close is not a dropped link
  if (!eventsSource) return;
  eventsSource.close();
  eventsSource = null;
}
if (document.visibilityState === "visible") connectEvents();

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    armRecoverDebug(); // ?recoverdebug=1 only: the recovery window may open right here
    connectEvents();
    loadSessions(); // picker freshness only — NEVER a liveness verdict (capped page)
    // T20 visibleResume: an armed resync, or a chat COLD-OPENED mid-answer —
    // a stored watermark with no dead stream (the socket closes while hidden),
    // or a PROVISIONAL slot (empty-tail join: no watermark, a live run to ask
    // about) — and a screen whose background run was in flight (T28, BK-027:
    // its run_done may have landed while this socket was closed). None of
    // these (a settled screen) ⇒ the transition asks NOTHING; every
    // verdict it does return runs through T27, the one entry point shared
    // with the watchdog and the run_done heal — no second decision tree.
    visibleResume(RUN_DEPS).then((res) => {
      if (res) applyEffects(res.effects, RUN_FX);
    });
  } else {
    // Release the SSE slot so the daemon stops counting this client as
    // in-app — hidden tabs must not suppress approval pushes.
    disconnectEvents();
  }
});
