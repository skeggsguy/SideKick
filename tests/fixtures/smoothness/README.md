# Front-end smoothness probe (plan frontend-smoothness, Step 8; BK-033)

Browser checks for every smoothness behaviour the plan shipped (BK-026..BK-032, BK-034..BK-036),
promoted from the step-by-step scratch probe (`scratch/smoothness_probe/`). They run against the
REAL web UI modules. The static suites use a mocked `/api/*` SSE (scroll_follow's
`mock_fetch.js` + `probe_scroll.js`). Three suites use the REAL gateway app (`create_app`, a
scratch DB, an ephemeral port). There is no model, no token and no real account, and nothing ever
talks to the live agentd on 8787.

    /opt/homebrew/bin/python3 tests/fixtures/smoothness/run_probe.py

Use `/opt/homebrew/bin/python3`, because it has Playwright. The uv venv deliberately doesn't, and
neither does `/usr/bin/python3`, which is the shell's first `python3` (learnings 2026-10-31). The
browsers are the cached ones in `~/Library/Caches/ms-playwright/`: WebKit, plus the
chrome-headless-shell via `executable_path`. The real-gateway suites start their server with
`uv run python`, so `uv` must be on PATH. A full run takes about 21 min of real wall time (rAF
pacing, no virtual time).

Flags (all optional, `=`-joined like the sibling fixtures):

    --only=stream,taps,toggles,pills,bgrun,activity,reopen,stopanywhere,senderstop,orphan   a subset
    --engines=webkit[,chromium]   one engine (pills/bgrun/activity/reopen are webkit-only)
    --webui=DIR                   serve another web UI copy (red runs, below)
    --red=slack|keepat            toggles' own red modes (toggles.py docstring)

Each suite also runs standalone (`/opt/homebrew/bin/python3 tests/fixtures/smoothness/<suite>.py
--engines=webkit`) with the same flags plus its own scenario filter (see its docstring).

**Output.** Each suite runs as its own subprocess. Its stdout and stderr are **redirected to
`scratch/smoothness_fixture/logs/<suite>.log`**, never piped, because a piped probe has hung.
When you run the runner yourself, redirect it to a file too
(`… > /tmp/smooth.out 2>&1 < /dev/null`). A suite prints `PASS|FAIL <row> | <json>` rows,
`MEASURE <key> = <json>` lines and one `PROBE-RESULT: PASS|FAIL (N failing)` line. The runner
rolls them up and ends with its own `PROBE-RESULT:` line.

**Exit codes** (pwa_resume / scroll_follow precedent): `0` every row PASS. `1` some row FAILed.
`2` the harness broke, in any of these ways: no Playwright; a suite timed out, crashed or exited
other than 0/1; no `PROBE-RESULT` line; ZERO rows; or, on a both-engine run, a suite under its
`MIN_ROWS` floor in `run_probe.py`. The floor is measured, may only rise, and is derived from a
run's unique rows, never from a report. A results block that ran less than it claims must never
read green.

**pytest.** This is the manual tier, like scroll_follow and pwa_resume. `tests/test_smoothness_probe.py`
(offline, in `uv run pytest`) pins the plumbing: flag parsing, the rollup's exit-2 guards, every
hard target in `metrics.py` going red on the value it guards, and every armed scenario existing
in `instrument.js`.

## What each row guards

