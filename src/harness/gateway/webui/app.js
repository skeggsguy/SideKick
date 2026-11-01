/* Sidekick web UI — entry module. Vanilla JS ES modules, no build step.

   Module map: dom.js (safe-DOM/fetch helpers), state.js (shared mutables),
   transcript.js (render engine — owns ALL render state), approvals.js
   (cards/modal/history), schedules.js (schedules panel + form),
   settings.js (memory/connections/tools).
   This file wires them: tabs, chat send, timezone, boot. The rest lives in
   picker/theme-toggle/dictation/sessions/runs/push/working/sendmode/events.

   BK-019 (design scratch/plans/bk-019-design.md §5.2): this module is the
   CHAT-SSE ROUTER. The lifecycle decisions live in runstate.js (T01–T15);
   this file calls the named transition, dispatches the returned effects on
   CHAT_FX below, and keeps what is NOT run-lifecycle: the fetch/stream
   plumbing, the per-stream ctx (started — §10.1), the stall detector, the
   sink-side dedupe MARK for the bookkeeping types the transitions route by
   name, and the working indicator (T24 rules stay with working.js).

   Security invariant (see dom.js): untrusted content reaches the DOM only via
   textContent or renderMd (marked → DOMPurify). */
"use strict";

import { $, api } from "./dom.js";
import { ui } from "./state.js";
import {
  addErrorMsg, addSystemMsg, addUserMsg, closeSegments, dispatchStream,
  finalizeAgentBubble, isReplaying, paintStream, renderSession, resetTranscript,
} from "./transcript.js";
import { showGreeting } from "./greeting.js";
import { initAttach, attachBusy, attachmentsReady, clearAttachments } from "./attach.js";
import { initLightbox } from "./lightbox.js";
import {
  applyEvent as statusEvent, phase as statusPhase, setLane as statusLane,
  start as statusStart, stop as statusStop, warn as statusWarn, join as statusJoin,
} from "./status.js";
import { loadApprovals, loadApprovalsHistory, pollApprovalsSoon } from "./approvals.js";
import { loadConnections, loadContacts, loadMemory, loadTools } from "./settings.js";
import { loadTriggers, wireScheduleForm } from "./schedules.js";
import { chatChoice } from "./picker.js";
import "./theme-toggle.js";
import { initDictation } from "./dictation.js";
import { fetchSession, lastChatId, loadSessions, openSession, rememberLastChat, sessionIdFromHash, showNewChatOption } from "./sessions.js";
import { loadRuns } from "./runs.js";
import { alreadyPainted, markPainted, paintGapFill } from "./replay.js";
import { setSendMode } from "./sendmode.js";
import { bumpWork, markChatWork, stopChatWork, workOn } from "./working.js";
import "./push.js";
import { armDetachWatchdog, armObserveWatch, armRecoverDebug } from "./events.js";
import {
  applyEffects, latched, paintChat, runError, runFinish, runStopped, screenLeave,
  sendFailed, sendRejected, sessionAnnounce, socketDied, stopRequest, streamEndSilent,
  submit, titleSeen, turnComplete,
} from "./runstate.js";
import { createCapture } from "./trace.js"; // BK-023: the recovery capture's formatter

/* ---- top-level tabs ---- */
document.querySelectorAll("nav#tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav#tabs button, main > .tab").forEach((e) => e.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "approvals") loadApprovals();
    if (btn.dataset.tab === "runs") loadRuns();
    if (btn.dataset.tab === "settings") loadActiveSettingsSub();
  });
});

// Load the loader for whichever Settings subtab is currently active (not always
// Triggers — otherwise the shown pane and the loaded data desync).
const SETTINGS_LOADERS = { triggers: loadTriggers, memory: loadMemory, contacts: loadContacts, connections: loadConnections, tools: loadTools };
function loadActiveSettingsSub() {
  const active = $("#tab-settings .subnav button.active");
  (SETTINGS_LOADERS[active ? active.dataset.sub : "triggers"] || loadTriggers)();
}

/* ---- approvals sub-nav (pending / history) ---- */
document.querySelectorAll("#tab-approvals .subnav button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#tab-approvals .subnav button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    const hist = b.dataset.appr === "history";
    $("#approvals-list").hidden = hist;
    $("#approvals-history").hidden = !hist;
    if (hist) loadApprovalsHistory();
  });
});

