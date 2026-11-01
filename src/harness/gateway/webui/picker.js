/* Model/effort picker — one <select>, sent with each /api/chat.
   Effort levels for the local model plus a single "Sonnet" entry (opt-in
   cloud, pinned to medium server-side) revealed when /api/health says the
   host has a key. A per-device preference: localStorage, wrapped like
   theme.js (private mode / storage-blocked PWAs throw). Unknown stored
   values fall back to the default. */
"use strict";

import { $, api } from "./dom.js";

const EFFORTS = ["off", "low", "medium", "xhigh"];
const SONNET = "sonnet";
const DEFAULT_EFFORT = "low";

function pickerValue() {
  const v = $("#chat-effort").value;
  return v === SONNET || EFFORTS.includes(v) ? v : DEFAULT_EFFORT;
}

// /api/chat body fragment: Sonnet rides `model` and omits effort.
export function chatChoice() {
  const v = pickerValue();
  return v === SONNET ? { model: SONNET } : { effort: v };
}

{
  let saved = null;
  try { saved = localStorage.getItem("effort"); } catch (e) { /* private mode */ }
  const sel = $("#chat-effort");
  sel.value = saved === SONNET || EFFORTS.includes(saved) ? saved : DEFAULT_EFFORT;
  sel.addEventListener("change", () => {
    try { localStorage.setItem("effort", pickerValue()); } catch (e) { /* private mode */ }
  });
  // The Sonnet option ships hidden; a host with an API key reveals it.
  // Unknown health status (fetch failed) leaves it hidden — same policy as
  // the mic button.
  const hideSonnet = () => {
    const opt = sel.querySelector('option[value="sonnet"]');
    if (opt) opt.hidden = true;
    if (sel.value === SONNET || sel.selectedOptions[0]?.value === SONNET) {
      sel.value = DEFAULT_EFFORT;
      try { localStorage.setItem("effort", DEFAULT_EFFORT); } catch (e) { /* private mode */ }
    }
  };
  api("/api/health")
    .then((h) => {
      if (h?.sonnet === true) sel.querySelector('option[value="sonnet"]').hidden = false;
      else hideSonnet();
    })
    .catch(hideSonnet);
}
