# BK-017 scroll-follow repro probe (plan bk-017-scroll-follow, Steps 1–3)

Deterministic repro + mechanism discriminator for "the chat snaps to the
bottom when a streamed answer finishes". Runs the REAL webui modules over a
mirrored site with a mocked `/api/*` fetch — no daemon, no token, no model,
nothing ever POSTs to a live gateway.

    python3 tests/fixtures/scroll_follow/run_probe.py

Optional filters: `--only=mechanisms,matrix,guards,anchors,recover` (families),
`--engines=chromium[,webkit]`, `--ports=desktop[,iphone]` — check lines are
prefixed `[engine/port]`. A full run is ~16–18 min of REAL wall time.

`python3` = the system interpreter (it has the Playwright package; the uv
venv deliberately does not — nothing was added to the project). The cached
`chrome-headless-shell` from `~/Library/Caches/ms-playwright/` is launched
via `executable_path`. REAL wall time only: the reveal pacing and the
follow spring are rAF-driven, and rAF never ticks under
`--virtual-time-budget` (learnings 2026-10-16).

Exit codes (pwa_resume precedent): `0` every check green · `1` probe ran,
a check failed · `2` harness broke (no playwright, jsc parse-check, no
cached chrome, the app never booted, a scenario timed out — printed with a
console/state diagnostic dump), or a STRUCTURAL guard broke: an engine
contributed ZERO rows without a launch skip (`silent_engine_problem`, any
scope — planted and reproduced 2026-10-30), the two engines' row SETS diverge
(`engine_set_problem`), or a full run's engine fell under
`MIN_UNIQUE_PER_ENGINE` (`coverage_problem`) — a results block that silently
ran less than it claims must never read as green. The floor speaks on full
runs only (no `--only`/`--ports` filter); the zero-row and set-identity
guards always.

## What Step 1 established (run green = these facts still hold)

Every scenario runs on BOTH engines — cached chromium and WebKit
(`python3 -m playwright install webkit`; a missing WebKit degrades to a
recorded SKIP, never a FAIL) — and every check name carries an
`[engine/port]` prefix (Step 3; Steps 1–2 runs were desktop-only).

**The DONE frame does not snap at completion — for a view parked far from
the bottom, on both engines.** With a real wheel scroll-up mid-stream, the
live-SSE completion moves nothing: `handleChatEvent`'s done path makes no
scroll call while unpinned (`follow()` early-returns, follow.js:77-78), and
`scrollHeight` does not shrink at done — measured **+77 px** (the last of the
paced reveal flushing), i.e. no clamp has anything to bite on. Checks
`live-path-flat`, `live-path-flat-list`, `no-done-shrink` pin exactly that,
per engine.

**What the wheel-unpinned trigger does NOT cover:** the wheel lands when the
log is ~300 px tall, so that parked view ends up **~1850 px above the bottom**
by `done` — no clamp or yank is physically possible from there. The board's
shape is "reading the *last rows*"; the `shrink-*` scenarios below park the
view 4 px above the bottom instead.

So a flat trigger says the DONE frame itself scrolls nothing while unpinned —
it does not say nothing snaps near completion. The named mechanisms that DO
snap at completion:

* **(a) the resync heal.** `run_done` arriving while `ui.resync` is armed
  (dead socket → detach, backgrounded tab, stall-abort) routes
  `events.js resyncRun` → `transcript.renderSession`, whose final
  `jumpToBottom()` is UNCONDITIONAL (transcript.js, end of `renderSession`).
  Scenario `heal`: reopen a finished session, wheel up 600 px, deliver
  `run_done` through the production `window.__sk.handleRunEvent` sink →
  the viewport lands exactly at the new bottom, `pinned=true`, with
  `jumpToBottom` in the setter-spy stack (`mech-a-heal-jumps`).
* **(c) the stale `pinned`.** follow.js unpins on `wheel`/`touchstart`
  ONLY (follow.js:138-147). A KEYBOARD scroll-up (think-head focused,
  PageUp) moves the viewport while `pinned` stays true, and the spring
  (`frame`, follow.js:71) keeps writing scrollTop through it — the reader's
  position is lost continuously, not in one done-frame event (measured:
  top 22 → **1901** by `topAtDone`, then 2018 — same shape on WebKit
  (1884 → 2001); the `repro-c-snap-at-done` detail prints the numbers).
  Scenario `keyboard`:
  `mech-c-stale-pinned`, `mech-c-spring-writes`, `repro-c-snap-at-done`.
  Scrollbar drags are the same event shape (no wheel, no touch) but are
  not drivable headlessly — the keyboard is their stand-in.

