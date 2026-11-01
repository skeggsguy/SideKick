/* The one Send/Stop button, two modes. Exported so both the chat submit path
   (app.js) and the live-events recovery (events.js) flip it. */
"use strict";

import { $ } from "./dom.js";
import { syncMicWidth } from "./dictation.js";

const sendBtn = $("#chat-send");

// One button keeps syncMicWidth() honest (the mic matches Send's measured
// width). In stop mode a click POSTs /stop instead of submitting — the click
// handler cancels the submit, and the submit guard in app.js covers Enter.
export function setSendMode(mode) {
  // BK-036 breadcrumb: every Send/Stop flip, stamped, into the ?recoverdebug
  // capture (inert without it) — a Stop lost mid-run shows WHEN, next to the
  // stream's own end (app.js chatEnd). The [name, state, stack, ms] shape.
  const L = window.__sk && window.__sk._thinkLog;
  if (L && sendBtn.dataset.mode !== mode) L.push(["composer", { mode }, null, Date.now()]);
  sendBtn.dataset.mode = mode;
  sendBtn.textContent = mode === "stop" ? "Stop" : "Send";
  syncMicWidth();
}

export function sendMode() {
  return sendBtn.dataset.mode;
}