| Suite | Ports | Rows (all hard asserts) | Guards |
|---|---|---|---|
| `stream` | webkit/iphone, webkit/ipad, chromium/iphone-4xcpu | `block-retypes-0`: no block changes type mid-stream (table/code/list/heading re-typing) | BK-028 (goal 3a) |
| | | `raw-marker-frames-0`: no frame shows raw `**`, `\|…\|` or a fence in prose | BK-028 (3b) |
| | | `think-collapse-0px`: the thinking strip doesn't shrink when thinking ends (the strip must be seen closing) | BK-030 (5, D2) |
| | | `selection-survives-streaming`: a selection in a completed block keeps its length and its node | BK-028 (3c) |
| | | `tap-highlight-none`: every transcript tappable (pill, think-head, show-all, links, summary, attach thumbs) computes `rgba(0, 0, 0, 0)` | BK-026 (1) |
| | | `code-scrollLeft-survives`: a code block swiped 40 px sideways stays at 40, on the same node, through the stream and the finalize. iPhone ports only, because on the iPad the block fits its box | BK-028 (3d) |
| | | `long-answer-churn-under-goal`: the 16k-char answer removes ≤ 6,232 nodes (≥ 90 % under the 62,322 baseline) | BK-028 (3e) |
| | | `sampler-watched-the-stream`, `no page errors`: the instrument really sampled, and nothing threw | — |
| `taps` | webkit/iphone, chromium/iphone-4xcpu | any press on prose (touch, mouse) unpins and "↓ Latest" resumes; a tap under a just-appeared pill stays unpinned; drags unpin; "↓ Latest" tapped while an (emulated) iOS momentum glide still carries the log upward lands at the bottom, pinned (touch, mouse); D1: opening a pill (touch/mouse/keyboard) or the thinking strip (touch/mouse) mid-stream pauses; idle opens stay pinned | BK-029 (4) |
| `toggles` | all three | toggle-anchor: the tapped header/pill holds ≤ 1 px on EVERY frame of the animated open/close (in place, in the clamp regime, streaming, reduced motion) and really animates (≥ 5 in-between frames); "↓ Latest" drops the slack | BK-030 (5) |
| | | `no-inner-vertical-scrollers` (both bodies open); a wheel over an open drawer/thinking body reaches the log's bottom; "Show all"/"Show less" hold the preview/button ≤ 1 px; Show all mid-stream by keyboard pauses | BK-031 (6, D3) |
| `pills` | webkit/iphone | a failed result is loud but leaves the drawer closed; only a tap opens or closes it; a second failure neither opens nor steals; re-renders (resume heal, chat switch) paint every pill closed | BK-035 (3b) |
| `bgrun` | webkit/ipad | a background run that finished while hidden is on screen ≤ 2 s after `visible`, with no chat switch and exactly one `/messages` fetch; still-running and other-chat variants | BK-027 (2) |
| `activity` | webkit/iphone, webkit/ipad | opening a chat mid-run (cron run 95 s into a web search; chat run composing a `web_search` call) shows the row at once with the run's label and a timer ≥ 1:35; a pending approval reads "Waiting for your approval…" within one poll and the tool's label returns when it clears; no "Working…"; a cron run's prompt renders, with or without a memory block | BK-041 |
| `reopen` | webkit/iphone, webkit/ipad | reopening your own chat mid-run (switch away 1.5 s into a paced reply, then back): the reply so far shows at once and keeps streaming, the row runs and the composer reads Stop; at the end the whole answer is on screen exactly once, the row is gone and the composer reads Send | BK-024 |
| `stopanywhere` | webkit, chromium | B opens A's live chat → Stop; switch away → Send, back → Stop; B's Stop ends the run and both pages show Send (A shows "Stopped"), with no reload; finish/gone/hidden variants | BK-034 (2b) |
| `senderstop` | webkit, chromium | the sender's Stop holds on every 200 ms sample while `/live` is active, across a clean EOF, a stream error, hidden + visible, approval-push taps and a relaunch, then the run ends cleanly | BK-036 (2c) |
| `orphan` | webkit, chromium | after every sleep/wake shape there is exactly 1 live `/api/events` stream server-side, and a published mark reaches the app once, from the current instance; `resurrect` models iOS reviving a `close()`d EventSource | BK-032 (7) |

## Hard asserts vs reported numbers (the timing rule)

- **Hard assert** = a zero or structural target: retypes 0, raw markers 0, collapse 0 px, inner
  scrollers 0, toggle-anchor ≤ 1 px, tap highlight transparent, pills tap-only, selection,
  scrollLeft, Stop/Send states, ≤ 1 stream. These don't depend on host speed.
- **Reported, never asserted** = wall-clock numbers: frame p50/p95/max, frames over 25/50 ms, and
  parse/sanitize ms. They print as `MEASURE` lines, and the runner lists them at the end. A slow
  or busy host must not flake the suite, so compare them against the plan's table by eye
  (p95 ≤ 18 ms, 0 frames > 50 ms on this host).
- **Bounded with headroom** = counts and latencies with a plan-set goal line. Long-answer churn
  is ≤ 6,232 nodes, and about 3 today. It only drops on a slower host, because fewer frames mean
  fewer re-renders. The resume heal is ≤ 2 s, and about 30 ms today.

