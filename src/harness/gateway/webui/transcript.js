/* The chat-transcript render engine: bubbles, and dispatchStream — shared by
   the chat SSE and the /api/events renderer. Thinking strips live in
   thinking.js, tool pills in pills.js (LOC-cap splits); their state is
   private there.

   All render state here (chatLog, agentBubble) is module-private: the two
   stream renderers call the SAME exported functions, so they can never hold
   divergent copies of it. External code closes an open pill cluster / thinking
   segment via closeSegments(), never by writing the state directly. */

import { $, api, el } from "./dom.js";
// BK-028: the bubble renders through stream_md.js (settle → renderMd → morph);
// renderMd stays the one marked → DOMPurify path, called there.
import { streamRender } from "./stream_md.js";
// Scrolling is follow.js's job (spring follow, gesture unpin, "↓ Latest" pill).
// Rule: any programmatic scroll AWAY from the bottom calls unpin() first.
// BK-017: isFollowing() is the ONE followed/at-bottom gate, captured BEFORE
// the height change it decides about; keepRead() brackets a mutation so a
// shrink never clamps the reader's position away (see follow.js's header).
import {
  follow, jumpToBottom, resetFollow, isFollowing, keepRead,
  captureRead, restoreRead, unpin,
} from "./follow.js";
// Separate concerns, separate modules — import, never redeclare
// (an unimported sibling call throws AT THE CALL: test_conventions'
// test_webui_modules_import_sibling_exports_they_call).
import { showGreeting, hideGreeting } from "./greeting.js";
import { dropSeamsTo, paintSeam, reloadSeams } from "./compaction.js"; // seam painter + reload seams (LOC-cap split)
// replay.js holds the seq/gap-fill machinery AND the pure replay helpers
// below (the LOC-cap split). It imports nothing from this file (the painter
// and dispatcher are injected), so there is no cycle.
import { clearLiveRun, consumeLiveJoin, flushBuffered, shownCall, stripMemoryBlock } from "./replay.js";
import { thumbRow } from "./thumbs.js";
import { startThinking, appendThinking, endThinking, resetThinking, wireThinking } from "./thinking.js";
// Tool pills (LOC-cap split): the cluster state (pillRow/pendingPill) lives THERE.
import { breakPillRow, resetPills, startToolPill, finishToolPill, addTruncatedChip, wirePills } from "./pills.js";
import { resetClips } from "./showall.js";

const chatLog = $("#chat-log");
let agentBubble = null;   // live narration <div class="md"> being streamed into
let mdRaf = 0;            // paced-reveal loop for the live agent bubble
// ONE render-in-flight state (Step 7; was _replaying + _pendingEvents, a flag
// and its shadow array): null = not replaying (paint straight through);
// an array = renderSession owns the DOM, these events wait for its end.
let _buffer = null;

// Paced reveal: a slow local model delivers text in lumps, so reveal the
// received text at an adaptive rate that keeps the display at most ~0.6s
// behind (faster as the backlog grows, never slower than MIN_CPS).
const REVEAL_LAG_S = 0.6;
const REVEAL_MIN_CPS = 8;

// Passive recovery diagnostic (thinking-block-vanish): armed by the probe or a
// Safari console via `window.__sk._thinkLog = []`. Zero production overhead —
// the array does not exist, so nothing is built or pushed. Read-only over
// render state; same local helper as thinking.js / events.js.
const tlog = (name, state, withStack) => {
  const L = window.__sk && window.__sk._thinkLog;
  if (!L) return;
  // BK-023: entry = [name, state, stack|null, event-time ms]. Stamped AT PUSH —
  // the ?recoverdebug pump drains every 2 s, so drain-time stamps cannot tell a
  // paced stream from a catch-up burst. Stack stays at index 2 (probes read
  // positionally); the null pad keeps the stamp always e[3].
  L.push([name, state, withStack ? new Error().stack : null, Date.now()]);
};

