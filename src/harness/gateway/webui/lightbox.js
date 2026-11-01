/* Full-size photo viewer for attachment thumbnails.

   A thumbnail used to be <a href="/media/<id>" target="_blank">. In a browser
   tab that opens the image in a new tab, but a STANDALONE PWA (iPad Safari /
   Android Chrome) has no back affordance — the webview got stranded on the raw
   image and the only way out was quitting the app. So we intercept the tap and
   show the photo in an in-app overlay (scrim + image + a ✕) instead: close via
   the ✕, a tap on the scrim, or Escape.

   XSS/CSP rules (dom.js header): built with el()/setAttribute only — no innerHTML;
   the ✕ label is static text, the image src is the same-origin /media/<id> the
   thumbnail already carries (CSP img-src 'self'), and the id is re-checked
   against MEDIA_ID_RE before it reaches <img src>. */
"use strict";

import { el } from "./dom.js";
import { MEDIA_ID_RE } from "./thumbs.js";

// One overlay, built once and reused. Appended at import (body exists: the
// entry script sits at the end of <body>); hidden until a thumbnail is tapped.
const box = el("div", "lightbox");
box.hidden = true;
box.setAttribute("role", "dialog");
box.setAttribute("aria-label", "Photo viewer");
const img = el("img", "lightbox-img");
img.setAttribute("alt", "Attached photo");
const closeBtn = el("button", "lightbox-close", "✕");
closeBtn.type = "button";
closeBtn.setAttribute("aria-label", "Close");
box.appendChild(closeBtn);
box.appendChild(img);
document.body.appendChild(box);

let opener = null; // the thumbnail to return focus to on close

function open(url, from) {
  opener = from || null;
  img.setAttribute("src", url);
  box.hidden = false;
  document.body.classList.add("lightbox-open"); // locks background scroll
  closeBtn.focus();
}

function close() {
  if (box.hidden) return;
  box.hidden = true;
  img.removeAttribute("src"); // release the decoded image
  document.body.classList.remove("lightbox-open");
  if (opener) { opener.focus(); opener = null; }
}

export function initLightbox() {
  // Delegated so it covers live-send AND replayed thumbnails alike: both render
  // <a class="attach-open">, and neither module needs to know the viewer exists.
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a.attach-open");
    if (!a) return;
    const url = a.getAttribute("href") || "";
    // Malformed id (shouldn't happen — thumbs.js guards at render): ignore.
    if (!MEDIA_ID_RE.test(url.slice("/media/".length))) return;
    e.preventDefault();
    open(url, a);
  });
  // The ✕ is a child of the box, so a click on it bubbles here too: any tap
  // that isn't the image itself dismisses (scrim or ✕). The <a>'s target=_blank
  // stays as a no-JS fallback — with JS active we always preventDefault above.
  box.addEventListener("click", (e) => { if (e.target !== img) close(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
}