**Candidate (b) — CONFIRMED as a completion-adjacent clamp, on both engines.**
The board's literal shape (the finished render coming out SHORTER than the
streamed partial, so the clamp bites at `done`) is not reached by any shape
this probe can build: `paintStream` re-renders PREFIXES of one growing `_raw`
string and `finalizeAgentBubble` renders that same `_raw`, so the done frame
re-parses MORE of one message — measured **+77 px**, `no-done-shrink`, both
engines. That is a measurement over this probe's answer shapes (table, list,
prose, short), not a theorem about every markdown input: a prefix that renders
taller than the same text completed (a partial row that becomes a table)
would need its own scenario. What this app really shrinks near completion is
the thinking strip collapsing: `endThinking()` drops the 600-char
tail (≈47.5 px) at `thinking_done` or at the first text `delta`, and
`closeSegments()` finishes the job at `turn_complete` — never at `done`.
Three scenarios park the view by fine REAL-wheel steps 4 px above the bottom
(UNPINNED, no JS write), tag the collapse frame at its SSE EMIT instant, and
measure the 8 ms sampler against the setter spy:

| scenario | shrink lands at | dH | top move (no writes) |
|---|---|---|---|
| `shrink-close` | `thinking_done` (gateway-normal close) | −47 px | −43 px, 616→573 = the NEW maxTop |
| `shrink-delta` | first text `delta` (gateway dropped `thinking_done`) | −19 px (tail −47.5, bubble label +27 same frame) | −15 px |
| `shrink-turn` | `turn_complete` → `closeSegments()` (strip still open at end) | −47 px | −71 px (the close reflows two rows in one frame) |

`shrink-*-clamp-signature` and `shrink-*-snapped-to-new-bottom` pin it per
engine (`[chromium]` AND `[webkit]`): the top moves to the new maxTop with
ZERO scrollTop-setter writes while `pinned` stays false and the pill stays
visible — the browser clamping, the app not scrolling. A reader 4 px off the
bottom silently loses their line. On WebKit this is the only
completion-adjacent movement that needs no scroll call at all (nothing in the
app scrolls an unpinned view there), so a fix that only gates scroll calls
would leave it in place.

**The completion boundary also shrinks the VIEWPORT — `#chat-status`, both
engines.** `app.js`'s `turn_complete` handler calls `statusStop()`
(`app.js:259-263`), which hides `#chat-status` — the IN-FLOW row between the
pill and the composer (`index.html`; `chat.css` reserves its space). Its 28 px
leaves the layout, so `#chat-log`'s clientHeight grows (chromium 464→492,
WebKit 481→509) and **maxTop drops 28 px while `scrollHeight` is unchanged**
(933 px both sides). A view parked UNPINNED 4 px above the bottom is clamped
down **24 px** with ZERO setter writes, `pinned` false, pill visible, on both
engines (the sample lands 4–10 ms after the tagged frame):
`status-clamp-parked-unpinned`, `-signature`, `-snapped-to-new-bottom`,
`-done-flat`. It needs no thinking segment and no shrinkable markdown — every
overflowing answer gets it — and it is invisible to any compensation keyed on
`scrollHeight`. It also explains the `gapAtDone=0` rows in the `shrink-*`
scenarios on WebKit: the hide lowers maxTop below a view that stayed parked —
a clamp, not a carry.

