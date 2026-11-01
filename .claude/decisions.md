# Decisions

Why A over B, for choices below plan level. Architecture-level locked decisions live
in the table at the top of docs/PLAN-v2.md — that table is the source of truth; do not
duplicate it here. Read before choosing between approaches — keep entries ~2 lines, newest first.
Full text of shortened entries: .claude/learnings-archive.md (grep the date).

Format: `YYYY-MM-DD — decision — why`

- 2026-11-01 — README is a **runnable project that is also a showcase** (Flash-Next structure: promise line, measured
  headline numbers, feature sections, quick start, settings table) — rejected keeping the "published personal system,
  not a product" stance: the system is now runnable, and one honest "what it really takes" paragraph covers the cost.
- 2026-11-01 — README headline numbers are **real usage from the runs table** (cloud era vs local era, spend as a
  guide) — rejected approval-prediction accuracy: 82.3% loses to the 95.4% always-approve baseline on the same 570 rows.
- 2026-11-01 — The README names the **concrete dogfooding workflows** (property watch, restaurant booking, holidays,
  document batches) — rejected generic phrasing: owner's call, not sensitive, and it brings the project to life.
- 2026-11-01 — The owner's display name comes from **`HARNESS_USER_NAME` in .env** (greeting + system prompt; unset =
  no name) — rejected a web-UI setting: a single-user value read at startup needs no UI or storage.

- 2026-11-01 — BK-024: reopening your own chat mid-run **un-latches the stream** (runstate T35 `reopenResume` at
  renderSession entry; supersedes BK-019 D9's for-good paint freeze) — rejected an explicit "run in flight elsewhere"
  state: the join + buffered flush already dedupe a mid-run open, so streaming on costs one guarded write, not a new UI state.
- 2026-11-01 — BK-015: phone md-table sizing is **cell-level word sizing + a `min-width: 3em` column floor
  (variant B), not `table-layout: fixed` equal columns (A)** — measured 390px: A crushed the Item column
  190→90.3 px; B's floor is honoured in BOTH engines (0 COLLAPSE, docSW==390).
- 2026-10-31 — BK-031: "Show less" holds **its own button** under the finger (follow.js `keepAt`: scrollTop −= the
  shrink, never a clamp) while "Show all" keeps scrollTop (keepRead) — rejected keepRead for both (the button jumps by
  the shrink, 634 px in toggles.py --red=keepat) and an animated toggle (holding the button per frame is BK-030's rejected compensation).
- 2026-10-31 — BK-030: an UNPINNED reader's animated accordion close holds its header by **bottom slack** (follow.js
  `holdShrink`: the clamp deficit reserved as `#chat-log` padding before the close, only ever trimmed after, dropped on
  re-pin) — rejected per-frame scrollTop compensation (no write can exceed maxTop: the clamp IS the move), animating
  only non-clamping closes (the clamping ones still jump) and transform/clip (layout height still drops, one frame).
- 2026-10-31 — BK-036: a chat stream that ends **cleanly after the run started is T15, not T14's "agentd restarted"** —
  resync armed, Stop kept, /live decides (a real restart answers inactive → heal + Send); plus T33: a T27 live gap-fill for
  the screen arms Stop when nothing holds it. Rejected keeping T14's hand-back (dropped the sender's Stop for a whole tool call).
