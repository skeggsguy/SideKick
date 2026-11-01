/* BK-017 scroll-follow probe instrument (tests/fixtures/scroll_follow/).

   The probe page's module entry (it replaces the app.js entry tag): it
   installs the scroll telemetry BEFORE importing ./app.js, then boots the
   REAL app over the mock fetch. run_probe.py (Playwright, real time) drives
   user actions — typing, submit, a real wheel gesture, PageUp keys — and
   reads window.__probe back.

   WHAT THE TELEMETRY SEPARATES (plan bk-017 Step 1's three candidates):
   - writes: EVERY JavaScript write to #chat-log.scrollTop, with the call
     stack (names survive — no bundler). follow.js's spring (frame), snap
     (jumpToBottom / follow's long-glide shortcut) and any other writer land
     here. The BROWSER's own clamp on a content shrink does NOT go through
     the JS setter — a scrollTop change with NO matching write is a clamp.
   - samples: scrollTop / scrollHeight / clientHeight / isPinned() / pill
     visibility on a 8 ms interval — the shape of any jump, and whether
     scrollHeight moved with it (clamp signature: scrollTop drops AS
     scrollHeight drops).
   - scrolls: the log's own scroll events (user, spring, or clamp).
   - doms: #chat-log child-count mutations — brackets the completion
     re-render (finalizeAgentBubble's innerHTML swap) in wall time.

   isPinned() comes from the SAME follow.js instance the app imported
   (relative specifiers resolve identically from /probe_scroll.js and
   /app.js), so it reads production state, not a mirror. */
import { ui } from "./state.js";
import { isPinned } from "./follow.js";
// Version-tolerant on purpose (red-discipline runs boot the PRE-fix app):
// `isFollowing` only exists post-BK-017, and a NAMED import of a missing
// export fails the whole module link — the app never boots. A namespace
// import never does, so the probe measures both contracts; pre-fix the gate
// predicate simply degrades to isPinned() (what the old code had).
import * as followMod from "./follow.js";
import { openSession } from "./sessions.js";
import {
  addUserMsg, dispatchStream, finalizeAgentBubble, resetTranscript,
} from "./transcript.js";

const logEl = document.querySelector("#chat-log");
const pill = document.querySelector("#chat-jump");
const subEl = document.querySelector("#chat-status-sub");

const T = { samples: [], writes: [], scrolls: [], doms: [], subs: [], pins: [],
            labels: [], rowToggles: [], meta: {} };
window.__T = T; // raw telemetry for console spelunking

// The status sub-line's show/hide moments (BK-017 status-sub scenario):
// attribute the compensation writes to the TOGGLE, not to the SSE frame that
// armed the hint — the toggle lands on the 1 s tick, up to a second later.
new MutationObserver(() => {
  T.subs.push({
    t: Math.round(performance.now()),
    hidden: subEl.hidden,
    h: subEl.offsetHeight,
    ch: logEl.clientHeight,
  });
}).observe(subEl, { attributes: true, attributeFilter: ["hidden"] });

// Probe-only clock shift (BK-017 status-sub scenario): status.js's slow-model
// hint shows the sub-line only after SLOW_HINT_MS (20 s) of waiting. A
// CONSTANT Date.now shift would change nothing — tick() reads both sides of
// its own subtraction. So shiftClock() raises the clock +21 s exactly once,
// AFTER the model_wait frame has armed waitingSince (run_probe calls it on
// the subArm tag) and before status.js's next 1 s tick, which then computes
// a 21 s wait and shows the sub. app.js's stall detector reads both sides of
// its own subtraction too (and 21 s < its 45 s threshold), so the jump is
// behavior-neutral for it. reset() clears the shift.
const realNow = Date.now.bind(Date);
let clockOffset = 0;
Date.now = () => realNow() + clockOffset;

function shortStack() {
  const raw = String(new Error().stack || "").split("\n").map((s) => s.trim());
  // Frame lines: chromium "at fn (url:ln:col)", WebKit "fn@url:ln:col" (and
  // "global code@..."). Keep both shapes, drop this helper's own frame and
  // WebKit's leading message line. One line, " <- " joined (probe results
  // must never carry raw newlines).
  return raw
    .filter((s) => (/^at /.test(s) || /@/.test(s)) && !s.includes("shortStack"))
    .slice(0, 6)
    .join(" <- ")
    .slice(0, 360);
}

// --- the scrollTop setter spy (who-wrote-scrollTop probe) -------------------
const descr =
  Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollTop") ||
  Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