**The engine split (chromium only): the carry-repin.** After the clamp the
view sits exactly AT maxTop. Chromium's bottom-edge scroll anchoring then
CARRIES the view down to each new maxTop as the post-thinking text regrows —
again with NO setter write — and follow.js reads that write-free downward
move as user intent: `maybeRepin` (REPIN_GAP 40; the gap is 0) re-pins, the
spring takes over (its first write moves 0.32 px — the browser had already
put the view where the spring aimed), and `done` then legitimately follows.
WebKit does NOT carry: in `shrink-close` the view stays where the clamp left
it, unpinned, ending 23 px above the bottom at `done` (in `shrink-delta` /
`shrink-turn` the later status-row clamp lowers maxTop past the parked view,
so those rows end at the new bottom with no carry at all — see the viewport
paragraph above). `shrink-*-carry-repin-attribution`
pins the attribution (every post-drop write is a spring `frame()`; a
`snap`/`jumpToBottom` carrier fails it), and `shrink-*-done-flat` pins that
the DONE frame still initiates no scroll of its own on any path
(no `jumpToBottom`, no shrink at `done`, both engines).

`control` (same length, plain prose, no thinking strip) pins the flat end of
the plain shape. `follow` (the table answer, never scrolled) is the real
following guard: the log overflows by >1000 px, the pill never appears and the
spring settles on the new bottom. `short` states only what it can prove — a
reply shorter than the log never scrolls (`short-reply-fits`); the old
"guard-follow-pinned" name sat on a `maxTop == 0` tautology.

## Files

* `run_probe.py` — builds `scratch/scroll_follow_probe/site` (symlink
  mirror of the webui, `?v=`-tolerant entry-tag swap → `probe.html`),
  serves it over loopback HTTP (ES modules reject `file://`), runs the jsc
  parse gate, drives every scenario on both engines and both ports through
  the real form/wheel/keys/touch (`--only/--engines/--ports` filters),
  prints `TELEMETRY`/`SHRINK` tables (incl. every setter-spy WRITE with its
  stack) + PASS/FAIL lines, screenshots to `scratch/`.
* `mock_fetch.js` — the `/api/*` mock: scripted SSE frames on a
  wall-clock schedule with EMIT-time tag recording (`tagAt` — the shrink
  windows anchor on the frame's resolve tick), the four clamp scenarios
  (`shrink-close/delta/turn` strip collapses + `status-clamp`'s viewport
  change; helper comments explain each close path), the two stop scenarios
  (`stop-stopped`/`stop-error`: last prose delta tagged `preTerm`, terminal
  frame stamps `doneAt`) and `status-sub` (status sub-row hide timed so the
  `turn_complete` ROW HIDE lands on the restored read gap),
  run events stamped `{seq, run_id}` like `chat_run.py`, and store timing
  honest (the assistant row appears when the call RETURNS, not at the
  first delta).
  Step 3 reuses Step 1's scripts for the matrix (`trigger`, `trigger-list`,
  `control`) and added `shape-code`, `shape-mixed` (with a ~1.4 s seam before
  its table block), `short-plain`, `guard-send` (the send-jump guard's
  answer) and `gap0` (word-dripped tail + tagged static window for the
  anchor residuals).
  A new turn APPENDs to a session's stored rows — replacing them hides the
  history from `openSession()` and silently starves the state-(a) scenarios
  of scroll room (pinned by `tests/test_scroll_follow_probe.py`).
  Also the EventSource and serviceWorker stubs (header
  comment explains each).
* `probe_scroll.js` — telemetry instrument (`window.__probe`): a
  `#chat-log.scrollTop` setter spy with call stacks (engine-tolerant —
  chromium and WebKit render stacks differently, `shortStack()` names the
  frame either way), a `#chat-jump.hidden` setter spy (Step 3: every
  `setPinned` edge WITH its caller stack — the one instrument that can name
  who re-pinned a reader), an 8 ms sampler (top/height/clientHeight/pinned/pill),
  scroll + MutationObserver event logs (incl. `#chat-status` sub-row
  attribute subs, and a `Date.now` shim + `shiftClock()` so the probe can
  move the clock BETWEEN arming and the next tick); scenario drivers
  `seedTranscript`/`paintDelta`/`flush` for the fake-scroller gate pin;
  `bubbleState()` (the full-render contract read off the DOM: newest row's
  bottom edge at the scroller's CONTENT bottom — border-box minus its own
  `paddingBottom` — plus `overflowed` for the shorter-than-viewport case);
  `insertAbove`/`removeGrow`/`anchorSet` for the anchor residuals;
  `arm()` carries the `window.__mdPace === "off"` reveal switch (see the
  src note under the Step-3 section); boots the real `app.js` after
  instrumenting.