/* ---- settings sub-nav ---- */
document.querySelectorAll("#tab-settings .subnav button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#tab-settings .subnav button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    document.querySelectorAll("#tab-settings .subtab").forEach((s) => s.classList.remove("active"));
    $(`#sub-${b.dataset.sub}`).classList.add("active");
    (SETTINGS_LOADERS[b.dataset.sub] || (() => {}))();
  });
});

if (location.hash === "#approvals") $('[data-tab="approvals"]').click();
if (location.hash.startsWith("#connections")) {
  $('[data-tab="settings"]').click();
  $('#tab-settings .subnav [data-sub="connections"]').click();
}

/* ---- composer autosize ---- */
// One JS path for both engines (iOS Safari lacks field-sizing:content).
// border-box: scrollHeight = content+padding, excludes the 1px borders → +2.
// CSS min/max-height clamp; overflow-y:auto scrolls past the cap. Programmatic
// .value writes fire no "input" event — those callsites invoke this directly.
const chatInput = $("#chat-input");
function autosizeInput() {
  chatInput.style.height = "auto";
  chatInput.style.height = `${chatInput.scrollHeight + 2}px`;
}
chatInput.addEventListener("input", autosizeInput);
autosizeInput(); // size bfcache-restored drafts at boot
initDictation(autosizeInput);
initAttach();
initLightbox();

// BK-019 fx dictionary (design §5.1) bound for the chat-SSE transport. The
// map is TOTAL: every token runstate's table can emit is bound here,
// including the /api/events-only ones (rerenderHeal/rerenderCold/gapFill/
// statusPhase/…) this transport never dispatches today — applyEffects THROWS
// on an unbound token, so dispatch totality is the contract, and
// tests/test_conventions.py checks these keys against runstate.js's source
// (never the paths a test happens to drive).
const CHAT_FX = {
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
  // the batch's OWN terminal verdict; preserveRead keeps a scrolled-up reader
  // put.
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
  observeWatch: () => armObserveWatch(), // BK-034 (events.js owns the bags)
  pollApprovalsSoon,
};

// The replay dedupe seam injected into runstate's paint transitions (T05).
const CHAT_DEPS = { alreadyPainted, markPainted, isReplaying };

// Bookkeeping types routed by NAME (never through the paint tail): they ride
// the sink-side dedupe gate, and the sink that sees one must MARK it —
// replay.js's painters skip these types UNMARKED precisely because the sink
// claims them ("belongs to whichever sink sees them", replay.js RENDERED note).
// `done` is chat-only (the events stream says run_done, handled upstream).
const BOOKKEEPING = { title: 1, turn_complete: 1, done: 1, stopped: 1, error: 1 };

const sendBtn = $("#chat-send");
sendBtn.addEventListener("click", (e) => {
  if (sendBtn.dataset.mode !== "stop") return; // normal Send: let it submit
  e.preventDefault(); // Stop mode: cancel the form submission
  // T04: no UI change here — the terminal event (`stopped`, or `done` if the
  // stop raced the answer) arrives on the stream and drives the state change.
  // The target is the live turn, falling back to the detached run (stopTarget).
  applyEffects(stopRequest(), CHAT_FX);
});

