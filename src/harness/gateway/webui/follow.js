/* Scroll follower for #chat-log: keeps a streaming chat glued to the bottom
   with a spring instead of per-frame snaps, and lets go when the user reaches
   back to read.

   - Spring: use-stick-to-bottom's model. The rAF loop runs only while pinned
     and not yet settled — no idle rAF.
   - Unpin on USER GESTURE only (ANY press on the log — finger, mouse or
     pen — wheel up) —
     never on distance from the bottom, which the spring itself briefly
     exceeds. So any PROGRAMMATIC scroll away from the bottom (scrollIntoView,
     scrollTop writes) must call unpin() first, or the spring yanks it back.
     Gesture coverage also reads the scroll events wheel/touch cannot see
     (keyboard, scrollbar drag): an UPWARD move that is neither one of our
     own writes nor a clamp (which lands exactly AT maxTop) is user intent —
     unpin. Direction + attribution, never distance (decisions 2026-09-28
     rejected distance-based unpin; this rule reads none).
   - Any press is the reader's "stop moving" (user, 2026-10-31, BK-029 — user
     control over tap≠scroll; reaffirms 2026-10-13's any-touch rule and adds
     mouse/pen). D1: OPENING a pill drawer or thinking body while a run is
     live pauses too (readerOpened) — the keyboard's path, which no press sees.
   - Re-pin when the user scrolls back down to within 40px of the bottom (this
     also catches iOS momentum), or taps the "↓ Latest" pill.
   - BK-017: isFollowing() is the ONE followed/at-bottom predicate every
     scroll decision in the app shares, and keepRead()/captureRead()/
     restoreRead() preserve the read position across height changes —
     content shrinks AND viewport (clientHeight) changes — so the browser's
     scrollTop clamp never silently eats it.
   - BK-030: an ANIMATED close (accordion, ~200 ms) shrinks the log frame by
     frame, out of keepRead's synchronous reach; holdShrink() reserves that
     shrink up front as bottom slack so the reader's tapped header holds still,
     and a press on a transcript button holds the pill's reveal until its click.
   - BK-031: "Show less" (showall.js) holds its own button instead (keepAt).

   The pill (#chat-jump) lives OUTSIDE #chat-log: resetTranscript wipes the
   log's innerHTML. All scroll state is private to this module. */

import { $ } from "./dom.js";
import { screenRunLive } from "./runstate.js"; // D1's "streaming" (state.js only below it: DAG)

const log = $("#chat-log");
const jumpBtn = $("#chat-jump");
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");

const TIGHTNESS = 0.05;   // spring stiffness (demo pane D default)
const REPIN_GAP = 40;     // px from the bottom that counts as "back at the bottom"

let pinned = true;
let pos = 0;              // float scroll position the spring integrates
let vel = 0;
let raf = 0;
let lastFrame = 0;

let touchY0 = null;       // finger position at touchstart; null = no touch
let lastTop = 0;          // previous scrollTop, for scroll direction
let movedDown = false;    // did the most recent scroll move toward the bottom?
let lastWritten = 0;      // last scrollTop value WE wrote (rounding included),
                          // for write attribution in the scroll handler
let slack = 0;            // BK-030 bottom slack, px (#chat-log's --slack padding)
let holdUntil = 0;        // performance.now() until which the slack must not trim
let holdTimer = 0;
let pillHeld = false;      // BK-030: unpinned by a press on a transcript button, pill
                          // reveal held until that press's click lands (see below)

const maxTop = () => Math.max(0, log.scrollHeight - log.clientHeight);
const canScroll = () => log.scrollHeight - log.clientHeight > 1;

// Our own writes, recorded for attribution: a scroll event landing back on
// (or within rounding of) this value is ours, not the user's. Browsers round
// scrollTop; follow.js already codes around that below (frame's re-seed).
function writeTop(v) {
  lastWritten = v;
  log.scrollTop = v;
}

// The direction tracker must not read our own write as user movement (see
// unpin's comment for the queued-scroll-event reason keepRead shares).
function rebase() {
  lastTop = log.scrollTop;
  movedDown = false;
}

