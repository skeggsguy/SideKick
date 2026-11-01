/* The live model-status row — the chat's ONLY visible "working" indicator.

   Owns #chat-status (an in-flow row at the transcript's foot that reserves
   its own space and vanishes at turn end; aria-hidden — a 1 Hz timer must
   never tick inside the aria-live log).
   start() on submit, phase() on every stream event that
   changes what's happening, warn() for a dropped stream, stop() at the turn's
   end. The mm:ss timer runs for the whole turn; the "local model can take a
   while" hint appears after 20 s of waiting. All text via textContent. */
"use strict";

import { $ } from "./dom.js";
// BK-017: every toggle below changes #chat-status's height, and the row is
// IN FLOW (chat.css #chat-status) — so each one changes #chat-log's clientHeight and
// drops maxTop, clamping an unfollowed near-bottom view with no scroll call
// anywhere. keepRead compensates the measured delta; the row stays in flow.
import { keepRead } from "./follow.js";
import { approvalOverlay, labelFor } from "./labels.js";

const row = $("#chat-status");
const labelEl = $("#chat-status-label");
const timerEl = $("#chat-status-timer");
const subEl = $("#chat-status-sub");

const SLOW_HINT_MS = 20000;
const SUB_LOCAL = "The local model can take a while on long chats.";
const SUB_SONNET = "Sonnet · medium effort — this answer leaves the machine.";
subEl.textContent = SUB_LOCAL;

/* Which model the NEXT turn goes to (app.js calls this before start()):
   swaps the sub-line. Queue phases ("busy", "reading %") only ever fire
   on the local lane — Sonnet turns have no slot watcher. */
export function setLane(model) {
  subEl.textContent = model === "sonnet" ? SUB_SONNET : SUB_LOCAL;
}

let interval = null;
let t0 = 0;
let waitingSince = null; // phase clock for the slow-model hint
let shown = null; // {label, waiting} on the row now
let covered = null; // the label the approval overlay hides (BK-041 G1)

function fmt(ms) {
  const s = Math.floor(ms / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function tick() {
  const now = Date.now();
  timerEl.textContent = fmt(now - t0);
  const hidden = !(waitingSince !== null && now - waitingSince >= SLOW_HINT_MS);
  // The 20 s slow-hint appearing shrinks #chat-log by a line: bracket the
  // toggle (only a real toggle — keepRead reads layout, don't force it free).
  if (hidden !== subEl.hidden) keepRead(() => { subEl.hidden = hidden; });
}

function show(label, waiting) {
  // The same wait re-announced (an observer's 2 s /live tick, an approvals
  // poll) keeps its clock — else the 20 s slow hint could never appear.
  const same = waiting && waitingSince !== null && shown?.label === label;
  shown = { label, waiting };
  labelEl.textContent = label;
  if (!same) waitingSince = waiting ? Date.now() : null;
  if (!subEl.hidden) keepRead(() => { subEl.hidden = true; });
  if (row.hidden) keepRead(() => { row.hidden = false; });
  if (interval === null) {
    t0 = Date.now();
    tick();
    interval = setInterval(tick, 1000);
  }
}

export function start() {
  show("Waiting for model…", true);
}

export function phase(label, opts = {}) {
  covered = null; // the run moved on: an approval overlay never restores past this
  if (row.hidden) {
    // Mid-run join (recovery path): first sight of the turn starts the timer.
    start();
    show(label, !!opts.waiting);
    return;
  }
  row.classList.remove("warn");
  show(label, !!opts.waiting);
}

/* A mid-run join (BK-041 G4): the server's {started_ms, phase} for a run
   already under way — /messages' live or bg_live. The row opens at the run's
   current label with its REAL elapsed time, not "Waiting for model…" from
   0:00. A row already running (this page's own stream) is left alone. */
export function join(view) {
  if (!view || !row.hidden) return;
  const l = labelFor(view.phase) || { label: "Waiting for model…", waiting: true };
  phase(l.label, { waiting: l.waiting });
  if (view.started_ms > 0) t0 = Math.min(view.started_ms, Date.now());
  tick();
}

/* The stream DIED but the run is alive server-side — amber, still counting.
   Not an error: the reply catches up via /api/events. */
export function warn(label) {
  row.classList.add("warn");
  show(label, false);
}

/* approvals.js, on every poll: is an approval pending for the on-screen chat?
   Only a visible row is overlaid — no run on screen, nothing to explain. */
export function approvalWait(pending) {
  if (row.hidden) { covered = null; return; }
  const r = approvalOverlay(shown, covered, pending);
  covered = r.covered;
  if (r.show) show(r.show.label, r.show.waiting);
}

export function stop() {
  covered = null;
  clearInterval(interval);
  interval = null;
  waitingSince = null;
  // The row is 28 px (more with the sub-line) of in-flow height vanishing at
  // the completion boundary — the status-clamp BK-017 measured, bracketed.
  if (!row.hidden) keepRead(() => { row.hidden = true; });
  row.classList.remove("warn");
}

// One phase map for both renderers (chat SSE + /api/events recovery) — the
// words live in labels.js (pure, jsunit-pinned).
export function applyEvent(ev) {
  const l = labelFor(ev);
  if (l) phase(l.label, { waiting: l.waiting });
}