## Red runs (every check can go red)

`--webui=DIR` serves an older web UI over today's fixture. Static suites mirror DIR; gateway
suites get `PROBE_WEBUI=DIR`. Export the tree to scratch:

    rev=1cc60d2   # the commit before the step you want red
    mkdir -p scratch/smoothness_fixture/red/$rev
    git archive $rev src/harness/gateway/webui | tar -x -C scratch/smoothness_fixture/red/$rev
    /opt/homebrew/bin/python3 tests/fixtures/smoothness/run_probe.py --only=stream --engines=webkit \
      --webui=scratch/smoothness_fixture/red/$rev/src/harness/gateway/webui > /tmp/red.out 2>&1 < /dev/null

Trees before each step: `1cc60d2` before step 1 (BK-026, so everything is red), `2f9e45c` before
step 2 (BK-027), `67a8c08` before 2b (BK-034), `8257aba` before 3 (BK-028), `d42a7f4` before 3b
(BK-035), `8ef6f4b` before 4 (BK-029), `98bcf84` before 2c (BK-036), `82ea652` before 5 (BK-030),
`c6f9e33` before 6 (BK-031), `1eec9ca` before 7 (BK-032). An older tree can crash a scenario that
needs newer DOM (e.g. `.acc-in` before step 5). That shows up as a FAIL `… ran` row, and on
both-engine runs as an exit-2 floor miss. Either way it isn't green. `--red=slack` and
`--red=keepat` turn the toggle-anchor rows red without an old tree (toggles.py docstring).

**Red-first record (2026-10-31, WebKit, each suite on the tree before its step):**