- 2026-10-31 — BK-029: **any press in `#chat-log` unpins** — touchstart (2026-10-13's rule, reaffirmed by the user) plus mouse/pen
  pointerdown, a bare tap included; D1: opening a pill/thinking body mid-run pauses too (`readerOpened`, the keyboard's path).
  Rejected tap-hold + 10 px slop (built, then reversed): the user wants control — touching the transcript means "stop moving".
- 2026-10-31 — BK-037: confabulated routify/OSS relay hosts are blocked **host-anchored — one regex, two call sites**
  (`_netguard.is_confabulated_host`: `check_target` before DNS + `egress_proxy.target_addresses` before connect, because
  Chrome follows redirects past the tool-side check). Rejected scanning call arguments / whole URLs (over-fires on prose —
  upstream removed its bare-token rule for exactly that) and the `/etc/hosts` sinkhole (a network error, not a directive).
- 2026-10-31 — BK-037: the relay refusal is a **directive-shaped tool result** (`RefusedConfabulated(Refused)`, audit
  `is_error=0`), never `RefusedInternal`. Rejected the egress taint (29 dials carried no user data; the taint is the
  attack-in-progress signal) and rejected raising out of `tool.run` for `is_error=True` (six files of churn; the audit
  `result_digest` is what the criterion requires).
- 2026-10-30 — BK-023: the `?recoverdebug` capture **collapses no-op `endThinking` runs at render** (key = name + state +
  rendered frames → one `first-last … ×N` line, merged across the 2 s pump drains) — rejected dropping them at the `tlog`
  push: the raw `_thinkLog` keeps every entry (probe G counts raw start/end entries, and a no-op close's stack names its
  caller). Capture line ordinals went with it; the leading stamp is the position marker. See learnings 2026-10-30.
- 2026-10-30 — BK-023: trace entries carry **event time stamped AT PUSH as `e[3]`**, the stack held at index 2 with an
  explicit `null` pad — rejected drain-time stamps (a 2 s drain cannot separate a paced stream from a catch-up burst,
  which is the criterion) and reordering the tuple (`probe.js` and the capture read `e[0..2]` positionally). See learnings 2026-10-30.
- 2026-10-30 — gotchas.md stays **one file**; the cutback evicts by kind (dead decisions, test-guarded gotchas) —

  rejected splitting gotchas into per-area files loaded on demand (Tom's call: a trap met outside its area would be out of view).
- 2026-10-30 — BK-022's on-device recovery trace arms from a **second manifest's `start_url` (`/?recoverdebug=1`)** behind
  a static launcher page — rejected a stored/tab-set flag: Home-Screen apps are storage-isolated from Safari (webkit 181849).
- 2026-10-30 — BK-019: the chat-run phase is **derived, never stored** (`runstate.js derivePhase`); `done`/`stopped`/
  `error` are transitions, not held phases — rejected a stored `ui.phase` (fixtures arm `ui.resync`/`liveTurn` directly,
  so it desyncs on the first arm) and holding `done` (a late `title` must still land). See learnings 2026-10-30.
- 2026-10-30 — `runstate.js` owns slot **existence and identity**; `replay.js` keeps the per-slot dedupe arithmetic —
  rejected absorbing replay's watermark logic (~270 lines; fixtures write `ui.liveSeq` directly) and a stored-effects
  queue (transitions RETURN effect tokens; the router applies them). See learnings 2026-10-30.
- 2026-10-30 — the `thinking`/`pills`/`transcript` cycle breaks by **injection wired in `transcript.js`'s module body** —
  rejected re-export indirection (cycle stays live), a `bubble.js` extraction (can't break `thinking↔pills`, moves
  probe-pinned code) and hoisting close calls to callers (probe F pins `endThinking`'s stack). See learnings 2026-10-30.
- 2026-10-30 — the JS unit tier runs the **real webui modules in cached headless Chrome over `http.server`** via
  `tests/test_jsunit.py` — rejected installing node/bun (none on this Mac) and browser-only lifecycle tests (~0.65 s/test
  keeps it affordable offline). See learnings 2026-10-30.

- 2026-10-29 — BK-017 **amends** the 2026-09-28 gesture-only unpin: follow.js also unpins on an upward scroll that is
  not an app write (±2 px) and lands `gap > 1` (keyboard/scrollbar fire no gesture) — rejected unpin on ANY upward
  scroll (a follower unpins at every thinking-strip collapse). See learnings 2026-10-29.
- 2026-10-29 — BK-017 read-position preservation is **two-branch** (keep `scrollTop` if ≤ the new `maxTop`, else restore
  the pre-change gap), gated by `isFollowing()` captured BEFORE the change — rejected pure distance-from-bottom (blind to
  viewport clamps) and pure gap-preservation (drags a reader 47–71 px per collapse). See learnings 2026-10-29.
- 2026-10-29 — `#chat-log { overflow-anchor: none }` **kept though not load-bearing** — rejected removing it (a gap-0
  unpinned view still gets carried down and read as intent) and a ResizeObserver net (sees only the clamped value).
  See learnings 2026-10-29.
- 2026-10-25 — Background runs ride the **chat lane**, not code: a chat waits behind a run's in-flight call — rejected
  keeping code (cuts + 300s holds stalled runs; preemption let a chat slip into a live run's session). See learnings 2026-10-25.
- 2026-10-25 — BK-001 tool-result images, `view_page` and a scheduled job's vision verdicts **reverted** (too heavy for
  the local model; BK-003 stayed); its ruling stands: bounded jail writes are MUTATING_LOCAL, never READ_ONLY. See learnings 2026-10-25.
- 2026-10-24 — Backlog work runs as **one supervised party run** (fighter builds, cleric reviews each, checkpoint commit
  per item) — rejected an autonomous scheduled loop: unattended code + destructive tools, and review can't stay honest.
- 2026-10-24 — **`scratch/backlog/` is the backlog SSOT** (its own git repo); `todo.md` went to `Parked.md` — rejected
  a parallel `todo.md` (two sources of truth) and filing it in Archive.md (shipped/closed items only). See learnings 2026-10-24.

- 2026-10-22 — Compaction summarizer **thinks on `low` with a fenced transcript** (lead-in + closing "now write the
  summary") — rejected raising effort (the framing was broken, not think depth) and softening the cut (gate-gaming). See learnings 2026-10-22.
- 2026-10-22 — **No deterministic output guards on the compaction summary** (Tom) — the eval measures instead; rejected
  a verbatim-coverage guard: the parrot was confabulation, not copy. See learnings 2026-10-22.
- 2026-10-22 — Compaction output **10k think + 10k text** (`COMPACTION_MAX_TOKENS` 20k, parameterized `answer_headroom`)
  — rejected 14,096 with the stock reserve: only 4,096 of text, so a ceiling summary truncates silently. See learnings 2026-10-22.
- 2026-10-22 — An **implausibly short summary is a failed summary** (< max(200 chars, 2% of band target) → failed path,
  no retry) — rejected persisting it as success: a frozen `ack` folds forward forever. Pinned test_echoed_instruction_summary_persists_as_failed.
- 2026-10-22 — Compaction STILL rides the -np 1 llama slot on a **corrected premise**: persisted checkpoints make it one
  cold prefill per budget crossing — rejected moving it off-server; supersedes 2026-09-30's premise. See learnings 2026-10-22.
- 2026-10-22 — Compaction is **persisted** (`session_checkpoints`, migration 015) and resume rebuilds it byte-identically —
  supersedes 2026-06-12's in-memory half; the full-history `messages` half stands. See learnings 2026-10-22.
- 2026-10-21 — A **failed summary is persisted too** (`summarized=0`), no retry — rejected skipping the write: it
  resurrects the per-turn re-summarize loop on a transient outage. Pinned by a test.
- 2026-10-20 — A doc upload needs a **claimed extension matching its magic bytes** — rejected a magic-only sniff: it flips
  `test_post_rejects_non_image_with_415` to 200, and only the claim fixes the suffix `read_document` dispatches on.
- 2026-10-20 — The original filename lives in an `uploads/<hash>.name` **sidecar, re-sanitized on read**, not copied by
  `copy_file` — rejected embedding it in the id (widens the traversal regex, breaks hash dedupe). See learnings 2026-10-20.
- 2026-10-20 — The document hint carries the **absolute** jail path — rejected the relative form (file tools anchor to
  daemon cwd); cost: the host path reaches the opt-in Sonnet lane.
- 2026-10-20 — `copy_file` **refuses secret-class sources outright** rather than serving them and tainting —
  rejected the `read_file` taint route: egress taint is name-based and would not follow copied bytes.
- 2026-10-19 — Doc attachments (pdf/docx/xlsx/txt, 20 MB, no OCR) are read **on demand** via a path hint +
  `read_document` — rejected inline extraction at upload (a 200-page PDF eats the window, can't be paged) and OCR tooling.
- 2026-10-19 — Doc saving is **chat-driven** (scratch copy / `drive_upload`); uploads land in `scratch/uploads` —
  rejected UI save chips (same deferral as images, 2026-10-14).
- 2026-10-19 — resume replays the **unpersisted tail**, joined at `ChatRun.persisted_seq` — rejected replaying the ring
  from 0: persisted tool steps re-paint, and rows carry no `seq` to dedupe against.
- 2026-10-19 — ring capacity **65,536** (~105 min, 2.1× the longest run), overflow reported as `gap` — rejected
  `QUEUE_MAXSIZE`'s 4000 (≈6.4 min; a lost mid-run head can't heal) and a byte cap. See learnings 2026-10-19.
- 2026-10-18 — Run identity: a slot vouches for exactly ONE run; a batch it can't vouch for COLD-JOINS — rejected
  gap-fill heuristics (seq restarts per run; a guessed mark is token loss). Pinned probe_live (see learnings 2026-10-18).
- 2026-10-18 — System-prompt trim (option A): file/sandbox tools defer to a group, web tools stay native, tool guidance
  lives only in descriptions — rejected deferring web tools (most-used). *Superseded in part:* notify stays native. See learnings 2026-10-18.
- 2026-10-18 — Recall reinforced for OPS LESSONS (recall before a repeat task; `remember` the method after a
  multi-attempt one) — rejected leaving saves to the extractor alone.
- 2026-10-18 — PWA "accurate on reopen" = **server contract first** (seq + per-run ring + projection, then one client
  resync) — rejected the advice's literal order (re-learns the reverted lossy re-sync) (see learnings 2026-10-18).
- 2026-10-17 — iOS PWA resume flash: **atomic re-render only** (fetch-then-swap) — rejected re-sync-on-first-recovered-
  event (built, REVERTED: it wipes the partial head). *Amended 2026-10-18:* only STORE-sourced mid-call re-sync is banned.
- 2026-10-16 — Settings panes keep **one vertical scroller** (`.cards`) on its own merits, NOT as the phone-swing fix
  (that was horizontal overflow; the "nested touch layers" theory was disproven on device). See learnings 2026-10-16.
- 2026-10-14 — Attachment thumbs load same-origin `/media/<id>`, not `blob:` URLs (WebKit CSP `img-src 'self'` rejects
  `blob:`); cost: a placeholder during upload (see learnings 2026-10-14).
- 2026-10-14 — Image attachments v1 = attach + see + respond (picker, removable chips, uploads kept on disk as path refs)
  — rejected Drive-save, reverse-search and a prune job as premature. See learnings 2026-10-14.
- 2026-10-12 — reddit_comments reads the post's .rss Atom feed over the .json API (403s
  non-browser clients) — rejected curl_cffi spoofing, an OAuth app, redlib mirrors; cost: no scores (see learnings 2026-10-12).
- 2026-10-10 — Apple FM triage refusals fixed by a named `<email>` tag + one free-text retry,
  over permissive guardrails (no effect) or restoring the cloud fallback (see learnings 2026-10-10).
- 2026-10-10 — Always-on profile/preference facts move to the END of the system prompt, FROZEN per
  session in sessions.memory_profile — rejected per-run rebuild (re-prefills) and an in-memory map (see learnings 2026-10-10).
- 2026-10-02 — Cloud fallback tier DELETED: local lane mandatory, Sonnet 5 a per-chat opt-in (one
  picker, effort medium, no tool groups) — the silent ladder made cloud the invisible default (see learnings 2026-10-02).
- 2026-10-02 — Split every file past ~400 lines, pinned by cap tests at 420 (one exemption) — the
  monoliths were where bugs hid; the number is a tripwire, not the goal (see learnings 2026-10-02).
- 2026-10-01 — Chat responsiveness: 15s heartbeat, live status row, reconnect pill, title at turn
  start, extraction off the critical path, explicit Stop; opencode contention surfaced, not fixed (see learnings 2026-10-01).
- 2026-09-30 — Compaction may use the -np 1 llama server; extraction stays off it (fires every turn) (see learnings
  2026-09-30). *Superseded 2026-10-22:* the "prefix is lost anyway" premise was wrong; the practice stands.
- 2026-09-28 — Cutover to the Mac mini done; WSL files deleted with a recovery note (docs/macos-host.md
  "Formerly WSL") rather than kept as fallback (see learnings 2026-09-28).
- 2026-09-28 — "Feel" UI choices (scroll, pacing) settled with a live slider demo on the iPad, not
  prose — the chosen slider values became the constants (see learnings 2026-09-28).
- 2026-09-28 — Chat follow = spring scroll + client-paced reveal + "↓ Latest" pill, unpin on gesture —
  rejected instant snap (jumps) and smooth scrollTo (stutters).
- 2026-09-28 — Mic shows only when the server has Whisper on (HARNESS_STT) — rejected deleting STT
  (may return) and hide-only (Whisper loaded for nothing).
- 2026-09-28 — Text pacing lives in each CLIENT, not a server smoothStream — server delay would drag
  through detach/replay. Cost: ~30 lines per client.
- 2026-09-28 — Tool groups (load_tools + call_tool, in-conversation) revisit search_tools for local
  prefill cost, not discovery; call_tool over native names (0/6 vs 6/6); profiles stay closed (see learnings 2026-09-28).
- 2026-09-28 — Main model = local Qwen via the fork's Anthropic-shaped /v1/messages as a base_url'd
  AnthropicProvider, not a new class; side calls off the lane. *Amended:* utilities → Apple FM (see learnings 2026-09-28).
- 2026-09-06 — Mac mini direction: Swift = thin client only (no harness rewrite), voice = STT only,
  STT server-side, inference server by benchmark, ONE resident Qwen + per-run effort (see learnings 2026-09-06).
- 2026-08-11 — search_tools + delegate RETIRED for disuse; with them went the soft-vs-hard profile
  split (search_tools was activate()'s only caller) — one closed profile concept (see learnings 2026-08-11).
- 2026-08-04 — Social cross-posting RETIRED from the harness: a standalone tool already
  does it; porting it re-added injection machinery for an identity transform (see learnings 2026-08-04).
- 2026-07-23 — Secret taint stays NAME-pattern based (innocuous-named sensitive files don't taint):
  broadening would strip egress on every CSV read; the destination gate blocks the send anyway (see learnings 2026-07-23).
- 2026-07-23 — Extraction: confidence replaced by source + horizon (strict); extractor sees recent
  memory; supersedes is TRACE-ONLY — Tom rejected auto-forget (see learnings 2026-07-23).
- 2026-07-18 — Memory categories expanded to nine (all new ones recall-only) over raising the
  always-on cap — the tier was polluted by an undefined enum (see learnings 2026-07-18).
- 2026-07-17 — Cron runs send no completion push (noise); watcher, notify-tool and approval pushes
  stay; a job that must alert calls notify. Removal, not a toggle (see learnings 2026-07-17).
- 2026-07-17 — Lane B v3: envelope-only (no per-action mode), identity refs in ClientConfigStore,
  child on the main tier, budget counts page ACTIONS, approved boundaries audit "approved" (see learnings 2026-07-17).
- 2026-07-14 — Open-loops sweep ROLLED BACK: background extraction into tables lost to the model
  reading Gmail live; closure detection is where it died (see learnings 2026-07-14).
- 2026-07-14 — Calendar outward = SEPARATE OUTWARD tools (attendees, respond) over attendee params
  on create/update; delete is DESTRUCTIVE with a notify flag (see learnings 2026-07-14).
- 2026-07-14 — Approval context = server-side live resolution at review time (gateway/preview.py),
  never model-supplied labels (injectable) (see learnings 2026-07-14).
- 2026-07-12 — Approval pushes suppressed SERVER-side when a client is visibly in-app, not in the
  service worker (iOS penalizes silent pushes); no safety-net re-push (see learnings 2026-07-12).
- 2026-07-07 — Scheduled jobs: AUTONOMOUS honors the GLOBAL allowlist; rows are self-contained
  (playbooks = templates); schedule_* always-ask, never DESTRUCTIVE. *Superseded in part:* 2026-08-11 (see learnings 2026-07-07).
- 2026-07-03 — Web search = Brave in-process tool + keyless web_fetch over Anthropic's server tool
  (bypasses policy/audit, provider complexity, ~$10/1k) (see learnings 2026-07-03).
- 2026-06-28 — Irreversible (OUTWARD/destructive) approvals use a confirm modal, never a press-and-
  hold bar — a background poll re-rendered the bar and detached its cancel path (see learnings 2026-06-28).
- 2026-06-28 — memory_hygiene is deliberately TWO-PHASE (propose, Tom replies, then apply) — the
  split IS the review gate; don't fold it back into one message (see learnings 2026-06-28).
- 2026-06-19 — Google scopes broadened to full per-service (gmail.modify ceiling): the policy engine,
  not the scope, is the boundary; ends the per-tool re-auth treadmill (see learnings 2026-06-19).
- 2026-06-19 — In-app vendor OAuth (/api/oauth/{vendor}/start|callback, Web-app client) over
  SSH copy/paste; no token-holding aggregators (see learnings 2026-06-19).
- 2026-06-16 — Allowlist eligibility = MUTATING_LOCAL only, so OUTWARD sends can never auto-fire;
  inviting is separate OUTWARD from own-calendar events.
- 2026-06-16 — calendar_event_create idempotency = client-side id hashed from (summary,start,end) —
  self-contained, crash-retry 409s instead of double-booking.
- 2026-06-16 — Interactive chat stays on the full catalog (no write profile) — no cache benefit for
  the maintenance (see learnings 2026-06-16).
- 2026-06-12 — Gmail triage failure raises mail as ACTIONABLE (fail-visible) over dropping it —
  extra runs are bounded; silently lost urgent mail isn't (see learnings 2026-06-12).
- 2026-06-12 — ms365 connector PAUSED by Tom: keep enabled=false, don't "finish" it unprompted
  (see learnings 2026-06-12).
- 2026-06-12 — delegate sub-runs link via trigger "delegate:<run_id>" over a parent_run_id migration
  (migrations auto-apply to the live db) (see learnings 2026-06-12).
- 2026-06-12 — Compaction is in-memory only; the messages table stays full history — rewriting it loses audit and
  breaks resume. *Superseded in part 2026-10-22:* compaction is persisted; the append-only messages rule stands.