function setPinned(p) {
  pinned = p;
  if (!pillHeld) jumpBtn.hidden = p; // a held reveal writes it when the press ends
  if (p && slack) setSlack(0); // a follower's bottom is the real one (the clamp lands it there)
}

// BK-030 bottom slack. An animated close shrinks the content under an UNPINNED reader
// over ~200 ms; near the bottom the browser would clamp scrollTop every frame and slide
// the tapped header down. No per-frame write fixes a clamp, so holdShrink() adds the
// deficit as padding under the content BEFORE the close starts (cost: blank space at the
// very bottom, at most the closed height). After that the slack only SHRINKS
// (settleSlack: streamed content, an upward scroll, a finished transition); a re-pin drops it.
function setSlack(px) {
  slack = px;
  log.style.setProperty("--slack", `${px}px`);
}

// The accordion duration, read from the CSS token (one source for the feel
// value; reduced-motion makes it 0s).
function accMs() {
  const v = getComputedStyle(log).getPropertyValue("--acc-dur").trim();
  const n = parseFloat(v) || 0;
  return v.endsWith("ms") ? n : n * 1000;
}

// Call BEFORE a mutation that will shrink the content below the reader by up
// to `px` — over `ms` (an animated close), or at once (ms 0). Pinned followers
// just follow: the clamp keeps them at the bottom, which is where they belong.
export function holdShrink(px, ms = accMs()) {
  if (pinned || px <= 0) return;
  const need = Math.ceil(log.scrollTop - (maxTop() - px));
  // > 1: a sub-pixel deficit (a 545.2 px body under a 545 px gap) clamps by
  // < 1 px — not worth a blank pixel row under the content.
  if (need > 1) setSlack(slack + need);
  // No trim mid-transition: the slack is sized for the END, so trimming it to
  // an in-between frame's need would let the remaining frames clamp.
  holdUntil = Math.max(holdUntil, performance.now() + ms);
  clearTimeout(holdTimer);
  holdTimer = setTimeout(settleSlack, Math.max(0, holdUntil - performance.now()) + 34);
}

// Trim the slack to what still holds the current view; never grows it.
export function settleSlack() {
  if (!slack || performance.now() < holdUntil) return;
  const real = log.scrollHeight - slack;
  const need = Math.max(0, Math.ceil(log.scrollTop + log.clientHeight - real));
  if (need < slack) setSlack(need);
}

function stopLoop() {
  if (raf) { cancelAnimationFrame(raf); raf = 0; }
  lastFrame = 0;
}

// Instant jump to the bottom, spring state synced so the loop doesn't jerk.
function snap() {
  writeTop(maxTop());
  pos = log.scrollTop;
  vel = 0;
}

function frame(now) {
  raf = 0;
  if (!pinned) { vel = 0; lastFrame = 0; return; }
  const dt = lastFrame ? Math.min(now - lastFrame, 100) : 16.7;
  lastFrame = now;
  const bottom = maxTop();
  // Browsers round scrollTop; only a real divergence (the user, a resize, a
  // snap elsewhere) re-seeds the float position. BK-017 (c): the divergence
  // check doubles as the unpin gate, BECAUSE the re-seed would mask the user
  // move — chromium measured: PageUp moves the view, our next-frame write
  // lands ON it (the re-seed makes pos follow the user), and the scroll
  // handler then attributes the move to us (|top − lastWritten| ≤ 2) while
  // the spring drags the reader back down. Same direction + attribution
  // rule as the scroll handler — up, not ours, not the clamp point — but
  // evaluated against OUR trajectory (pos), before anything re-seeds.
  if (Math.abs(log.scrollTop - pos) > 2) {
    if (touchY0 === null && log.scrollTop < pos
        && maxTop() - log.scrollTop > 1 && Math.abs(log.scrollTop - lastWritten) > 2) {
      unpin(); // stopLoop inside: nothing below runs, nothing writes
      return;
    }
    pos = log.scrollTop;
  }
  const diff = bottom - pos;
  vel = (0.7 * vel + TIGHTNESS * diff) / 1.25;
  pos += vel * (dt / 16.7);
  let settled = false;
  if (Math.abs(diff) < 0.5 && Math.abs(vel) < 0.2) { pos = bottom; vel = 0; settled = true; }
  if (pos > bottom) { pos = bottom; vel = 0; }   // never overshoot the bottom
  writeTop(pos);
  if (settled) { lastFrame = 0; return; }
  raf = requestAnimationFrame(frame);
}