* `jsc_check.js` — parse gate for every module the page loads (no node on
  this machine; JavaScriptCore `checkModuleSyntax`).

## Contract after the fix (Step 2, flipped 2026-10-29)

The fix shipped in the shapes Step 1 predicted. In the fixture's own words —
what each check now proves:

* **ONE shared gate** (`follow.js`): `isFollowing()` = `pinned && gap <
  REPIN_GAP`, captured BEFORE the height change it decides about. Every
  scroll path calls it at entry: `renderDelta`/`finalizeAgentBubble`/
  `paintStream` (transcript), the gated `addSystemMsg`/`addErrorMsg` (the
  Stopped/error line no longer yanks a reader — `addUserMsg`'s jump stays
  unconditional: sending IS a scroll-to-bottom act), and both `events.js`
  re-display renders via the `preserveRead` argument stated at the call.
  `gate-followed`/`gate-unpinned` pin that the SAME predicate governs both
  paint paths: the followed view tracks a seeded paint to the new bottom;
  the unfollowed one moves ZERO px with ZERO writes across delta ticks AND
  the finalize flush.
* **Read-position preservation, not "compensation on completion"**:
  `keepRead(fn)` brackets every mutation that can SHRINK (the three segment
  collapses, the `#chat-status` hide, prefix re-renders of a growing one —
  a markdown prefix can lay out SHORTER); `captureRead()`/`restoreRead()`
  bracket `renderSession`'s atomic swap (two-branch restore: keep `top`
  while ≤ new maxTop, else restore the pre-change gap). The shrink checks
  assert the COMPENSATED shape: one named write in the collapse frame, the
  parked gap held to a FLOOR (`gap_drop >= parked − 2.5` — a regrowth under
  a parked view legitimately widens it), `pinned` stays false.
* **`#chat-log { overflow-anchor: none }`** (chat.css) deletes chromium's
  write-free bottom-edge CARRY — `shrink-*-carry-repin-attribution` is
  green with `repin=none` on both engines — but NOT the clamps: the JS
  bracket is still required on both engines (probe-proven both ways).
* **Keyboard/scrollbar unpin = direction + write attribution** (fix (c),
  amendment to the gesture-only pin in `.claude/decisions.md`): an upward
  move is user intent unless our own last write explains it (|top −
  lastWritten| ≤ 2) or it lands at gap ≤ 1 (clamps land AT maxTop). The
  spring's `frame()` divergence branch runs the same test BEFORE re-seeding
  — otherwise the re-seed attributes the user's own move as ours.
  `mech-c-keyboard-*`: PageUp unpins (~150 px up), the spring writes
  NOTHING after, the view stays flat through done.
* **The heal renders in place**: `mech-a-heal-preserves` — a reader parked
  600 px up stays there through the `run_done` heal (one `restoreRead`
  write, `pinned` false, pill shown); a following view still jumps.
* **`status-sub` (new)**: the status sub-row toggles are attribute-guarded
  so the subs log stays sparse. `subHide` is a zero-growth `thinking_done`
  so the `turn_complete` ROW HIDE lands on the restored ~4 px gap — where
  the clamp deterministically bites and `keepRead` catches it. SHOW/HIDE
  windows around the viewport shrink are harmless pins: a SHRINK raises
  maxTop, so clamping is arithmetically impossible there. The clock shift
  (`shiftClock()`) fires on the status-LABEL flip — the signature that the
  app PROCESSED `model_wait` — never on `tagAt`'s emit stamp (the mock
  emits a frame before the app runs it; a constant `Date.now` shift is
  invisible to elapsed-time subtraction unless it lands between arming and
  the next tick).
* **`stop-stopped`/`stop-error` (new)**: Stop (or an error frame) while
  scrolled up → the line lands but the view stays flat (gap ~420 px, zero
  writes during the reveal). The wheel happens at mock tag `preTerm`: the
  paced reveal keeps the log under any tall threshold until the terminal
  frame's own flush.