| Suite | Tree | Result |
|---|---|---|
| stream | `1cc60d2` | 13 FAIL. The plan's baselines come back exactly: retypes 3 (`H1→H2`, `P→DIV.md-table-wrap`, `P→OL`), raw-marker frames 12/10, collapse 48 px, selection 12 → 0, scrollLeft 40 → 0, tap highlight `rgba(0, 0, 0, 0.4)`, long churn 62,340/62,322 |
| bgrun | `2f9e45c` | 6 FAIL (A: no heal on resume, 0 fetches; B/C marks) |
| activity | `2486c8c` (2026-11-01) | 18 FAIL (the row stays hidden on a mid-run open: no label, no timer) |
| reopen | `ff5b0b7` (2026-11-01) | 4 FAIL (the reopened chat freezes at the join — words 5→8→8 — and the answer never lands: 8 of 24 words, no final word) |
| stopanywhere | `67a8c08` | 14 FAIL (B never shows Stop) |
| pills | `d42a7f4` | 9 FAIL (failed pills open themselves, replays reopen them) |
| taps | `8ef6f4b` | 5 FAIL. The mouse/keyboard rows fail. The touch rows pass, because the 2026-10-13 rule already unpinned on any touch |
| taps (mid-glide rows) | `13a62ab` (2026-11-01) | 2 FAIL (the glide carries the log 2,566 px from the bottom after the tap, unpinned) |
| senderstop | `98bcf84` | 4 FAIL (a clean EOF mid-tool drops the sender's Stop) |
| toggles | `c6f9e33` | 17 FAIL (inner scrollers, wheel trapped, no Show all) |
| toggles | tree + `--red=slack` | 7 FAIL (near-bottom closes, reduced-motion clamp close, iPad swap) |
| orphan | `1eec9ca` | 4 FAIL, `resurrect` live streams per cycle 2, 3, 4, 5 (today 1, 1, 1, 1) |

## Known traps

- **WebKit + the service worker.** Once the real app's `/sw.js` controls a page, headless WebKit
  never answers `page.evaluate` (it hangs, with no error). The real-gateway suites open contexts
  with `service_workers="block"`. The static suites serve `probe.html`, whose entry swap never
  registers the worker. senderstop simulates the worker's `postMessage` (`sw_message`).
- **No BFCache in headless WebKit.** `pageshow.persisted` is always false, so no headless row can
  exercise a BFCache resume. That's why orphan's `resurrect` row *models* iOS reviving a closed
  EventSource. The real engine path needs the Simulator or a device.
- **Touch drags.** Playwright has `touchscreen.tap` but no touch drag. In WebKit a drag is
  `dispatch_event("touchstart"/"touchmove"/"touchend")` on `#chat-log` (isTrusted false, moves no
  pixels). Chromium also gets a real CDP `Input.dispatchTouchEvent` drag.
- **"↓ Latest" floats over the bottom edge's centre.** Once unpinned, a centre tap on a last-row
  header lands on the pill, so the toggles taps aim at the header's label (`left=True`).
- **agentd serves the working tree live.** These suites never touch it. But a device pass sees
  whatever the tree holds, uncommitted edits included, and only after the PWA is relaunched.

## Simulator pass (goal 8, manual)

`sim.py` serves the same static site plus `sim.html`. That page is `probe.html` + `sim_autorun.js`,
which injects the instrument, arms `?scenario=` and sends after `?delay=` ms, because there is no
Playwright inside Simulator Safari.

    /opt/homebrew/bin/python3 tests/fixtures/smoothness/sim.py --port=8799 > scratch/smoothness_probe/sim/server.out 2>&1 &
    xcrun simctl boot <udid>; open -a Simulator
    xcrun simctl io <udid> recordVideo --codec=h264 scratch/smoothness_probe/sim/smooth.mov &
    xcrun simctl openurl <udid> "http://127.0.0.1:8799/sim.html?scenario=smooth&delay=4000"
    # ~30 s later: kill -INT the recordVideo pid; stop sim.py

When the run closes, the page POSTs its sample to sim.py, which prints the `stream` verdict rows
(`PASS|FAIL [sim] block-retypes-0 …`) as measured by the Simulator's own WebKit, and saves the
raw sample to `scratch/smoothness_fixture/sim-result-<scenario>.json`. ffmpeg isn't installed
on the host, so to pull frames from the `.mov` use AVFoundation (`AVAssetImageGenerator`, a few
lines of Swift via `xcrun swiftc`) and look at a sample for flips or jumps.

Reading the numbers: the sample's `frame_max` includes the send click itself, which swaps the
empty-state hero for the conversation and is the one long frame (191–299 ms in the Simulator).
For the streaming window alone, take `frames[len(frames) - bubbleFrames:]`. A second `openurl` on
an already-open page reloads it, so expect one blank frame near the start of the video.

Record (2026-10-31, iOS 27.2 runtime, `smooth`):

| Simulator | retypes | raw marker frames | think collapse | stream-window frames: p95 / max / >25 ms |
|---|---|---|---|---|
| iPhone 17 | 0 | 0 | [0] | 17 / 22 / 0 (889 frames) |
| iPad Air 11-inch (M4) | 0 | 0 | [0] | 17 / 19 / 0 (884 frames) |

Video: no flips, and thinking → answer keeps the header in place. Follow steps only move upward
and die away to 0 (iPad: at most 49 px a step at 0.25 s spacing, nothing in reverse). The one
movement no suite asserts is the streamed **table re-running auto layout** as rows arrive: Notes
widens and the other columns narrow. On the iPad it widens to the right and the left columns hold.
On the iPhone (390 px) the headers re-wrap mid-word ("Before (s)" becomes "Befor e (s)").

## Files

- `run_probe.py`: the entry point and rollup (import-safe; pinned by tests/test_smoothness_probe.py)
- `common.py`: shared plumbing (site mirror, server spawn, boot, the `Rows` output contract)
- `metrics.py`: pure verdicts over the instrument's numbers (the hard targets)
- `instrument.js`: the per-rAF sampler + the scenarios (`smooth`, `tapper`, `tapper2`, `thinker`, `toggler`, `longbody`, `long`)
- `stream.py`, `taps.py`, `toggles.py`, `pills.py`, `bgrun.py`, `activity.py`, `reopen.py`: the static-site suites
- `stopanywhere.py`, `senderstop.py` + `stopanywhere_server.py`; `orphan.py` + `orphan_server.py`: the real-gateway suites
- `sim.py`, `sim_autorun.js`: the Simulator pass
- The device checklist for the user's iPad/iPhone: `docs/smoothness-device-checklist.md`

`scratch/smoothness_probe/` is now superseded. `run.py`, `extra.py`, `toggles.py`, `pills.py`,
`bgrun.py`, `stopanywhere*.py`, `senderstop.py`, `orphan*.py` and `instrument.js` all live here
now. It's gitignored and left in place, with its `results*.json` as the step-by-step record.