// Glide to the bottom if pinned. Call after any content change.
export function follow() {
  if (!pinned) { settleSlack(); return; } // new content fills the slack it lands in
  // More than a screenful to go (scroll position lost — e.g. across the Chat
  // tab's display:none — or a big block landed at once) would be a long,
  // pointless glide: snap. (Tab re-show itself is snapped by the ResizeObserver.)
  if (reducedMotion.matches || maxTop() - log.scrollTop > log.clientHeight) {
    stopLoop(); snap(); return;
  }
  if (raf) return;                       // already gliding; it targets the live bottom
  pos = log.scrollTop;
  vel = 0;
  raf = requestAnimationFrame(frame);
}

// Instant, and pins: the user's own message, session render, errors/notices.
export function jumpToBottom() {
  stopLoop();
  setPinned(true);
  snap();
}

export function unpin() {
  stopLoop();
  vel = 0;
  setPinned(false);
  // Re-baseline the direction tracker: the spring's last scrollTop write may
  // still have a scroll event queued, and it must not read as the user
  // scrolling DOWN near the bottom (maybeRepin would undo this unpin at once —
  // seen in WebKit, whose wheel scroll lands asynchronously).
  rebase();
}

// D1 (frontend-smoothness, user 2026-10-31): the reader OPENED a pill drawer
// or a thinking body — to read it. While a run is live on screen the next
// delta would glide it away, so following pauses: "↓ Latest" shows, one tap on
// it resumes, and closing never auto-resumes. No run live: nothing is coming,
// so nothing changes. A tap or click already unpinned at its press (below);
// this is what covers a KEYBOARD open (Enter/Space on the focused control).
export function readerOpened() {
  if (pinned && screenRunLive()) unpin();
}

// THE followed/at-bottom predicate (BK-017) — one threshold (REPIN_GAP), no
// second copy, shared by the paced autoscroll and EVERY completion-path
// scroll. Both terms are load-bearing: `pinned` alone lies (a write-free
// downward move re-pins via maybeRepin), gap alone lies too (one ArrowUp from
// the bottom leaves gap ≈ 19 < REPIN_GAP, and a gap-only gate yanks the exact
// keyboard reader the unpin rule exists to save).
// CAPTURE DISCIPLINE: evaluate it BEFORE the height change it gates. Read
// after the final renderMd has grown the log, the gap term reads false, the
// gated follow() is skipped, and a followed answer never settles.
// REVIEW-ENFORCED: planting a late capture, a dropped gap term or dropped write
// attribution left the whole probe matrix green (REPIN_GAP slack + the spring hide it).
export function isFollowing() {
  return pinned && maxTop() - log.scrollTop < REPIN_GAP;
}

// Read-position preservation across a height change (content shrink OR
// viewport/clientHeight change — both bite the same clamp). Snapshot, let the
// caller mutate synchronously, then restore. Two branches, each measured
// against a measured BK-017 failure: keep scrollTop when it is STILL VALID
// (≤ new maxTop) — pure gap-preservation would drag a far-up reader up their
// history on every segment collapse; only when the change clamped it, restore
// the pre-change GAP (the status-row hide leaves scrollHeight UNCHANGED, so
// any distance-from-bottom rule writes nothing and the clamp still yanks).
// A pinned view needs nothing: the clamp parks it at the new bottom, which
// is exactly where a follower belongs.
export function captureRead() {
  const top = log.scrollTop;
  return { top, gap: maxTop() - top, following: isFollowing() };
}

export function restoreRead(read) {
  if (read.following) return;
  const mt = maxTop();
  const target = read.top <= mt ? read.top : Math.max(0, mt - Math.min(read.gap, mt));
  if (log.scrollTop !== target) {
    writeTop(target);
    rebase(); // our write must not read back as user movement (unpin's reason)
  }
}