Unchanged guards (green before AND after, untouched): `guard-follow-pinned`
(followed answer settles at the bottom, pill never shown), `live-path-flat*`,
`control-plain-flat`, every `*-done-flat`, `*-parked-unpinned`,
`short-reply-fits`, `status-clamp-unpinned`, the `instrument` pair, and the
pill contract (visible when unfollowed, tap = `jumpToBottom`, hidden when
following).

**`guard-heal-following` (new guard, added in review)**: the `heal` production
path with NO gesture — a follower must land at the bottom of the HEALED
transcript, not stay at the stale partial's bottom (`preserveRead` is a
reader's rule, not "always preserve"; wizard Q3.6). Green before and after.

**`follow-shrink` (new guard, added in review)**: the `shrink-close` shape with
the view FOLLOWING (`scenario_shrink(park=False)`: the spring must have settled
at the bottom before the tagged collapse). Fix (c)'s risk is the mirror of its
job — a direction rule that reads the CLAMP as the user's upward move would
unpin a follower at every segment collapse, which `guard-follow-pinned` cannot
see (at its collapse the log is not yet scrollable, so nothing clamps). Green
pre-fix too (no unpin rule existed), so it is a guard, not a flipped check: it
reddens only if the rule loosens to "any upward scroll event unpins" — planted
2026-10-29, the naive rule turned it red and left the rest of the matrix green.

**Red discipline**: with the src fix stashed, every flipped check and every
new-scenario check ran RED on both engines for its named pre-fix reason
(heal → bottom, spring yanks after PageUp, carry-repin cascade, the +24 px
status clamp, non-flat stop reveals); every guard above stayed green.

## Acceptance matrix, guards, and the anchor verdict (Step 3, 2026-10-29)

**Families.** The run now has four families (`--only=` selects any subset):
`mechanisms` (the Step-1/2 diagnosis, desktop), `matrix` (below), `guards`
(the legitimate scroll-to-bottom paths must STILL jump), `anchors` (the two
`overflow-anchor` residual measurements). Matrix and guards run per
(engine, port): desktop (1000×680) and the iPhone port (390×844, `hasTouch`
both engines; `is_mobile` is a Chromium-only emulation — WebKit runs the
same viewport without the UA/DPR lie).

**The matrix** — six answer shapes × two states × four engine/port pairs,
all through the real flow (form submit → mocked SSE → done):

| shape | script | state (a) gesture lands… |
|---|---|---|
| table | `trigger` | mid-stream, on the streaming view |
| list | `trigger-list` | mid-stream |
| prose | `control` | mid-stream (the history preamble IS prose-b) |
| code | `shape-code` | mid-stream (fence re-layout at done) |
| mixed | `shape-mixed` | over the history, during the table-block seam |
| short | `short-plain` | over the history while the short reply is still to come |

`short`'s (b) state runs over the SAME history session — a follower at the
bottom of an overflowing transcript, carried down to the short answer's newest
bubble. A fresh-session short-b proves nothing: `maxTop` stays 0, so "at the
bottom" is free (the vacuity trap Step 1 hit twice). `mixed`'s (b) state is a
fresh-session follower whose whole answer lands in the finalize render (the
reveal is off for this shape). Every matrix cell files a screenshot of its
end state to the probe work dir:
`matrix-<shape>-<a|b>-<engine>-<port>.png`.

* **(a) unfollowed**: a REAL gesture unpins mid-stream — wheel on desktop,
  a `touchstart` tap + CDP `Input.dispatchTouchEvent` drag on
  chromium/iphone; WebKit cannot touch-DRAG through Playwright (no drag API,
  no CDP backend), so there the tap proves the unpin rule and a hovered
  wheel moves the position — recorded in the check's `gesture=` field. The
  gesture ENDS away from the bottom (returning to the bottom re-pins by
  app rule — `maybeRepin` — and the a-state measures the unpinned reader).
  Then at done: Δ scrollTop ≤ 2 px, zero non-compensation writes, pill
  visible, `pinned` false, gap held ≥ 100 px, and a real pill tap lands the
  view at the newest message (`matrix-*-a-unpinned-flat`, `*-a-pilltap`).