Object.defineProperty(logEl, "scrollTop", {
  configurable: true,
  get() { return descr.get.call(this); },
  set(v) {
    T.writes.push({
      t: Math.round(performance.now()),
      from: Math.round(descr.get.call(this) * 100) / 100,
      to: Math.round(v * 100) / 100,
      stack: shortStack(),
    });
    descr.set.call(this, v);
  },
});

// --- the pin-edge spy (who flipped pinned? the pill's `hidden` is written
// by exactly one function, setPinned — intercept it with the caller stack).
const hiddenDescr = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "hidden");
Object.defineProperty(pill, "hidden", {
  configurable: true,
  get() { return hiddenDescr.get.call(this); },
  set(v) {
    T.pins.push({
      t: Math.round(performance.now()),
      pinned: !!v, // hidden===pinned: setPinned(p) writes hidden=p
      stack: shortStack(),
    });
    hiddenDescr.set.call(this, v);
  },
});

// --- the status-row writer spies (BK-019 recover family). The recovery
// contract lives in the ORDER of status.js's label writes ("Reconnecting ·
// still working" → "Catching up…" → "Writing…" all land inside ONE
// synchronous handleRunEvent task — a MutationObserver callback can count
// them but never order them), so intercept the writes at the element, like
// the scrollTop spy. status.js assigns labelEl.textContent on EVERY
// show()/phase()/warn() call (and again inside the row-hidden phase()
// branch), so the log is a faithful call trace. #chat-status's `hidden`
// setter is status.js's stop() (guarded by `if (!row.hidden)`) and show()'s
// row-open — one write per REAL toggle, the stop-count witness.
const labelEl = document.querySelector("#chat-status-label");
const statusRow = document.querySelector("#chat-status");
const labelDescr = Object.getOwnPropertyDescriptor(Node.prototype, "textContent");
Object.defineProperty(labelEl, "textContent", {
  configurable: true,
  get() { return labelDescr.get.call(this); },
  set(v) {
    T.labels.push({ t: Math.round(performance.now()), v: String(v) });
    labelDescr.set.call(this, v);
  },
});
const rowHiddenDescr = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "hidden");
Object.defineProperty(statusRow, "hidden", {
  configurable: true,
  get() { return rowHiddenDescr.get.call(this); },
  set(v) {
    T.rowToggles.push({ t: Math.round(performance.now()), hidden: !!v });
    rowHiddenDescr.set.call(this, v);
  },
});

// --- samplers ---------------------------------------------------------------
setInterval(() => {
  T.samples.push({
    t: Math.round(performance.now()),
    top: Math.round(logEl.scrollTop * 100) / 100,
    h: logEl.scrollHeight,
    ch: logEl.clientHeight,
    pinned: isPinned(),
    pill: !pill.hidden,
  });
}, 8);

logEl.addEventListener("scroll", () => {
  T.scrolls.push({ t: Math.round(performance.now()), top: Math.round(logEl.scrollTop * 100) / 100, h: logEl.scrollHeight });
}, { passive: true });

new MutationObserver(() => {
  T.doms.push({ t: Math.round(performance.now()), n: logEl.children.length, top: Math.round(logEl.scrollTop * 100) / 100, h: logEl.scrollHeight });
}).observe(logEl, { childList: true, subtree: true });