// The bracket form: run the DOM mutation synchronously inside, and the view
// keeps its read position across it. Synchronous by contract — it must sit
// inside renderSession's atomic swap and land before paint (observer-based
// compensation would only ever see the ALREADY-clamped value).
export function keepRead(fn) {
  const read = captureRead();
  const out = fn();
  restoreRead(read);
  return out;
}

// BK-031: hold ONE element's screen y across a synchronous shrink ABOVE it —
// "Show less" stays under the finger, the preview's tail landing just above it
// (keepRead would keep scrollTop and fling a deep reader into the next rows).
// The shrink is all above the element, so the target (scrollTop − shrink) is
// ≤ the new maxTop: never a clamp, no slack. A follower just follows.
export function keepAt(node, fn) {
  if (isFollowing()) return fn(); // captured BEFORE the change (BK-017)
  const y = node.getBoundingClientRect().top;
  const out = fn();
  const dy = node.getBoundingClientRect().top - y; // after any clamp the shrink caused
  if (Math.abs(dy) >= 0.5) { writeTop(Math.max(0, log.scrollTop + dy)); rebase(); }
  return out;
}

export function resetFollow() {
  stopLoop();
  pillHeld = false; // a reset mid-press: setPinned below must write the pill
  setPinned(true);
  pos = 0;
  vel = 0;
  touchY0 = null;
  lastTop = 0;
  movedDown = false;
  lastWritten = 0;
  holdUntil = 0;
  clearTimeout(holdTimer);
}

export function isPinned() { return pinned; }

// Back near the bottom by the user's own downward scroll — never while a finger is
// down (the drag that unpinned would re-pin and fight it), never on an upward scroll.
function maybeRepin() {
  if (pinned || touchY0 !== null || !movedDown) return;
  if (maxTop() - log.scrollTop < REPIN_GAP) { setPinned(true); follow(); }
}

// After a programmatic scroll that followed unpin(): a scrollIntoView whose
// target is already on screen moves nothing, so no scroll event fires and the
// user is left unpinned AT the bottom with a stale pill. Re-pin if still there.
export function repinIfAtBottom() {
  if (pinned || touchY0 !== null) return;
  if (maxTop() - log.scrollTop < REPIN_GAP) { setPinned(true); follow(); }
}

// Any press in the log unpins at once (2026-10-13 for touch, BK-029 for
// mouse/pen): no deadzone to wait out, no spring writing under a resting
// thumb. This touchstart listener is also what lets :active (chat.css) fire on iOS.
log.addEventListener("touchstart", (e) => {
  touchY0 = e.touches[0].clientY;
  if (pinned && canScroll()) unpin();
}, { passive: true });
// Mouse and pen: the same rule at pointer-down. Touch pointers are left to
// touchstart above — it also owns touchY0, maybeRepin's finger guard.
log.addEventListener("pointerdown", (e) => {
  if (e.pointerType !== "touch" && pinned && canScroll()) unpin();
}, { passive: true });
const touchDone = () => { touchY0 = null; maybeRepin(); };
log.addEventListener("touchend", touchDone, { passive: true });
log.addEventListener("touchcancel", touchDone, { passive: true });
log.addEventListener("wheel", (e) => {
  if (e.deltaY < 0 && pinned && canScroll()) unpin();
}, { passive: true });
log.addEventListener("scroll", () => {
  const top = log.scrollTop;
  if (top !== lastTop) movedDown = top > lastTop;
  // The gesture listeners cannot see the keyboard or a scrollbar drag, so
  // their scroll events stand in (BK-017 mechanism (c)): pinned + a move
  // UP that is NOT one of our own writes (|top - lastWritten| ≤ 2 covers the
  // rounding frame() already codes around) and does NOT land at the clamp
  // point (a clamp parks exactly AT maxTop, gap ≤ 1 — a real scroll-up
  // cannot, one ArrowUp leaves ~19). Direction + attribution, never a
  // distance threshold (see the header note + decisions 2026-09-28).
  if (pinned && touchY0 === null && top < lastTop && canScroll()
      && maxTop() - top > 1 && Math.abs(top - lastWritten) > 2) {
    unpin(); // rebases the direction tracker
  }
  lastTop = log.scrollTop;
  maybeRepin();
  settleSlack(); // an upward scroll uncovers the slack: drop what it no longer holds
}, { passive: true });
log.addEventListener("transitionend", settleSlack); // an accordion finished