* **(b) followed**: pinned through every sample, pill never shown, ends at
  maxTop, and the FULL-RENDER contract reads off the DOM: the answer's tail
  bytes rendered (`tailOk`) and the newest row's bottom edge AT the
  scroller's content bottom ±2 px (`*-b-pinned-bottom`, `*-b-full-render`).
  (A shape that does not overflow the log carries `overflowed=false` and the
  geometric clause is skipped — no matrix cell needs that branch now: all
  six b-cells overflow, which is why `short`'s b-state runs over the
  history session.)

`mixed` runs with the paced reveal OFF — the one src edit of Step 3: a
`window.__mdPace === "off"` switch in `transcript.js`'s tick loop, set by
`__probe.arm` only for that scenario, production never sets it. It exists so
the whole table block lands in the FINALIZE render (the streamed-partial
unclosed-fence state the shape measures); with the reveal on, the mid-reveal
wheel raced `follow()`'s snap branch (+1167 px, measured). Consequence:
mixed's fresh session has NO scroll room mid-stream, and the tall-height
gate cannot be port-honest (the iPhone viewport swallows the code shape
whole — its gate satisfied 12 ms AFTER done once; measured). So mixed-a runs
over the history transcript (`scenario_mixed_over_history`), and the touch
scenarios gate on `wait_reader_room` (streamed chars + actual overflow), not
a fixed height.

**Guards** (per engine/port): `guard-send-jumps` (sending still jumps —
`addUserMsg`'s unconditional jump), `guard-open-jumps` (`openSession()` call
site), `guard-picker-jumps` (the `#session-select` change handler —
sessions.js's OTHER `renderSession` caller, driven through the real picker).
The heal-follower and the stopped/error pair live in the mechanism suite
(`guard-heal-following`, `stop-*-flat`).

**ANCHOR VERDICT: KEEP `overflow-anchor: none`.** Measured on both engines
(anchors family, `--only=anchors`):

* **R1 growth-above** (content inserted ABOVE a parked reader): with `none`
  the view does not move — `dTop=+0.0 px`, 0 writes, the text slides under
  the reader (correct: the transcript is append-only, but the rule is what
  makes insert-above safe). With `auto`: `dTop=+240 px` — the browser
  silently relocates the read position.
* **R2 gap-0 stray growth** (parked AT the bottom, unpinned, word-dripped
  `gap0` tail): with `none`, `dTop=+0 px`, gap 0→240, `pinnedEnd=False`,
  pill still in hand. With `auto`, the write-free +240 carry lands the view
  at the new bottom and **`maybeRepin` reads the browser's carry as user
  intent — `pinnedEnd=True`, the reader is yanked into follower mode they
  never asked for** (real user scrolls re-pin on real scrolls; a browser
  compensation is not one). Step 1's "WebKit does not carry" was a
  bottom-edge-growth-only observation: at gap-0 WebKit DOES carry —
  `anchor-auto-gap0-carries` is a real check on both engines, and both
  carry the same verdict.

**Red discipline** (chromium/desktop matrix, sabotage runs, re-measured
2026-10-29 on the review pass): unconditional `jumpToBottom` at finalize (the
pre-BK-017 disease) → all 12 state-(a) lines red (six shapes × both checks),
(b) green. Completion-path `follow()` removed everywhere (finalize + tick +
delta tail) → all 12 (b) lines red (six shapes × `*-b-pinned-bottom` and
`*-b-full-render`), (a) green. 120-char half-render at finalize → all six
`*-b-full-render` red (`tailOk=False`), plus every `*-b-pinned-bottom` and the
(a) rows where the shrunk content clamps the parked view (19 lines).
`restoreRead` disabled leaves the whole matrix GREEN — a coverage boundary of
this family, not a gap in the step: the matrix's (a) cells park ≥100 px up and
their completion vector only grows, so no browser clamp fires there; the
clamp class is the mechanisms family's (the `shrink-*` / `status-clamp`
brackets, Step 2). All sabotages restored; `git diff src/` shows only the
5-line probe switch.

**Residuals (honest limits)**: composer-side `clientHeight` drift while
UNPINNED stays unproven headlessly (the `#chat-log` ResizeObserver net was
rejected in Step 2 — mutation sites are bracketed instead). A physical
iPhone (real fling momentum, real safe-area viewport) is outside any
headless run: the CDP drag has no inertia, so post-touch glide is not
exercised here. WebKit's unpin is proven by `touchstart` (the rule), not by
a drag (the API does not exist in Playwright). WebKit delivers a wheel to
whatever sits under the pointer: one matrix run's mixed-a wheel unpinned the
log but moved it only 3 px, so `scroll_up_gesture` retries a gesture that
did not move the view (the cell's own `gap` assertion still decides — a
gesture that never lands is a FAIL). `is_mobile` emulation is chromium-only
by design.