export function resetTranscript() {
  tlog("resetTranscript",
    { children: chatLog.children.length, hadBubble: !!agentBubble, replaying: _buffer !== null }, true);
  resetThinking(); // cancel its paint loop + drop the segment refs (thinking.js)
  clearLiveRun(); // the seq watermark belongs to the DOM it painted (replay.js)
  // The reveal loop outlives a frame now — a stale one on the wiped bubble
  // would also block the next bubble's loop from starting (mdRaf != 0).
  if (mdRaf) { cancelAnimationFrame(mdRaf); mdRaf = 0; }
  chatLog.innerHTML = "";
  agentBubble = null;
  resetPills();
  resetClips(); // the wiped "Show all" bodies leave the shared observer (BK-031)
  resetFollow();
}

// Close any open thinking segment and break the current pill cluster — the
// segment boundary external stream handlers (turn_complete/done/run_done)
// need. Replaces the old direct `pillRow = null` writes from outside.
export function closeSegments() {
  tlog("closeSegments", { replaying: _buffer !== null, queued: _buffer ? _buffer.length : 0 });
  endThinking();
  breakPillRow();
}

// attachments (optional) = [{id, mime, name, size}]: the live send passes
// what it just uploaded, replay the stored rows — thumbs.js renders both.
export function addUserMsg(text, attachments) {
  hideGreeting(true);
  endThinking();
  finalizeAgentBubble();
  breakPillRow();
  const msg = el("div", "msg user", text);
  if (attachments && attachments.length) msg.appendChild(thumbRow(attachments));
  chatLog.appendChild(msg);
  jumpToBottom();
}
export function addErrorMsg(text) {
  // BK-017: the error line must not YANK a reader who scrolled up mid-run to
  // read — "I scrolled up and an error frame grabbed the screen" is the same
  // complaint as the completion snap. The line still lands on screen for a
  // FOLLOWING view (the common case: an error with the composer watched).
  // addUserMsg's jump stays unconditional — sending IS a scroll-to-bottom act.
  const following = isFollowing(); // before any mutation this call performs
  endThinking();
  finalizeAgentBubble();
  breakPillRow();
  chatLog.appendChild(el("div", "msg error", text));
  if (following) jumpToBottom();
}
// A small neutral system line (e.g. "Stopped") — a turn boundary that is
// neither an answer, a warning, nor an error. Gated like addErrorMsg:
// pressing Stop while reading history must not steal the viewport; the
// "↓ Latest" pill is the reader's own route to the line.
export function addSystemMsg(text) {
  const following = isFollowing();
  endThinking();
  finalizeAgentBubble();
  breakPillRow();
  chatLog.appendChild(el("div", "msg system", text));
  if (following) jumpToBottom();
}

function ensureAgentBubble() {
  if (agentBubble) return agentBubble;
  const wrap = el("div", "msg agent");
  const md = el("div", "md");
  md._raw = "";      // everything received
  md._shown = 0;     // float count of revealed chars (paced reveal)
  md._rendered = 0;  // floor(_shown) at the last render
  wrap.appendChild(md);
  chatLog.appendChild(wrap);
  agentBubble = md;
  return md;
}
// The FLUSH: stop the reveal and render everything received. At a segment or
// turn end at most ~REVEAL_LAG_S of backlog appears at once (accepted).
// Session replay sets _raw and finalizes directly, so it is never paced.
export function finalizeAgentBubble() {
  if (mdRaf) { cancelAnimationFrame(mdRaf); mdRaf = 0; }
  const md = agentBubble;
  if (!md) return;
  agentBubble = null;
  const hadBacklog = md._shown < md._raw.length;
  const following = isFollowing(); // BEFORE the render — see follow.js
  // The final render is exactly renderMd(raw) (no settling), morphed over the
  // streamed DOM; it can change height either way (a prefix can lay out
  // taller or shorter than the whole); the bracket keeps an unfollowed view's
  // read position across it, the flush below re-bottoms a following one.
  keepRead(() => { streamRender(md, md._raw, true); });
  if (hadBacklog && following) follow();
}

