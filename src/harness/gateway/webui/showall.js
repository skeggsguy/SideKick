/* "Show all" (BK-031, plan decision D3): a long thinking body or tool drawer
   flows at full height in the transcript — never an inner scroller. Those
   trapped swipes on iOS, and a wheel over an open drawer (overscroll-behavior:
   contain) never reached #chat-log. A body taller than its preview cap
   (chat.css --clip-h) is clipped there under a fade, with a "Show all" button
   below it; "Show less" clips it again. Each open starts clipped (resetClip).

   Anchors (BK-017's discipline — isFollowing() read before the change):
   - Show all grows the body BELOW what the reader sees: keepRead holds the
     preview still, the rest flows in under it, the button moves to the end.
   - Show less shrinks it ABOVE the button the reader just pressed: keepAt
     holds that button, so the preview's tail lands right above the finger.
   Both are instant: a per-frame animated collapse would need per-frame
   scroll writes to hold the button (the compensation BK-030 rejected).

   The box holds content written by its owner (textContent / el() only) —
   this module never touches it, only its classes. */
"use strict";

import { el } from "./dom.js";
import { keepRead, keepAt, readerOpened } from "./follow.js"; // + D1: Show all mid-run pauses

// One observer for every box — the net under the owners' own syncClip calls.
// Whether a box overflows its cap only changes when its border box changes
// too (it grows to the cap, or shrinks below it), so this sees every
// transition: live thinking growth, a width change, the Chat tab coming back
// from display:none. It reports AFTER layout, though, so an owner that has
// just written content syncs at once: the button lands in that same layout.
const ro = new ResizeObserver((entries) => {
  for (const e of entries) syncClip(e.target);
});

// resetTranscript has wiped #chat-log: every observed box died with it. Drop
// them all, so the observer never pins a dead transcript's DOM in memory.
export function resetClips() { ro.disconnect(); }

// Make `box` a clipped body; returns its button for the caller to place right
// after it (a sibling inside the same accordion body).
export function makeClip(box) {
  box.classList.add("clip");
  const btn = el("button", "show-all", "Show all");
  btn.type = "button";
  btn.hidden = true;
  btn.setAttribute("aria-expanded", "false");
  btn.addEventListener("click", () => {
    if (box.classList.contains("expanded")) {
      keepAt(btn, () => setExpanded(box, false));
    } else {
      keepRead(() => setExpanded(box, true));
      readerOpened(); // D1: opened to read — following pauses while a run is live
    }
  });
  box._clipBtn = btn;
  ro.observe(box);
  return btn;
}

// Back to the clipped preview without anchoring — for an open, which starts
// clipped (the accordion animates to the preview height).
export function resetClip(box) {
  if (box.classList.contains("expanded")) setExpanded(box, false);
}

function setExpanded(box, on) {
  box.classList.toggle("expanded", on);
  box._clipBtn.textContent = on ? "Show less" : "Show all";
  box._clipBtn.setAttribute("aria-expanded", on ? "true" : "false");
  syncClip(box);
}

// The fade + button follow the measured overflow. Expanded keeps its button
// ("Show less" is the way back) even if the content has since shrunk. Under
// display:none (no layout box) nothing measures: skipped, and the observer
// re-syncs on the way back — hiding every button there would re-flow the log.
export function syncClip(box) {
  if (!box.getClientRects().length) return;
  const open = box.classList.contains("expanded");
  const over = !open && box.scrollHeight > box.clientHeight + 1;
  box.classList.toggle("clipped", over);
  box._clipBtn.hidden = !(over || open);
}