$("#chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (sendBtn.dataset.mode === "stop") return; // a turn is live — Enter is not a second send
  if (attachBusy()) return; // a photo is still uploading — 📎 shows the pending state
  const text = $("#chat-input").value.trim();
  const atts = attachmentsReady(); // [{id, mime, url}] of finished uploads
  if (!text && !atts.length) return; // a photo alone is a message too
  $("#chat-input").value = "";
  autosizeInput(); // back to rest height
  clearAttachments(); // chips done: the bubble shows the thumbs, previews revoked
  const choice = chatChoice(); // {effort} local lane, {model:"sonnet"} cloud
  // T01: arms ui.liveTurn and returns composer-stop + lane + row-start + the
  // user bubble. (The working indicator is a router duty, not an effect.)
  applyEffects(submit(text, atts, choice.model), CHAT_FX);
  workOn();
  markChatWork();
  // Per-request stream context: which session this stream renders into, and
  // whether the server confirmed the run started (§10.1 — that flag is the
  // transport's, never derivable from the fields). The for-good stream latch
  // is NOT here: it rides ui.liveTurn.detached (D3, T03), one copy per turn.
  const ctx = { session: ui.sessionId, started: false };
  // Stall detector: with pings every 15 s, 45 s of zero bytes means a
  // half-open socket (the reader would wait forever) — abort into the drop
  // path below, which keeps the run alive and arms the recovery.
  const ctrl = new AbortController();
  let lastByte = Date.now();
  const stall = setInterval(() => {
    if (Date.now() - lastByte > 45000) ctrl.abort();
  }, 5000);
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text, session_id: ui.sessionId, ...choice,
        ...(atts.length ? { attachment_ids: atts.map((a) => a.id) } : {}),
      }),
      signal: ctrl.signal,
    });
    if (!resp.ok) {
      // e.g. 409 while a previous (detached) turn is still running — the
      // JSON body would otherwise be swallowed by the SSE framing below.
      const detail = (await resp.json().catch(() => null))?.detail;
      // T12 (D5): clears the armed-but-never-started turn; the composer goes
      // back to stop only when a DIFFERENT run's resync still needs Stop.
      applyEffects(sendRejected(detail || `request failed: ${resp.status}`), CHAT_FX);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      lastByte = Date.now();
      bumpWork(); // includes ": ping" heartbeats — alive means alive
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const line = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        if (line.startsWith("data: ")) handleChatEvent(JSON.parse(line.slice(6)), ctx);
      }
    }
    // T14: stream closed without a terminal event. Field-gated (liveTurn ∧
    // ¬resync); after a terminal event liveTurn is already null → no-op.
    // BK-036: once the run STARTED a clean end is not proof it ended (iOS / a
    // proxy can close the stream error-free while the run goes on detached),
    // so T14 arms the resync exactly as T15 does and /live decides.
    endBreadcrumb("eof", ctx);
    applyEffects(streamEndSilent(ctx.session, ctx.started), CHAT_FX);
  } catch (err) {
    // Transport death. T15 first: AFTER the run started (ctx.started) the
    // server may NOT notice — a silently-vanished network leaves a zombie
    // stream that heartbeats into the void — so arm resync (Stop stays
    // armed, liveTurn KEPT), tell it to reroute the turn's events to
    // /api/events, and re-arm the detach POST's watchdog (that POST can die
    // in the same outage). THE resync (runstate T27, driven by events.js)
    // picks it back up — a recoverable state, not an error line. T13 (death
    // before `session`): an error line, and D5 clears the never-started turn.
    endBreadcrumb("error:" + (err && err.name), ctx);
    applyEffects(socketDied(ctx.session, ctx.started)
      || sendFailed(`request failed: ${err.message}`, ctx.started), CHAT_FX);
  } finally {
    clearInterval(stall);
    stopChatWork();
    loadSessions();
    loadApprovals();
  }
});

// BK-036 breadcrumb: HOW the chat stream ended, beside sendmode.js's composer
// flips — inert unless ?recoverdebug armed the log (events.js's tlog shape).
function endBreadcrumb(how, ctx) {
  const L = window.__sk && window.__sk._thinkLog;
  if (L) L.push(["chatEnd", { how, started: ctx.started, mode: sendBtn.dataset.mode }, null, Date.now()]);
}

// Once the displayed session diverges from the stream's (session picker, or a
// background run opened via its toast), the stream detaches for good: global
// bookkeeping still runs, but it must never touch the transcript render state —
// that belongs to whatever session is on screen (transcript.js owns it; the
// segment boundary goes through closeSegments()). The turn persists
// server-side; renderSession replays it on reopen. The latch itself is
// runstate's (T03, ui.liveTurn.detached — D3), read here through latched().
function handleChatEvent(ev, ctx) {
  // T03, before every event (as today's divergence check): the user switched
  // sessions — the stream no longer owns the screen OR the status row (the
  // visible session's own events drive it).
  applyEffects(screenLeave(), CHAT_FX);
  if (ev.type === "session") {
    ctx.session = ev.session_id;
    ctx.started = true; // server confirmed the run — a later transport death is non-terminal
    // T02: liveTurn.session updates UNCONDITIONALLY (§10.2 — a switch between
    // submit and this frame must not strand the turn's Stop/recovery); the
    // screen/picker writes ride the effect, only while the screen owns.
    applyEffects(sessionAnnounce(ev), CHAT_FX);
    return;
  }
  // Step-6 watermark on the chat-SSE sink (the server stamps these too):
  // track what this stream paints, and drop anything already painted — the
  // dedupe is per run_id, so a NEW run's renumbered events never read as
  // seen. A latched stream paints nothing, so it must not mark either: its
  // session's DOM is gone; reopening re-renders and re-primes the slot. The
  // content tail's gate lives INSIDE T05 (paintChat); the name-routed types
  // are claimed here because their transitions return before the tail.
  if (BOOKKEEPING[ev.type] && !latched()) {
    if (alreadyPainted(ctx.session, ev)) return;
    markPainted(ctx.session, ev);
  }
  if (ev.type === "turn_complete") {  // answer landed; naming may still run
    stopChatWork(); // the working indicator: a router duty (T24 set)
    applyEffects(turnComplete(ev), CHAT_FX); // T08
    return;
  }
  if (ev.type === "title") {
    // Early auto-name: relabel the option in place (no refetch). Titles are
    // model output → textContent. (Attached frames carry no session_id —
    // this stream IS the session: relabel ctx.session, never ui.sessionId.)
    applyEffects(titleSeen(ev, ctx.session), CHAT_FX); // T07
    return;
  }
  if (ev.type === "done") { applyEffects(runFinish(ev), CHAT_FX); return; }     // T09
  if (ev.type === "stopped") { applyEffects(runStopped(ev), CHAT_FX); return; } // T10
  if (ev.type === "error") { applyEffects(runError(ev), CHAT_FX); return; }     // T11
  applyEffects(paintChat(ev, ctx.session, CHAT_DEPS), CHAT_FX); // T05 tail
}