## BK-029: any press unpins + D1 (guards family, `run_touch_rules`, 2026-10-31)

The user's rule ("any tapping of screen to stop the following of streaming"
— user control): ANY press in `#chat-log` unpins when the log can scroll —
`touchstart` for a finger (2026-10-13's rule, reaffirmed), `pointerdown` for
mouse and pen. D1: opening a pill drawer or thinking body while a run is
live on screen (`runstate.screenRunLive`) pauses following too — the
keyboard's path, which no press sees. A tap-hold + 10 px slop design was
built and reversed. Over `touchrules` (overflowing log, then a streaming
thinking strip, a slow tool pill, a word drip) and `trshort` (a dripped
reply that never overflows), each on a fresh chat:
`touch-tap-unpins` (iPhone) / `mouse-click-unpins` (desktop) — a real press
on text: unpinned + "↓ Latest" at once, no pinned sample while the answer
grows below; `latest-repins`; `press-under-pill-stays-unpinned` — a press
where the pill then appears: a tap's click is hit-tested at the LIFT, so
before the pill's press-origin guard (follow.js) the same tap's click landed
on the just-revealed pill and re-pinned (measured on chromium/iphone);
`touch-drag-unpins` (iPhone, dispatched drag); `press-unscrollable-keeps`;
`d1-keyboard-pill-pauses` (Enter on the focused running pill: drawer opens,
paused, drawer still on screen mid-drip), `d1-latest-resumes`,
`d1-idle-open-keeps`, `d1-keyboard-think-pauses` (closing never resumes).

## BK-030 / D2: the strip no longer collapses (mechanisms + guards, 2026-10-31)

D2 replaced the streaming strip's growing 3-line live tail with a fixed-height
skeleton row, so `endThinking()` changes no height — the ~47.5 px collapse the
`shrink-*` scenarios were built around is gone (`probe.tailInfo().tailH` is
`null`; `stripH` reports the row). The scenarios still run all three close
paths under a reader parked 4 px off the bottom, and two rows changed meaning:
`*-parked-unpinned` measures the park against `PRE_D2_TAIL_PX` (the OLD tail,
so the park is still one a collapse would clamp), and `*-compensated` became
**`*-flat`** — no content shrink, the parked gap survives, and the only
writes in the window are named `keepRead` ones (shrink-turn: `#chat-status`
hides at `turn_complete`, the viewport change `status-clamp` guards). A
segment-close shrink coming back reddens `*-flat`. `*-done-flat` and
`*-carry-repin-attribution` are unchanged.

`guard-follow-through-shrink` kept its job (a clamp under a FOLLOWER must
never read as the user's upward move), but needed a new clamp: after the
`follow-shrink` run (whose D2-flat segment close it still rides), idle and by
keyboard, it opens strip B, wheels down to the new bottom over the log's own
padding (before BK-031 an open body was an inner scroller that ate the wheel;
the aim stayed), and closes it. The accordion close (BK-030, ~200 ms) clamps on every
frame; the follower stays pinned on every 8 ms sample, no pill, and ends at the
new bottom. An UNPINNED reader's animated close is follow.js `holdShrink`'s
bottom slack (no clamp at all) — its per-frame check lives in
`scratch/smoothness_probe/toggles.py`.

## BK-019 Step 5b: the `recover` family — runstate's CHAT-transport recovery paths (2026-10-30)

The fifth family (`--only=recover`, desktop port — no gesture, no geometry
anywhere) drives the five runstate recovery transitions over the CHAT
transport and asserts each one on OBSERVABLE outcomes only: T15 `socketDied`
(+ T16 `catchingUp` and the watchdog's two verdicts), T03 `screenLeave`,
T12 `sendRejected`, T13 `sendFailed`, T14 `streamEndSilent` — 22 checks,
~75 s/engine of real wall time (the watchdog's 10 s interval is MEASURED,
not stubbed: first `/live` ask at 10.1 s after the death, window 8.5–13.5 s).

**The mock models the SERVER, not the client** (`recover` scripts in
`mock_fetch.js`): a `die: true` frame REJECTS the reader (the frame is
server-EMITTED — it rides the `/live` ring — the socket just never delivers
it), the run KEEPS RUNNING server-side (`serverRun`: a time-filtered ring
backs `/live`, `serverDone` persists the answer when the model call returns
— `t15-die` completes at 16 s, between the watchdog's 1st and 2nd ticks:
tick #1 gap-fills the missed ring bytes into the live bubble, tick #2 reads
inactive and runs the persisted heal). `chatFail` answers the POST itself:
`mode:"status"` (409 + detail, store untouched) or `mode:"fetch"` (reject —
nothing exists afterwards). And the stamping is honest: **only the `session`
frame carries `session_id`** — exactly like `chat_sse_response` — queue
frames carry just `{seq, run_id}`. (The committed mock never stamped
`session_id` at all, so `ui.sessionId` stayed `undefined` mid-run and no
recovery path could honestly run. The committed families now see the real
sid mid-run; the full two-engine matrix re-ran green with it.)

**What the checks read**: the status label's WRITE ORDER — a textContent
spy on `#chat-status-label` + a `hidden` spy on the row (`probe_scroll`),
chosen because the warn → catching → writing sequence lands inside ONE
synchronous `handleRunEvent` task, which a MutationObserver cannot order;
the composer's `dataset.mode`; the mock's call log (`/detach` POSTs, `/live`
asks); the transcript's own text. `ui` fields are PEEKED for detail lines
only (`__probe.peek()`), never asserted on. Every wait goes through
`poll()` — bounded, returns a bool — so an unplugged transition produces a
NAMED FAIL row, never an exit-2 HarnessError. `probe.reset()` restores the
run chrome too (composer back to send, row hidden): a scenario that ends
LIVE (t15b) must not turn the next submit-click into a STOP-click. The T03
switch target is recover's own `RC_HIST_SID` session — the matrix's
fresh-page guard owns `HIST_SID` and both families share the desktop page.

**Mutation proofs** (each: `return null;` planted at the top of the
transition in `runstate.js`, scoped `--only=recover` run, restore by copy,
`shasum -a 256` byte-identical to pre-mutation `721f47b4…`):

| transition unplugged | named FAIL(s) |
|---|---|
| T15 `socketDied` | `recover-t15-warn-not-error` + the whole t15 cascade (detach/gapfill/heal/watchdog) + `t16-flip-once` (nothing arms) |
| T16 `catchingUp` | `recover-t16-flip-once` + `recover-t15b-premise-retired` (no flip → the watchdog keeps its premise and asks `/live`) |
| T03 `screenLeave` | ONLY `recover-t03-latched-silent` (`runBytesShown=True`, `labelWrites=3`, `rowReopens=1`) |
| T12 `sendRejected` | `recover-t12-error-lands`, `-hands-back`, `-t12b-stop-stays-for-detached` |
| T13 `sendFailed` | `recover-t13-error-lands`, `-not-detached` |
| T14 `streamEndSilent` | ONLY `recover-t14-keeps-stop` |

The T12b/T14b corners pin the ternary/`|| ui.resync` guard OBSERVATIONALLY
(their rows red when the ternary simplifies or the guard drops); a
`return null;` mutation of their transition would FALSE-GREEN them — the
table's T14 row shows the designed split: `t14-hands-back` reds,
`t14b-resync-holds-stop` stays green.

**Rollup guards** (pwa_resume precedent, `main()` before the verdict line):
`engine_assertion_rows` folds the `[engine]`/`[engine/port]` naming schemes
to per-engine SETS of unique PASS/FAIL identities; `engine_set_problem`
exits 2 whenever the two engines' sets diverge (a family that ran on one
engine only — identity, not totals); `coverage_problem` exits 2 when a
selected, non-SKIPPED engine contributes fewer rows than
`MIN_UNIQUE_PER_ENGINE` (measured from the full run; may only RISE). Pinned
browser-free in `tests/test_scroll_follow_probe.py`.
