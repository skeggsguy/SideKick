/* Theme bootstrap — loaded synchronously in <head> so the right palette is set
   before first paint (no flash). CSP forbids inline scripts, so this lives in
   its own same-origin file. Exposes window.applyTheme / window.themePref for
   the header toggle in app.js. */
"use strict";
(function () {
  // The status-bar / browser-chrome color per effective theme.
  var THEME_COLORS = { light: "#ffffff", dark: "#0f1722" };

  function effective(pref) {
    if (pref === "light" || pref === "dark") return pref;
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  // pref: "light" | "dark" | null (null = follow system). Returns the effective
  // theme actually applied.
  window.applyTheme = function (pref) {
    var root = document.documentElement;
    if (pref === "light" || pref === "dark") root.setAttribute("data-theme", pref);
    else { root.removeAttribute("data-theme"); pref = null; }
    try {
      if (pref) localStorage.setItem("theme", pref);
      else localStorage.removeItem("theme");
    } catch (e) { /* private mode */ }

    var eff = effective(pref);
    var m = document.querySelector('meta[name="theme-color"]');
    if (m) m.content = THEME_COLORS[eff];
    // Deliberately NO status-bar-style swap here: iOS reads that meta ONCE, at
    // Add to Home Screen (from the live DOM), and bakes it into the icon — a
    // dark-mode "black-translucent" got locked in for good, and iOS 27 mis-sizes
    // translucent web apps (62px blank band under the chat form). index.html's
    // static "default" is the only value; iOS 26+ tints the strip from the page.
    return eff;
  };

  window.themePref = function () {
    try { return localStorage.getItem("theme"); } catch (e) { return null; }
  };

  window.applyTheme(window.themePref());
})();