// Cycle break (BK-019 criterion 5): thinking.js and pills.js import NEITHER
// each other NOR this file — the calls that made them do (startThinking's
// finalize+breakPillRow, startToolPill/addTruncatedChip's endThinking+
// finalize) run through module bindings injected HERE, at module evaluation,
// before any segment can paint (every function passed is a hoisted
// declaration: our own, or theirs, already live when their bodies ran).
// transcript.js is the only module importing all three — the segment graph
// is one-way from here, the replay.js injected-painter pattern above.
// Pin: test_no_import_cycle_between_thinking_and_transcript.
wireThinking({ finalizeAgentBubble, breakPillRow });
wirePills({ endThinking, finalizeAgentBubble });

function renderDelta(t) {
  const md = ensureAgentBubble();
  md._raw += t;
  if (mdRaf) return; // the running loop picks the new text up
  let last = 0;
  const tick = (now) => { // capture md — never the moving global
    const backlog = md._raw.length - md._shown;
    if (backlog <= 0) { mdRaf = 0; return; }
    const dt = last ? Math.min(now - last, 100) : 16.7;
    last = now;
    // Probe switch (window.__mdPace === "off", set by __probe.arm for the
    // shape-mixed scenario): reveal NOTHING mid-stream — the finalize render
    // shows the whole answer at once, the streamed-partial-unclosed-fence
    // state that shape exists to measure. Production never sets it.
    if (window.__mdPace === "off") { mdRaf = requestAnimationFrame(tick); return; }
    const cps = Math.max(backlog / REVEAL_LAG_S, REVEAL_MIN_CPS);
    md._shown = Math.min(md._raw.length, md._shown + cps * dt / 1000);
    let n = Math.floor(md._shown);
    const c = md._raw.charCodeAt(n - 1);
    if (c >= 0xd800 && c <= 0xdbff) n--; // never split a surrogate pair (emoji)
    if (n !== md._rendered) {
      const following = isFollowing(); // BEFORE the render — see follow.js
      md._rendered = n;
      // A reveal step re-renders a PREFIX (settled, then morphed: unchanged
      // blocks keep their nodes); markdown can still lay out shorter than the
      // previous frame, so even this growth-by-default write belongs in the
      // read-preservation bracket.
      keepRead(() => { streamRender(md, md._raw.slice(0, n)); });
      if (following) follow();
    }
    mdRaf = requestAnimationFrame(tick);
  };
  mdRaf = requestAnimationFrame(tick);
}

// Shared by chat SSE and background /api/events. Grouping rules: consecutive
// tool_calls cluster into one pill row; thinking or answer text closes the
// row; a tool call or answer text closes an open thinking segment (so a
// dropped thinking_done can never leave a zombie shimmer).
// While a session replay is in flight, events are BUFFERED (they would
// otherwise paint above the not-yet-replayed history and race the replay's
// pill state); renderSession flushes the queue once the replay is down.
export function dispatchStream(ev) {
  if (_buffer) { _buffer.push(ev); return; }
  paintStream(ev);
}

// The raw paint — no replay buffer. Gap-fill and the cold-open tail ARE the
// replay: they must paint through, not queue behind themselves (replay.js).
export function paintStream(ev) {
  // One shared gate for BOTH scroll paths (BK-017 criterion 3): captured
  // before the paint, it decides the trailing follow() — the same predicate
  // finalizeAgentBubble and renderSession use, no second threshold.
  const following = isFollowing();
  if (ev.type === "delta") { endThinking(); breakPillRow(); renderDelta(ev.text); }
  else if (ev.type === "thinking_delta") appendThinking(ev.text);
  else if (ev.type === "thinking_done") endThinking();
  else if (ev.type === "tool_call") startToolPill(ev.tool, ev.args);
  else if (ev.type === "tool_result") finishToolPill(ev.is_error, ev.digest, ev.chars);
  else if (ev.type === "truncated") addTruncatedChip();
  // closeSegments first: the seam is a hard transcript boundary (compaction.js owns the position)
  else if (ev.type === "compaction") { closeSegments(); paintSeam(ev); }
  if (following) follow(); // no-op unless pinned; the spring targets the live bottom
}

/* ---- session replay ---- (stripMemoryBlock/shownCall live in replay.js) */

