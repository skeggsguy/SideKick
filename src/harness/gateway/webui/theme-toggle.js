/* Header theme toggle (System → Light → Dark). The bootstrap itself lives in
   theme.js (a plain head script — no FOUC); this is the DOM wiring, which
   needs the DOM, so it's a module. */
"use strict";

import { $ } from "./dom.js";

const THEME_ORDER = [null, "light", "dark"];
const THEME_ICON = { null: "🌗", light: "☀️", dark: "🌙" };
const THEME_NAME = { null: "System", light: "Light", dark: "Dark" };

function refreshThemeBtn() {
  const p = window.themePref ? window.themePref() : null;
  const btn = $("#theme-btn");
  if (!btn) return;
  const next = THEME_ORDER[(THEME_ORDER.indexOf(p) + 1) % THEME_ORDER.length];
  btn.textContent = THEME_ICON[p] || "🌗";
  // Announce the current state AND what a tap will do.
  btn.setAttribute("aria-label", `Theme: ${THEME_NAME[p]} — tap for ${THEME_NAME[next]}`);
}

$("#theme-btn").addEventListener("click", () => {
  const p = window.themePref ? window.themePref() : null;
  const next = THEME_ORDER[(THEME_ORDER.indexOf(p) + 1) % THEME_ORDER.length];
  window.applyTheme(next);
  refreshThemeBtn();
});

// Track OS theme changes while on "system".
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (!(window.themePref && window.themePref())) window.applyTheme(null);
});

refreshThemeBtn();
