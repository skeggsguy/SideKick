# PWA-resume repro probe (plan pwa-resume-reconcile, Step 1)

Deterministic repro + yardstick for the iOS-PWA resume blank-flash: forces
"chat stream dies mid-turn → server detaches → client recovers" headlessly
and asserts on the real `transcript.js`/`events.js` rendering.

    # The Step-5 matrix: chromium AND WebKit (the shipping engine for the
    # iPad/iPhone PWAs), one site build. Needs the system python3 (Playwright).
    python3 tests/fixtures/pwa_resume/run_probe.py
    # Offline/uv equivalent — chromium only, with a loud NOTE (the uv venv
    # deliberately has no Playwright); same assertions, same exit codes.
    uv run python tests/fixtures/pwa_resume/run_probe.py

Exit codes: `0` all assertions green (the state since pwa-accurate-resume
Step 6 landed the client resync) ·
`1` probe ran, assertions failed · `2` harness broke (jsc parse-check,
missing results block, no app.js entry tag in `index.html`, an engine below
the 107-assertion floor — a scenario family silently skipped — or the two
engines' assertion sets disagreeing).

**TODAY'S CORRECT STATE IS EXIT 0.** The four live-resume assertions
(`resume-mid-thinking`, `no-duplicate-words`, `partial-head-survives`,
`cold-open-live`), the two Step-6 regression guards (`reopen-dedupe`,
`seq-restart-new-run-paints`), and the six run-identity assertions
(`second-device-run`, `second-device-short-run`, `empty-tail-join-provisional`,
`terminal-join-attribution`, `late-old-run-title`, `missing-route`) went green with
Step 6's client resync and are now the REGRESSION GUARD for it — any FAIL
line is a regression. Everything
else is GREEN too — the probe stays the regression guard for the recovery path.
THE HEAL CONTRACT (pwa-accurate-resume Step 6): a resume asks
GET /api/chat/{sid}/live AT ITS OWN WATERMARK (`live.seq`, never
`live.since`); `active:true` gap-fills the ring tail into the existing DOM
through the live painter (only persisted rows replay unpaced), `active:false`
falls back to the run_done-style persisted refetch (the sole POST-run heal).
The pre-Step-6 yardsticks below stay green as the regression guard: mid-call
re-sync from the STORE remains forbidden.

- `blank-flash` (Step 2): `renderSession()` used to call `resetTranscript()`
  before awaiting the messages fetch, leaving `#chat-log` empty for the fetch
  duration (the visible flash). Fixed by the fetch-then-swap in
  `transcript.js`; the `run_done` heal swaps atomically.
- `mid-word` + `heal` (the settled contract): scenario B delivers the first
  recovered `/api/events` event for the lost session (events.js's catching-up
  point) and asserts the partial tail IS painted into the frozen DOM (the
  accepted artefact — proves the recovery path ran); then `run_done` heals to
  the COMPLETE answer with exactly ONE `/messages` fetch, the fragment gone,
  replaced atomically (no empty frame through the heal).
- `no-premature-resync` (the guard): ZERO `/messages` fetches while
  catching-up, before `run_done`. `mock_fetch.js` models the MID-CALL store
  (`settled=false` hides the in-flight assistant message, as `agent.py`
  persists only when the call returns), so a `renderSession` that sneaks back
  in would visibly wipe the frozen head — both the fetch counter and the
  head-survives assertion go loudly red (proven against a mutated
  `events.js`; a future re-sync-at-gap-detection cannot pass silently).
- `F` + `G` (thinking-block-vanish): a frozen DOM carrying a `.think` strip
  (F: still streaming — the catching-up `tool_call` closes it via the real
  `startToolPill→endThinking`; G: already collapsed to "Thought for Ns") must
  SURVIVE recovery: `.think.done` after the sole heal, in the store's order,
  thinking text preserved, and exactly one replay
  `startThinking→endThinking(replay)` pair in `window.__sk._thinkLog` with no
  segment close after the buffer flush. Both GREEN today — the iPhone bug is
  NOT reproducible in Chromium headless, so the `LOG-F`/`LOG-G` output lines
  (every transition, stacks for `endThinking`/`resetTranscript`) are the
  diagnostic payload for the on-device case. The strip-present detectors are
  positive-controlled in-run (remove the element → red); the log assertions
  were proven against a mutated `transcript.js` — a spurious thinking cycle in
  the buffer flush reddened F(a), F log(b)/log(c), G's log check AND scenario
  A's segment-count guard (8 FAILs), then reverted byte-exact.
- `cold-start-restore` (pwa-accurate-resume Step 2): scenario I imports the
  REAL `app.js` (the entry tag is swapped, so module evaluation runs the
  production boot; a query-string re-import is another cold boot while all
  dependencies stay the same instances). With a stored `lastChat` id and no
  `#session=` hash it asserts the boot REOPENS the stored chat (its own
  `/messages` fetch, transcript painted, greeting hidden); that "+ New chat"
  CLEARS the stored id and reopening stores it again; that `openSession`
  itself (the deep-link/push write site) stores the id it opens; and that a
  `run_start` auto-open (events.js `openLiveSession`) moves the pointer too,
  as does tapping that run's toast (both funnel through `openLiveSession`).
  THE DELETION VERDICT IS THE SINGLE-SESSION ROUTE, NOT THE LIST —
  `GET /api/sessions` is capped at the 50 most recent rows (live DB: ~950
  sessions), so boots build FULL 50-row pages without the id: with
  `singleRow` set the chat restores anyway and its option is PREPENDED with
  the real title — and a later picker rebuild (visibilitychange / title /
  done / rename all call `loadSessions`) KEEPS that option, since the capped
  page never re-lists it (rename refreshes its label in place); with
  `singleGone` the route answers 200+null — only THAT
  forgets the id, which is then never fetched; with NEITHER (absent mode: a
  404, i.e. an agentd predating `GET /api/sessions/{id}`) the boot greets but
  KEEPS the id — version skew must not destroy the pointer. A FAILING list
  fetch (`sessionsFail`, offline relaunch) likewise greets and keeps. A boot
  with NO stored id still lands on the greeting with the populated picker
  (the pre-existing home view). Never an error line on any path (asserted via
  absence of `.msg.error`). localStorage is the real store under test — live,
  not a source regex (it works fine under chrome-headless-shell; see the
  control line). `mock_fetch.js` holds the
  `sessions`/`sessionsFail`/`singleRow`/`singleGone` modes, and the scenario
  stubs `navigator.serviceWorker` with a never-resolving object so
  headless-shell SW registration can't throw into the page-error list
  (push.js guards every entry with `"serviceWorker" in navigator`).
- `H` (stale buffer discarded — the wizard's fix): during a RECOVERY
  `renderSession` (armed via the recovering argument, reached through
  `events.js`'s one refetch entry point `resyncRun` — called by run_done,
  visibilitychange, and the detach-watchdog)
  the completed turn's rerouted events that buffer inside the fetch window are
  stale — the authoritative replay already contains them — so the finally
  clears `_buffer` WITHOUT dispatching (`renderSession:flush` entry
  carries `discarded:true`). H asserts `.think.done` + the replay label + no
  zombie strip survive, and nothing closes a strip post-flush. `DUMP-H2`
  (evidence, not an assertion) runs the same window injections through the
  NON-armed, flushing path (normal opens keep flushing) and dumps what a
  flushed stale event does: `thinking_delta` zombie-spawns a second `.think`,
  `delta` appends a tail bubble.

The other assertions are the positive controls that keep those greens honest
(the harness paints, the observers detect empties/splices, the fetch counter
sees a `/messages` request at all, the recovery branch really ran, nothing
double-paints) — they must stay green throughout.

## Live-resume assertions (pwa-accurate-resume Steps 5–6): green since Step 6

Scenario L (`probe_live.js`, run after H2, before the cold-start boots) drives
the Step 3–4 server contract the way a real resyncing client will face it. It
was RED by design until **Step 6 landed the client resync** (`webui/replay.js`
— seq slots, `/live` gap-fill, `/live`-predicate resume decisions); it is now
the regression guard for that machinery. The mock models a RUNNING
server (`mock_fetch.js` `liveRun`): a per-run ring of stamped events
(`{seq, run_id, ...}` — `seq` restarts at 1 every run, so a watermark is only
meaningful paired with its `run_id`), `GET /api/chat/{sid}/live?since=N` →
`{active, seq, oldest_seq, gap, events}` (else `{active: false}`), and
`/messages` → `{messages, live: {since, seq, oldest_seq, gap, events} | null}`.
**The timing is honest:** while a run is active the persisted store holds ONLY
the model calls that already RETURNED — a tool-loop run's mid-call `messages`
carries the completed steps (its text and its tool card) but NOT the in-flight
step, whose only source is the ring tail. A mock that always held the finished
answer would green-light exactly the lossy design that was built and reverted
(decisions 2026-10-17); this one does not. Suspension is real to the app:
`document.visibilityState` is overridden and a `visibilitychange` event
dispatched, so production's own handlers make the decisions under test.

The contract facts Step 6 must branch on (all pinned server-side by
`tests/test_chat_recovery.py` — that file IS the client's contract):

- **The two `sinces` are different numbers.** `live.since` is the SERVER's
  persisted watermark — the join point where the rows end and the tail begins.
  It is NEVER a client watermark. The client's own watermark, after painting
  messages + tail, is `live.seq`; later resyncs send THAT to
  `/live?since=N`. `cold-open-live` reads the mock's recorded `/live` URLs to
  pin exactly this.
- **There are two kinds of `gap`, and which endpoint said it is the branch.**
  A `gap` on `/messages` lost only UNPERSISTED tail (the in-flight step
  outgrew the ring; the persisted half is whole by construction): paint the
  suffix from `oldest_seq`, the loss heals at `run_done`. A `gap` on `/live`
  lost events the CLIENT lacked: paint the suffix from `oldest_seq` too, but
  know the client's own hole is the one being proven. Neither is fillable by a
  mid-call store refetch (the store has no in-flight answer — the lossy path).
  Read the flag; never do seq arithmetic.
- **`active` (the predicate) is not registry membership.** `/live`'s `active`
  answers the one liveness predicate (`gateway/live.py`); a session row's
  `active` is `gw.active_chats` MEMBERSHIP (stale through the done→`_reap`
  tick), and the capped 50-row `/api/sessions` page makes an off-page live run
  read as ABSENT — the "finished while away" verdict `partial-head-survives`
  weaponizes to wipe the head. Resume logic decides liveness via `/live`, and
  only `active: false` obliges the persisted-refetch fallback.
- **Re-pull rule.** A client holding a watermark for the live run must NOT
  re-fetch `/messages` (the full batch is MiB-scale): it sends its watermark to
  `/live` and returns to `/messages` only when `/live` says `active: false`. A
  COLD open (no watermark) fetches `/messages` alone — one fetch, one screen.
  Both visibility-resume assertions count `/messages` calls across the resume:
  a mid-run re-render reds them even though the Step 4 live join can
  reconstruct the head from the store + tail (the DOM alone cannot tell the
  two paths apart).
- **`/api/events` also carries seq-LESS background-run events** — pass them
  through; "no seq" ≠ "old". The existing scenarios deliver seq-less events
  through `handleRunEvent` and stay green through Steps 6–7, which pins
  the pass-through half of the dedupe rule.
- **Watermarks belong to the DOM they painted.** A resync that WIPES the
  transcript (re-render) destroys the already-painted events sitting below the
  fresh tail's `since` — replaying with the stale watermark then skips them and
  loses the head for good. Either keep the watermark in lockstep with the DOM
  (reset the run's watermark when its transcript is rebuilt) or don't rebuild:
  gap-fill INTO the existing DOM. `resume-mid-thinking` and
  `partial-head-survives` are written to catch exactly this trap.
- **The multi-step join is server-side.** Every Step-5 run is a tool loop (the
  ToolLoopAgent shape from `test_chat_recovery.py`): rows carry the persisted
  steps, `live.events` (from `since`) the unpersisted tail — persisted rows
  have no `seq`, so no client-side dedupe can join the two; replay the tail
  from `since` as given and the persisted pill stays painted exactly once.

The four assertions (one compound FAIL line each; what GREEN requires):

- `resume-mid-thinking` — the thinking strip is streaming at suspension, and
  the B-deltas were emitted while hidden: a hidden tab holds no `/api/events`
  socket, so those events reached NO sink — the ring is their only source.
  After resume, ONE `.think` block must show A (pre-suspend) + B (the
  ring-only hole) + C (the continuing tail) in order, each marker once, and the
  answer must keep painting. GREEN: the resume fetches `/live` at its own
  watermark and paints the missing events into the live DOM through the live
  painter — no reset, no store refetch, no vanish, and no re-render whose
  stale-dedupe drops A.
- `no-duplicate-words` — the gap-fill batch is a snapshot at request arrival;
  while it is in flight the rerouted queue event WD4 (also in the batch) and a
  live event WD6 (NOT in it) paint through `/api/events`. When the batch lands it
  must skip WD4 (already painted) and still paint **WD5 — a seq BELOW the
  high-water mark WD6 just set**, because the live stream skipped it entirely.
  Before Step 6 neither happened (no `/live` request was made — the detail
  line showed the empty `liveCalls`); a Step 6 that replays a batch blindly
  paints WD4 twice.
  GREEN: dedupe by **"already painted"** on every replay source. The live
  `/api/events` sink may use the high-water mark (events arrive in order), but a
  replay batch may only skip an event it has ACTUALLY painted: WD5 is below the
  high-water mark WD6 set yet is exactly the missing word, so a scalar
  `seq <= last-seen` cutoff silently drops it (`WD5=0` in the detail line) and
  the hole survives until run_done. Track the painted seqs per `run_id` (a
  seen-set — and membership must be run_id-aware: seq restarts at 1 every run,
  so a new run's first events must not read as painted by the last run's
  intervals), or hold live events while the resync is in flight and apply both
  in seq order — either way the batch fills holes, it does not just trim a tail.
  This one fixture is what separates the two: the word-doubling class is killed
  by the seen check, and the hole-filling word survives only with it.
- `partial-head-survives` — a tool-loop run is mid-call-2 (the pill's step is
  persisted, the answer head is not) and its row is OFF the capped page; the
  visibility handler's row-membership verdict said "finished while away" and
  pre-Step-6 it re-rendered from the store, wiping the on-screen head —
  structurally the reverted 2026-10-17 re-sync. GREEN: decide liveness via
  `/live`'s `active` BEFORE any store-based heal — `active: true` means fill
  the ring tail instead; HEAD1/HEAD2 stay on screen exactly once, the persisted
  pill is never double-painted (a `/live?since=0`-style replay would re-paint
  it), the post-resume tail keeps painting, and NO `/messages` fetch happens
  mid-run (the re-pull rule — a store render + live join can rebuild the same
  head, so the DOM alone cannot distinguish the paths).
- `cold-open-live` — a cold boot (`app.js?coldopen=1` re-evaluation restoring
  `lastChat`) opens a chat mid-answer: `renderSession` must paint `messages`
  AND replay `live.events` — ANSW1/ANSW2 exist on no other source. Pill count
  1 and one user bubble prove the persisted step painted once. Then the tab
  backgrounds again and returns: the resume gap-fills ANSW3 (ring-only) from
  **`live.seq` (7), not `live.since` (4)** — the mock records every `/live`
  call's `since` for this pin.

Two Step-6 regression guards (cleric, same commit; both were RED on the
client-resync build and are green after the fix):

- `reopen-dedupe` — a NORMAL reopen (not a suspension) mid-run: an event
  emitted before the `/messages` fetch is sampled but delivered on
  `/api/events` during it is BOTH in the join's tail and in the replay buffer;
  flushing it after the join paints it twice. Mirrors `no-duplicate-words` at
  the join/flush seam.
- `seq-restart-new-run-paints` — a second run on the same session restarts at
  seq 1; the slot still holds the previous run's painted interval. The sink
  gate checks `alreadyPainted` BEFORE `markPainted`'s run-change reset, so
  membership must be run_id-aware or the new run's first tokens are swallowed
  (reachable on the normal second message — nothing resets the slot between
  turns).

THE IDENTITY RULE (cleric's handed-back; `webui/replay.js` header, one rule,
three call sites): **a slot vouches for exactly one run, and BATCH resync may
only paint what the slot vouches for** (`joinReason(id, events, seq)` — the
single verdict). Same run → gap-fill, deduped. Different run / provisional slot
(an empty-tail join names no run) / terminal run (run_done passed the batch
while the fetch flew) → **cold-join**: the sanctioned one-fetch live join
(`renderSession` rows + tail, recovery-armed when the run already ended).
Every slot reset MARKS the displaced run terminal (syncRun + observed
run_done); a terminal run's events never touch the slot (markPainted refuses
at the choke point — every gate inherits it). An empty-tail join leaves a
PROVISIONAL slot with no marks and no watermark: unattributed marks answer
"painted" for a NEW run's seq 1..N — token loss; the flush-race row-duplicate
they might have prevented is transient and heals at `run_done`. An EMPTY
batch is an identity witness too: seq is monotonic PER RUN, so a response
whose `seq` is BELOW my watermark cannot be my run — a SHORT new run would
otherwise read as "caught up" (`seq == water` is the one case no signal can
split; it heals at the new run's next event or run_done). `/live` 404
(agentd predates the route) → the `{missing:true}` sentinel: the watchdog
finishes its /detach retries and STOPS — run_done stays the healer, and a
store render would be the lossy path on a server this old (no live join).

- `second-device-run` — run A's head painted live, the stream died, and while
  hidden A ended (its `run_done` died with the socket) and run B started
  ELSEWHERE. The resume asks `/live?since=<A's watermark>`: the batch names B
  and seq restarted, so B's seq 1 (`E1`) is INVISIBLE to the ask — the exact
  skip the rule forbids gap-filling through (RED without the cold-join at
  `E1=0`). The join paints A's rows once, B's whole tail once, clears
  `resync` (A's heal IS this join), and re-primes the slot on B.
- `second-device-short-run` — face 1's HARD half: B is SHORTER than A's
  watermark, so `/live?since=<A's water>` returns an EMPTY batch. No event
  names a run; the response's own `seq` is the witness (below my watermark ⇒
  the numbers restarted). It must still cold-join: B's head (`T1`, `T2`) is
  invisible to the ask (RED without the `seq` witness: `T1=0 T2=0`, the
  client calls itself caught up and `resync` stays armed).
- `empty-tail-join-provisional` — face 3: a mid-run cold open whose every
  emitted event is persisted (`live.events == []`) names NO run. The join
  must leave a provisional slot: no marks, no watermark. Proven
  behaviourally — a late post-`run_done` `title` for the ended run must not
  stamp a watermark (RED with the terminal guard off: the next resume asks
  `since=7`), and after B starts elsewhere the next resume asks the WHOLE
  window (no `since`) and cold-joins B's head in (`Q1`,`Q2` once).
- `terminal-join-attribution` — face 3's SECOND seam: a NAMED tail whose
  run ends DURING the `/messages` fetch. `syncRun` refuses terminal runs, but
  this payload is the only witness — the join must attribute the fresh slot
  (runId + that run's water), never leave a provisional slot holding
  `live.seq` (the forbidden unattributed-watermark state). Asserted on the
  slot plus both paints (R0 row, R1 tail, once each); RED without the
  attribution (`runId:null, water:2`).
- `late-old-run-title` — the carried post-`run_done` `title` publisher, run
  A's name, after B primed the slot: it relabels the picker (read before any
  picker rebuild) but must NOT reset B's watermark — proven by the next
  resume asking `since=B.seq` and painting the new event once (RED with the
  terminal guard off: the watermark is wiped, the resume escalates, content
  goes missing on the catch-up screen).
- `missing-route` — `fetchLive` reads a 404 as `{missing:true}`, never as a
  network null (retry forever) and never as `active:false` (which would
  store-render — lossy on that server). The 10s watchdog loop itself is too
  long for the probe's virtual budget; its stop branch is pinned in
  `test_conventions.py`.

Positive controls keep these detectors non-vacuous (green): the mock `/live`
route answers active, filters `since>`, clamps a negative `since` to 0 exactly
as `event_ring.replay` does, records the call, and is session-scoped (another
session's `/messages` carries no `live`); the word-counter sees a deliberately
doubled marker; each assertion's detector goes red on an emptied transcript.
Each scenario resets `liveRun`/`liveCalls`/localStorage/ui state so the
pre-Step-5 scenarios and the cold-start boots are untouched (verified: with
scenario L skipped entirely, scenario I still passes all 61 checks), and
scenario I still runs last.

## Compaction seam + status (compaction-checkpoint-bookmark Step 4): green since Step 4

Scenario M (`probe_compaction.js`, runs after H2 and BEFORE L, before the
cold-start boots) drives the compaction frames through the production sink and
asserts the DOM: `compaction_start` puts the status row into "Compacting…"
(spinner on — the phase carries `waiting: true`, so the 20 s slow-model hint
still applies); a `compaction` frame paints a `.compaction-seam` divider
MID-TURN — ONE `#chat-log` child inserted before the LAST `.msg.user` row (the
current turn's first row), `role="separator"`, carrying `data-cp` =
checkpoint_id and no server text at all (the plan's affordance is the line; the
aria-label is a static string). A second check reads the COMPUTED style: the
node must be compaction.css's 2px `var(--accent)` rule, so a dropped stylesheet
link or a class renamed on one side cannot ship an invisible empty div green.
Re-delivery of the same `compaction` seq is skipped by replay.js's painted-seq
gate — that, not `data-cp`, is what keeps the seam single (`data-cp` is Step 5's
reload-dedupe handle). The orphan case (a cancelled run closes the stream
before `compaction`): each terminal frame — `stopped`, `error`, `run_done` —
must clear the "Compacting…" phase and hide the row. `compaction` itself sets
no phase; the next `model_wait` relabels. Each scenario's control REMOVES the
seam and asserts its detector goes red, keeping the position assertions
non-vacuous (an always-append mutation reddens exactly the three position
checks). Exact anchoring at the checkpoint's row is Step 5's reload job; live,
the seam is an approximation of it. `compaction` is in replay.js's RENDERED
whitelist (it is transcript content); `compaction_start` deliberately is not —
it only drives the status row.

## Compaction reload seams (compaction-checkpoint-bookmark Step 5): green since Step 5

The scenario-M reload block (`compaction-reload` checks, same
`probe_compaction.js`) drives the OTHER half of the seam: a page refresh
where the transcript comes from `/messages`, not the live sink. The mock's
`/messages` answers gained `compactions` (real-route shape: `checkpoint_id,
anchor_idx, created_at, est_before, est_after`) and the fixture rows gained
`idx` — the STORE idx `anchor_idx` addresses (array position would
misanchor once the table has a hole). What these pin:

- R1: a reload with a checkpoint renders ONE `.compaction-seam` anchored
  directly above the row whose idx first reaches `anchor_idx` (below the
  pre-cut answer), carrying `data-cp` AND `data-anchor`; the detector is
  positive-controlled by removing the node.
- R2: a session WITHOUT compaction data renders none.
- R3 — THE DEDUPE (cleric's mid-turn double-paint): one compaction offered
  TWICE — in `compactions` AND in the ring tail `consumeLiveJoin` paints —
  must produce ONE seam. Anchor 1 puts the anchored position ABOVE the
  first answer, distinct from the join's before-the-last-user-row
  approximation, so a dedupe that kept the wrong offer is red on position
  as well as count. (Mutation-proven: dedupe-off → `seams=2` on this line
  ONLY.)
- R4 — the flush: a seam anchored past the last persisted row (the race
  snapshot where the compaction event postdates the transcript read) parks
  ABOVE the live tail via `flushSeams` (replay.js `consumeLiveJoin`, ahead
  of both early returns); the tail paints after it. (Mutation-proven:
  flush-off → `seams=0` on this line ONLY.)
- R5 — a session that compacted TWICE (the real long-session shape, ~one
  per 70k of growth): BOTH seams paint, each above its own anchor row, in
  anchor order — the payload is a LIST drained per row, not a single
  entry. (Mutation-proven: keeping only the first entry → `seams=1` on this
  line ONLY; draining last-first → `anchors=3,1`.)

The live-frame seam now also carries `data-anchor`; `paintSeam` dedupes on
anchor-then-cp by iterating the on-screen seams' datasets (no server value
into a selector — XSS rule).

How it works:

- `run_probe.py` mirrors `src/harness/gateway/webui/` into
  `scratch/pwa_resume_probe/site/` with symlinks (learnings 2026-10-16
  recipe), copies the real `index.html` with the app.js entry tag swapped for
  `mock_fetch.js` + `probe.js` (+ `probe_coldstart.js`, the cold-start
  scenario, `probe_live.js`, the Step-5 live-resume scenario, and
  `probe_compaction.js`, scenario M — all three imported by `probe.js`), serves
  it over http:// (ES modules require
  it), and dumps the DOM with the cached chrome-headless-shell under
  `--virtual-time-budget` (plain `--dump-dom --timeout` dumps AT the load
  event and misses async writes — corrected recipe, learnings 2026-10-17).
  The entry tag is matched version-tolerantly (`/app.js?v=NN`), because the
  webui convention bumps that `?v=` on every asset edit — a pinned version
  made the probe exit 2 on exactly the runs it exists to measure.
- **Engines (BK-019 Step 5):** the canonical `python3 run_probe.py` now runs
  chromium AND WebKit by default (both against one site build) whenever
  Playwright is importable; under the uv venv (no Playwright) it runs chromium
  alone and prints a loud `NOTE:` — the Step-5 matrix is the system-python
  command. `--engines=chromium,webkit` / `--engines=webkit` selects explicitly.
  Two integrity guards make a one-engine or empty-engine green impossible:
  each engine must emit at least **107 unique assertions** (the floor, raised
  with the figure when a check is deliberately added/merged), and when two
  engines run their assertion SETS must be identical (a family skipped on one
  engine is a harness error, not a pass). WebKit — the shipping engine for the
  iPad/iPhone PWAs — has no virtual-time knob, so `run_webkit()` drives the
  same page through Playwright's cached WebKit in REAL time and waits for the
  results block: the fixture sleeps already carry 350–700 ms of slack over the
  150 ms mock latency, and the tightest window (the 40 ms sample inside the
  150 ms fetch window, scenarios A/B/H and two in probe_live) survived real
  timing on the matrix runs (107 unique assertions green, both engines).
  rAF ticks for real there — harmless, the assertions flush via
  `finalizeAgentBubble()` regardless. The 40 ms samples cannot reorder past
  the mock's 150 ms timer: both are `setTimeout`s on the same event loop and
  the earlier expiry runs first, so machine load lengthens a run instead of
  moving the sample outside the fetch window — a WebKit red in A/B/H is a
  real red, not a timing artefact. Run it with the system `python3`
  (it has Playwright; the uv venv deliberately does not), like
  `tests/fixtures/scroll_follow/run_probe.py`.
- `mock_fetch.js` (classic script → installed before any module evaluates)
  answers `/api/.../messages` after a 150 ms delay — that delay is the
  blank-frame window being measured. A `settled` flag models the MID-CALL
  store: while false (a model call is in flight) the route omits the assistant
  message, exactly as `agent.py` persists it only when the call returns.
  While `liveRun` is set the route answers the Step 4 contract instead
  (`{messages: messagesMid, live}` — the in-flight step absent from the store,
  the ring tail joined at the server watermark), and it serves
  `/api/chat/{sid}/live?since=N` from the same one `active` flag (recording
  every call's `since` in `liveCalls`, which the watermark pins read).
- `probe_live.js` (scenario L) hosts the four live-resume assertions (green
  since Step 6), the two Step-6 regression guards, and the six run-identity
  assertions (`second-device-run`, `second-device-short-run`,
  `empty-tail-join-provisional`, `terminal-join-attribution`,
  `late-old-run-title`, `missing-route`); its
  `emit()` mirrors the run's `emit()` closure (stamp `seq`/`run_id` once at
  emit, write the ring), `deliver()` hands the /api/events sink view of an
  event, and suspend/resume drive the production `visibilitychange` handlers.
- `probe.js` paints a mid-stream transcript through the real handler, sets
  the recovery state (`ui.resync = { id, seen }`), delivers `run_done`
  (scenario A) and the first recovered delta followed by the terminal
  `run_done` (scenario B) through the production `window.__sk.handleRunEvent`
  hook, and watches `#chat-log` with MutationObservers (callbacks land on
  microtask checkpoints = paintable frames) plus a mid-fetch timer sample and
  a `/messages` fetch counter.
- **Recovery diagnostic (`window.__sk._thinkLog`)**: `thinking.js`,
  `transcript.js` and `events.js` each carry a local `tlog(name, state,
  withStack)` that pushes `[name, state, stack|null, ms]` — `new Error().stack`
  on request, else an explicit null, plus `Date.now()` taken AT PUSH (BK-023:
  the 2 s pump's drain-time stamps could not tell a paced stream from a burst)
  onto `window.__sk._thinkLog`. It is armed ONLY when the array exists — the
  probe sets it, a Safari console, or `?recoverdebug=1` on the app URL (the
  console-less on-device capture: `events.js`'s `armRecoverDebug` creates the
  array and unhides/re-creates `<pre id="recover-debug">` inside `#chat-log`;
  `app.js`'s boot pump appends drained entries every 2 s — inert without the
  param, and the probe page never loads `app.js`) — so production pays two
  property reads per call and nothing else. It never mutates render state
  (that state is module-private: transcript.js header).
- **rAF is dead headless**: `requestAnimationFrame` callbacks never fire under
  `--virtual-time-budget` (measured 0 ticks in 400 ms of virtual time while
  `setTimeout` fast-forwards), so `transcript.js`'s paced reveal paints nothing
  on its own. Every "what is on screen" assertion flushes with
  `finalizeAgentBubble()` — what a real browser does via rAF within ~0.6 s.
  Prefer `setTimeout` for anything that must actually run in this harness.
- `jsc_check.js` parse-checks the fixture scripts PLUS the webui modules
  around the recovery path (`thinking.js`, `transcript.js`, `events.js`, and
  `app.js` — not loaded by the probe, but it carries the capture boot block)
  with the bundled jsc (run with cwd = repo root; the printed lines are the
  signal, not the exit code) — a syntax error in any of them exits 2 before
  the chrome launch.

Server-side counterpart: `tests/test_chat_recovery.py` pins the
detach/reroute contract (consumed events never replay, queued events reroute
in order, exactly one `run_done`) AND, from Steps 3–4, the whole resync
contract this probe now mocks: seq-at-emit, the per-run ring +
`GET /api/chat/{sid}/live`, and the `{messages, live}` join at
`ChatRun.persisted_seq`. That file is the contract; scenario L is its client
half, green since Step 6 honors it.