/* ---- client timezone ---- */
let lastReportedTz = null;
async function reportTimezone() {
  let tz;
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch { return; }
  if (!tz || tz === lastReportedTz) return;
  try {
    await fetch("/api/client/timezone", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tz }),
    });
    lastReportedTz = tz;
  } catch { /* offline; retry on next resume */ }
}
reportTimezone();

/* ---- boot ---- */
loadApprovals();
wireScheduleForm();
const deepLinked = sessionIdFromHash(location.hash);
if (deepLinked) openSession(deepLinked);
else restoreLastChat();

/* Cold start with no #session= deep link: reopen the chat the user was
   reading (iOS kills backgrounded PWAs; landing on the greeting every time
   is the complaint). A miss in the picker list is NOT a deletion — that list
   is capped at the 50 most recent sessions — so the verdict comes from the
   single-session route (sessions.js fetchSession): its row restores the chat
   even when it is off the page (option prepended, real title); only its
   200-null says deleted — then forget the id and greet. Any other answer
   (offline, or an agentd predating the route) shows the greeting and KEEPS
   the id: a blip must not eat the pointer. Never an error line on any path.
   "+ New chat" clears the id (sessions.js), so leaving on the new-chat
   screen stays left there. */
async function restoreLastChat() {
  const saved = lastChatId();
  if (saved) {
    const sessions = await api("/api/sessions").catch(() => null);
    if (Array.isArray(sessions)) {
      if (sessions.some((s) => s.id === saved)) { openSession(saved, sessions); return; }
      const row = await fetchSession(saved); // row | null=gone | undefined=?
      if (row) { openSession(saved, [row, ...sessions]); return; }
      if (row === null) rememberLastChat(null);
      showGreeting();
      loadSessions(sessions); // render the picker from the same fetch
      return;
    }
  }
  showGreeting();
  loadSessions(); // no id, or offline: the picker loads exactly as before
}

/* ---- on-device recovery capture (armed ONLY by ?recoverdebug=1) ----
   Streams the passive _thinkLog (events.js's armRecoverDebug owns the arming
   and the <pre>) into #recover-debug so the thinking-strip vanish can be
   captured on the phone without a Safari console. Line shape and the BK-023
   noise rule live in trace.js: every line carries its push-time EVENT stamp,
   and runs of consecutive no-op endThinking closes (the per-chunk ~85% of a
   streamed answer, BK-022's device trace) collapse to one `×count` line with
   a first/last span — real transitions stay verbatim. Inert otherwise:
   armRecoverDebug() returns null without the param, so no interval ever
   starts. Re-arming every tick costs one regex and survives resetTranscript()
   wiping the <pre> — the accumulated text is owned by the capture and
   re-filled into the replacement. */
if (armRecoverDebug()) {
  const cap = createCapture();
  setInterval(() => {
    const pre = armRecoverDebug(); // re-ensure (the log wipe re-creates the pre)
    const L = window.__sk._thinkLog;
    if (!pre || !L) return;
    const fresh = L.splice(0); // drain; the tlog guard sees the empty array, not a missing one
    if (!fresh.length) return;
    cap.push(fresh);
    pre.textContent = cap.text(); // textContent only — stacks are untrusted-ish (XSS rule)
    pre.scrollTop = pre.scrollHeight; // follow the trace
  }, 2000);
}