// The pill ignores only the GHOST click: a tap's click is hit-tested where the
// finger LIFTS, so a tap on the newest line unpins at touchstart, the pill
// appears under that same finger, and the tap's own click would re-pin at once
// — the press undone (BK-029, scroll_follow press-under-pill-stays-unpinned).
// pressOffPill is true only between a real press that started OFF the pill and
// the click that follows it: any click consumes it, pointercancel (a drag) and
// a short timer after pointerup (a drag-off-then-release, no click) clear it.
// A click with no preceding press — keyboard (detail 0), VoiceOver's
// synthesized activation — therefore always lands: the pill cannot go dead.
// A press on a transcript BUTTON (thinking header, tool pill) still unpins at
// once, but the pill's reveal waits until that press's click has gone to the
// button (BK-030): D2's streaming strip is the log's LAST row, its header right
// where the pill pops up — a touch's click then landed on the pill (eaten as
// the ghost) and a mouse click on neither (mousedown and mouseup on different
// elements), so "Thinking…" could not be opened while it streamed.
let pressOffPill = false;
let pressClear = 0;
let heldClear = 0;
function revealHeld() {
  clearTimeout(heldClear);
  if (!pillHeld) return;
  pillHeld = false;
  jumpBtn.hidden = pinned;
}
function pressStart(e) {
  clearTimeout(pressClear);
  pressOffPill = !jumpBtn.contains(e.target);
  if (pressOffPill && log.contains(e.target) && e.target.closest("button")) pillHeld = true;
}
// Capture on document: before #chat-log's own unpinning listeners below (and
// touchstart too — engines differ on whether it precedes pointerdown).
document.addEventListener("pointerdown", pressStart, { capture: true, passive: true });
document.addEventListener("touchstart", pressStart, { capture: true, passive: true });
document.addEventListener("pointercancel", () => { pressOffPill = false; revealHeld(); },
  { capture: true, passive: true });
document.addEventListener("pointerup", () => {
  clearTimeout(pressClear);
  pressClear = setTimeout(() => { pressOffPill = false; }, 400);
  clearTimeout(heldClear);
  if (pillHeld) heldClear = setTimeout(revealHeld, 400); // a press that never clicks
}, { capture: true, passive: true });
document.addEventListener("click", revealHeld); // bubble: after the button's own handler
// The pill is OUTSIDE #chat-log: its tap doesn't stop an iOS glide, which overrides our writes
// and re-unpins (iPhone, 2026-11-01). WebKit cancels a glide when the log stops scrolling.
function pillRepin() {
  log.style.overflowY = "hidden";
  requestAnimationFrame(() => requestAnimationFrame(() => { log.style.overflowY = ""; }));
  setPinned(true); follow();
}
jumpBtn.addEventListener("click", (e) => {
  const ghost = pressOffPill && e.detail !== 0;
  pressOffPill = false;
  if (!ghost) pillRepin();
});
// BK-039: a tap that stops an iOS momentum glide gets NO click — a still touch on the pill re-pins at its lift.
let pillTouch = null;
jumpBtn.addEventListener("touchstart", (e) => { pillTouch = [e.touches[0].clientX, e.touches[0].clientY]; }, { passive: true });
jumpBtn.addEventListener("touchend", (e) => {
  const t = e.changedTouches[0], p = pillTouch; pillTouch = null;
  if (!p || Math.hypot(t.clientX - p[0], t.clientY - p[1]) > 10) return;
  e.preventDefault(); // the re-pin happened here: no click after it
  pillRepin();
}, { passive: false });

// Composer autosize (app.js) shrinks #chat-log as the field grows; the browser
// keeps scrollTop, hiding the newest messages. Re-bottom instantly while pinned.
new ResizeObserver(() => { if (pinned) snap(); }).observe($("#chat-form"));