// Re-entrancy guard: the fetch is async, so two overlapping calls both await
// and, without this, BOTH append — every message painted twice. The newest
// call owns the DOM AND the buffer: a late older one must not release it, or
// it would unlock the screen while the newest replay is still in flight.
let renderSeq = 0;

// True while a replay owns the DOM — a gap-fill must not paint over one. Same
// question _buffer answers internally (null = not replaying), stated outward.
export function isReplaying() { return _buffer !== null; }

// BK-034 screen-swap hook: runstate's T30 observeLeave (the screen moves to
// `id`) at entry and T31 observeJoin (the swap's live join) after it, applied
// on events.js's fx map. INJECTED by events.js's module body — this module
// imports no router. Unwired (a bare scaffold without events.js): skipped.
let screenHook = null;
export function wireScreen(hook) { screenHook = hook; }

// The ONE resync entry point's paint side. `recovering` replaces the OLD
// AMBIENT discard flag — a module-level boolean with three separate arm
// sites, which is exactly how the stale-tail class came back (the
// thinking-block-vanish, 2026-10-17: events rerouted
// during a recovery fetch are the just-completed turn's tail, and flushing
// them repaints it over the authoritative replay — zombie thinking strips,
// tail bubbles). It is now a PARAMETER: the caller states the render's purpose
// at the call, so there is nothing to consume synchronously at entry and
// nothing a superseded render can leak into a later NORMAL render. Every
// resync path arrives through runstate.js's resyncVerdict() (T27), the single
// entry point, whose heal/join effects the transports bind to the renders
// below (BK-019 Step 4).
// `preserveRead` is the same stated-at-the-call pattern for SCROLL (BK-017):
// this render re-displays the transcript the user may be READING (the
// run_done heal, a cold join) rather than OPENING one, so the read position
// survives the swap. It cannot be inferred from ui.sessionId — EVERY caller
// sets it before calling; the two fx-map call sites (rerenderHeal/rerenderCold
// in app.js and events.js) state it explicitly.
// A following view still lands at the bottom (that's what following means).
// "Hard reload fixes it" = a recovery STATE bug, never platform: reload and recovery
// both land here, so the fault is stale module state + frozen DOM — fix the orchestration.
export async function renderSession(id, recovering = false, preserveRead = false) {
  const seq = ++renderSeq;
  tlog("renderSession", { id: id || null, seq, recovering });
  _buffer = []; // events queued for an older replay die with its DOM
  try {
    screenHook?.leave(id || null); // inside the try: a throw must still release _buffer
    if (!id) { resetTranscript(); showGreeting(); return; } // sync — no blank frame
    // Step 4 shape {messages, live}; a bare array is an older server/mock.
    const data = await api(`/api/sessions/${id}/messages`).catch(() => []);
    const messages = Array.isArray(data) ? data : data.messages || [];
    const live = Array.isArray(data) ? null : data.live || null;
    if (seq !== renderSeq) return; // a newer renderSession owns the DOM + flag now
    // Atomic swap: wipe + repaint in ONE synchronous block — no await between
    // resetTranscript() and the replay loop, so the browser never gets a
    // checkpoint at which #chat-log is empty (the iOS resume flash). The old
    // transcript stays on screen until the fetched one replaces it; events
    // arriving meanwhile are buffered by _buffer as before.
    // BK-017: the read position is captured HERE, after the fetch (the user
    // can scroll during it) and before the wipe; resetFollow() inside will
    // claim pinned=true — the restore at the end of this same block undoes
    // that claim for a reader (unpin(), so the pill and the spring state are
    // honest again too), still inside the swap so nothing paints the jump.
    const read = preserveRead ? captureRead() : null;
    resetTranscript();
    hideGreeting();
    const resultById = {}; reloadSeams(data); // Step 5: arm anchored reload seams (compaction.js drains them)
    for (const m of messages) for (const r of m.tool_results || []) resultById[r.tool_call_id] = r;
    for (const m of messages) { dropSeamsTo(m.idx);
      if (m.role === "user") {
        const text = m.text || "", atts = m.attachments || [];
        // A cron run's prompt ("[Scheduled run · …]") shows like any user turn
        // (2026-11-01: was skipped only when no memory block preceded it).
        if (text || atts.length) addUserMsg(stripMemoryBlock(text), atts); // photo-only turns have no text
      } else if (m.role === "assistant") {
        if (m.content_blocks && m.content_blocks.length) {
          // Exclusivity rule: content_blocks is the ordered raw record (one
          // message per API call), so render ONLY from it — m.text/m.tool_calls
          // are derived duplicates. Unknown block types (redacted_thinking, …)
          // are never rendered.
          for (const b of m.content_blocks) {
            if (b.type === "thinking") {
              if (b.thinking) {
                startThinking();
                appendThinking(b.thinking);
                endThinking({ replay: true });
              }
            } else if (b.type === "text") {
              if (b.text) {
                endThinking();
                breakPillRow();
                const md = ensureAgentBubble();
                md._raw = b.text;
                finalizeAgentBubble();
              }
            } else if (b.type === "tool_use") {
              const pill = startToolPill(...shownCall(b.name, b.input || {}));
              const r = resultById[b.id];
              // Pass THIS row's pill explicitly (startToolPill's return) —
              // never let the shared global decide which pill the result is for.
              finishToolPill(r ? r.is_error : false, r ? r.content : "(no result recorded)", null, pill);
            }
          }
        } else {
          // Legacy rows persisted before extended thinking.
          if (m.text) {
            endThinking();
            breakPillRow();
            const md = ensureAgentBubble();
            md._raw = m.text;
            finalizeAgentBubble();
          }
          for (const tc of m.tool_calls || []) {
            const pill = startToolPill(...shownCall(tc.name, tc.arguments));
            const r = resultById[tc.id];
            finishToolPill(r ? r.is_error : false, r ? r.content : "(no result recorded)", null, pill);
          }
        }
      }
    }
    endThinking(); // persisted rows are history: nothing they paint stays open
    // Step 6: the unpersisted tail joins the swap — painted onto the fresh
    // DOM through the live painter (deltas stay paced; no finalize), with no
    // re-render and no second fetch; it may legitimately leave a thinking
    // strip open (the run is still thinking).
    consumeLiveJoin(id, live, paintStream);
    // The swap's exit (BK-017, mechanism (a)): a following view jumps (that IS
    // following); a reader goes through unpin() — which stopLoop()s a spring
    // consumeLiveJoin may have armed and re-shows the pill — then the
    // two-branch restore. Still the SAME synchronous block, so the iOS rule
    // above holds: the browser never paints the interim jump.
    if (read && !read.following) {
      unpin();
      restoreRead(read);
    } else {
      jumpToBottom();
    }
    // A bare-array body (fetch failed / pre-Step-4 server) says nothing about
    // liveness: undefined, not null — T31 changes nothing on an unknown.
    screenHook?.join(id, Array.isArray(data) ? undefined : live, data.bg_live);
  } finally {
    // Replay over (however it ended — the newest call always reaches here):
    // release the buffer, then events that arrived during the fetch either
    // flush onto the tail of the replayed transcript (normal open) or are
    // discarded as stale (recovery render — the replay already has them).
    if (seq === renderSeq) {
      const queued = _buffer;
      _buffer = null; // released BEFORE dispatching: the flush paints, it must not re-queue
      if (recovering) {
        // Recovery render: these events are the just-completed turn's rerouted
        // tail — the replay above already painted all of it. Dispatching them
        // now would undo the authoritative DOM (the thinking-strip vanish).
        // Clear the buffer WITHOUT dispatching (normal opens flush below).
        tlog("renderSession:flush", { seq, queued: queued.length, discarded: true });
      } else {
        tlog("renderSession:flush", { seq, queued: queued.length });
        // Flushed events reach THIS replayed DOM: mark them for the seq
        // dedupe, but skip anything the /messages live join above painted
        // (join-then-flush double-paints a rerouted event that sat in both).
        flushBuffered(id, queued, dispatchStream);
      }
    }
  }
}
