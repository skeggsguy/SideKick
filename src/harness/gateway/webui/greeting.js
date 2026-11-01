/* The empty-chat greeting, split out of transcript.js (LOC cap) — a
   self-contained concern: it touches only #chat-greeting, never the shared
   render state (chatLog/agentBubble/pillRow). Time of day comes from Date() on
   the VIEWING device (iPad, not the server), since this runs client-side.
   días 6–12, tardes 12–20, noches otherwise. */
"use strict";

import { $ } from "./dom.js";

const greetEl = $("#chat-greeting");

function greetingText() {
  const h = new Date().getHours();
  const g = h >= 6 && h < 12 ? "Buenos días" : h >= 12 && h < 20 ? "Buenas tardes" : "Buenas noches";
  return `${g}, Tom`;
}

export function showGreeting() {
  $("#chat-greeting-text").textContent = greetingText();
  greetEl.classList.remove("fade");
  greetEl.hidden = false;
}

export function hideGreeting(fade) {
  if (greetEl.hidden) return;
  if (!fade) { greetEl.hidden = true; return; }
  greetEl.classList.add("fade");   // CSS transitions opacity → 0
  setTimeout(() => { greetEl.hidden = true; }, 650);
}