// --- the driver API (run_probe.py calls these via page.evaluate) ------------
window.__probe = {
  ready: false,
  mark(name) { T.meta[name] = Math.round(performance.now()); return T.meta[name]; },
  metrics() {
    return {
      t: Math.round(performance.now()),
      top: Math.round(logEl.scrollTop * 100) / 100,
      h: logEl.scrollHeight,
      ch: logEl.clientHeight,
      pinned: isPinned(),
      pill: !pill.hidden,
      children: logEl.children.length,
      mode: document.querySelector("#chat-send").dataset.mode || "send",
      // The in-flow live-turn status row (index.html): hidden at
      // turn_complete, which makes #chat-log this much taller and drops
      // maxTop — the completion boundary's VIEWPORT change (status-clamp).
      statusH: document.querySelector("#chat-status").offsetHeight,
      statusHidden: document.querySelector("#chat-status").hidden,
      label: document.querySelector("#chat-status-label").textContent,
    };
  },
  // The shrink scenarios' strip, read from the live DOM. Pre-D2 the streaming
  // strip's tail was what endThinking() removed (CSS max 3.8em ~= 47.5 px, the
  // shrink the browser clamped against); D2 (BK-030) dropped the tail, so
  // tailH is null now and stripH is the fixed-height row that replaced it.
  tailInfo() {
    const t = logEl.querySelector(".think.streaming .think-tail");
    const strip = logEl.querySelector(".think.streaming");
    const px = (e) => (e ? Math.round(e.getBoundingClientRect().height * 100) / 100 : null);
    return {
      strips: logEl.querySelectorAll(".think").length,
      streaming: logEl.querySelectorAll(".think.streaming").length,
      tailH: px(t),
      stripH: px(strip),
    };
  },
  collect() {
    return JSON.parse(JSON.stringify({
      samples: T.samples, writes: T.writes, scrolls: T.scrolls, doms: T.doms,
      subs: T.subs, pins: T.pins, labels: T.labels, rowToggles: T.rowToggles,
      meta: T.meta,
      doneAt: window.__mock.doneAt,
      runClosed: window.__mock.runClosed,
      final: window.__probe.metrics(),
    }));
  },
  reset() {
    // Drop the transcript + follow state + every run slot, keep the mock's
    // persisted store (the heal scenario reopens a finished session).
    resetTranscript(); // also resetFollow(): pinned=true, spring stopped
    ui.sessionId = null;
    ui.resync = null;
    ui.liveTurn = null;
    ui.liveSeq = {};
    ui.liveDone = {};
    // The run CHROME returns to pristine too: a scenario that ends LIVE (t15b
    // leaves Stop armed on purpose) must not turn the NEXT scenario's
    // submit-click into a STOP-click, and the status row starts hidden.
    // Same fiction as clearing the run slots above — reset is the seam.
    const send = document.querySelector("#chat-send");
    if (send) send.dataset.mode = "send";
    const row = document.querySelector("#chat-status");
    if (row) row.hidden = true;
    clockOffset = 0;
    try { localStorage.removeItem("lastChat"); } catch (_) { /* private mode */ }
    window.__mock.reset();
    T.samples.length = 0; T.writes.length = 0; T.scrolls.length = 0; T.doms.length = 0;
    T.subs.length = 0; T.pins.length = 0; T.labels.length = 0; T.rowToggles.length = 0;
    T.meta = {};
    return window.__probe.metrics();
  },
  following() {
    return typeof followMod.isFollowing === "function"
      ? followMod.isFollowing() : isPinned(); // the ONE gate, read live
  },
  // runstate's FIELDS, peeked for DIAGNOSTIC detail only — the recover checks
  // assert observables (labels, row toggles, composer mode, mock call log,
  // transcript text); this explains a FAIL, it never is one.
  peek() {
    return {
      sessionId: ui.sessionId === undefined ? null : ui.sessionId,
      liveTurn: ui.liveTurn
        ? { session: ui.liveTurn.session === undefined ? null : ui.liveTurn.session,
            detached: ui.liveTurn.detached === true } : null,
      resync: ui.resync ? { id: ui.resync.id, seen: ui.resync.seen === true } : null,
    };
  },
  arm(name, sid) {
    // The pacing switch rides arm(): a missed arm can never leave the reveal
    // off for the NEXT scenario.
    window.__mdPace = name === "shape-mixed" ? "off" : "";
    return window.__mock.arm(name, sid);
  },
  // Jump the probe clock past status.js's 20 s slow-hint threshold (see the
  // Date.now note; run_probe calls it right after the subArm tag).
  shiftClock() { clockOffset = 21000; return true; },
  // The REAL open path (sets ui.sessionId — resyncRun's heal only renders
  // when the session is the displayed one).
  openSession(id) { return openSession(id); },
  // Arm the detached-run recovery exactly as a dead socket would, then the
  // caller delivers run_done through the production events.js sink.
  armResync(id) { ui.resync = { id, seen: true }; ui.liveTurn = { session: id }; return true; },
  deliver(ev) { T.meta.deliverAt = Math.round(performance.now()); window.__sk.handleRunEvent(ev); return true; },
  scrollTop(v) { logEl.scrollTop = v; return logEl.scrollTop; }, // spy positive control
  // ── fake-scroller gate pin (plan criterion 3): drive the SAME production
  // paint paths (dispatchStream → renderDelta, and the stream-end flush)
  // against a seeded transcript — no SSE — to pin that BOTH scroll paths run
  // through the ONE isFollowing() gate: append unfollowed never moves the
  // view, append followed stays pinned at the bottom.
  seedTranscript(n) {
    for (var i = 0; i < n; i++) {
      addUserMsg("Seed " + (i + 1) + " — " + Array(12).join("filler word ") + i);
    }
    return window.__probe.metrics();
  },
  paintDelta(text) { dispatchStream({ type: "delta", text: text }); return true; },
  flush() { finalizeAgentBubble(); return true; },
  // ── Step-3 matrix helpers ──────────────────────────────────────────────────
  // The state the FOLLOWING (b) half of the matrix asserts on the newest
  // answer: the reveal drained (_shown caught up with _raw — a still-paced
  // tail is a half-rendered answer), the rendered text ends with the SAME
  // tail bytes as the raw answer (the shapes' outros end in plain words), and
  // the bubble's bottom edge sits at the log's content bottom (the 14 px of
  // #chat-log padding aside) — "pinned to the newest message", geometrically.
  // The state the FOLLOWING (b) half of the matrix asserts on the newest
  // answer, measured like a READER (post finalizeAgentBubble, post paint
  // settle): the rendered text carries the answer's tail bytes — the whole
  // markdown rendered, fence CLOSED, list and table laid out (a half-shown
  // tail ends with different bytes) — and the newest content's bottom edge
  // sits at the bottom of the VISIBLE viewport: what a reader at the bottom
  // actually demands. The scroller's 14 px bottom padding is deliberately
  // NOT part of the contract (it is design, not correctness); the in-flow
  // #chat-status row, visible while a run is open, is not content.
  bubbleState() {
    const mds = logEl.querySelectorAll(".msg.agent .md");
    const md = mds.length ? mds[mds.length - 1] : null;
    if (!md) return { present: false };
    const raw = md._raw || "";
    const text = md.textContent || "";
    const tail = raw.trimEnd().slice(-8);
    const lb = logEl.getBoundingClientRect();
    let lowest = -Infinity, last = null;
    for (const kid of logEl.children) {
      if (kid.hidden) continue;
      const cs = getComputedStyle(kid);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      const r = kid.getBoundingClientRect();
      if (r.height > 0 && r.bottom > lowest) { lowest = r.bottom; last = kid; }
    }
    // The reader's contract: the newest content's bottom edge AT the
    // viewport's CONTENT bottom — the scroller's own bottom padding and the
    // last row's design margin are not gaps a reader sees, so the anchor is
    // the padding-box edge (lb.bottom is the BORDER box; #chat-log carries
    // 14px of bottom padding). When the exchange is shorter than the
    // viewport there is no content bottom to meet — overflowed=false tells
    // the checker the geometric clause does not apply (top==0, nothing to
    // scroll: the b-pinned-bottom check covers that shape).
    const padB = parseFloat(getComputedStyle(logEl).paddingBottom) || 0;
    const margin = last ? parseFloat(getComputedStyle(last).marginBottom) || 0 : 0;
    return {
      present: true,
      shown: Math.floor(md._shown || 0), rawLen: raw.length,
      tailOk: tail.length === 0 || text.trimEnd().endsWith(tail),
      tail: tail,
      textLen: text.length,
      lastChild: last ? (last.className || last.id || last.tagName) : "-",
      bottomDelta: Math.round(lowest + margin - (lb.bottom - padB)), // ~0 followed end
      overflowed: logEl.scrollHeight > logEl.clientHeight + 1,
    };
  },
  // Anchor residual R1: synthetic content INSERTED ABOVE the parked viewport.
  // The production transcript is append-only (nothing inserts above a reader
  // — that inventory fact is half the verdict); this drives the clamp/anchor
  // question directly: with anchoring, the browser would compensate scrollTop
  // here; with `overflow-anchor: none` it must not move (the text slides).
  insertAbove(px) {
    const d = document.createElement("div");
    d.className = "probe-grow";
    d.style.height = px + "px";
    logEl.insertBefore(d, logEl.firstElementChild);
    return { top: Math.round(logEl.scrollTop * 100) / 100, h: logEl.scrollHeight };
  },
  removeGrow() {
    logEl.querySelectorAll(".probe-grow").forEach((d) => d.remove());
    return logEl.children.length;
  },
  // A/B the shipped rule: set "" restores the stylesheet value (none).
  anchorSet(v) { logEl.style.overflowAnchor = v; return window.__probe.anchorGet(); },
  // shape-mixed runs with the paced reveal OFF: the streamed partial (fence
  // unclosed) must not paint ahead of the finalize render, or the tall wait
  // measures a DOM height the answer never streams to. Set at arm time per
  // scenario (below); restore("") puts the reveal back.
  pacing(mode) { window.__mdPace = mode; return String(window.__mdPace); },
  anchorGet() { return getComputedStyle(logEl).overflowAnchor; },
};

// Instrumented BEFORE the app boots (dynamic import — a static one would
// evaluate app.js before the setter spy existed).
import("./app.js").then(() => {
  setTimeout(() => { window.__probe.ready = true; }, 250);
});
