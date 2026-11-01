# Gotchas

Traps no code owns — tooling, host ops, environment, cross-cutting process. Read before your first edit each
session; 1–2 lines each, capped at 8,000 chars by `/party:long-rest`. Traps that live in code are comments and test
failure messages there now; full text of anything moved or shortened: .claude/learnings-archive.md (grep the date).

## Store, data, live daemon

- Any harness command opening the live harness.db applies pending migrations — scratch
  HARNESS_DB_PATH for CLI smoke; back up with raw `sqlite3 .backup` (see learnings 2026-07-17).
- A trigger row with an INLINE prompt holds the ONLY copy of it (`triggers.playbook`; no history, and backups
  restore the whole db) — edit via a builder that asserts anchor uniqueness and diffs against a saved read of
  the row (filed BK-012).
- A chat beat runs the BASE chat prompt (paste deployed rule bytes, never paraphrase) plus an auto-injected `<memory>`
  block that can hand an "un-premed" proof — or a recall check — its own answer: verify `messages.idx=0` (see learnings 2026-10-25).
- A live DB's mtime moving during a scratch smoke is NOT contamination (the Gmail watcher writes `watch_state` on its
  120s cadence) — prove isolation from artifacts (scratch log's `listening on` port, row counts), never mtimes. (see learnings 2026-10-19).

## Host ops & deploy

- Deploy = `launchctl kickstart -k gui/501/com.harness.agentd` ONLY — never kill the shared :8080 llama server — and
  only when no run is active: a kickstart KILLS an in-flight chat run unrecoverably (see learnings 2026-10-10).
- A chat run DETACHES on client disconnect: a curl `-m` timeout leaves it RUNNING and holding the
  shared :8080 slot — free it with `POST /api/chat/{sid}/stop`, confirm via `lsof :8080` (the runs.status row can stay 'running') (see learnings 2026-10-14).
- Scratch agentd: HARNESS_LOCAL_URL=http://127.0.0.1:9 (empty exits; the real one reaches the
  live model); never `pkill -f agentd` (matches the live daemon) (see learnings 2026-10-13).
- Concurrent runs spawn SAME-NAMED probe servers (`orphan_server.py`) from sibling worktrees: stop an
  ad-hoc one by its own PID (the `$!` you spawned), never `pkill -f <script>` — it matches the other run's
  server too. `common.spawn_server` terminates by PID for this reason.
- A real-socket smoke needs a model that CLOSES: `HARNESS_LOCAL_URL=http://127.0.0.1:9` fails calls in ms (an ACTIVE
  `/live` payload is undemonstrable with it — fake Anthropic-SSE upstream instead); the fake must close-frame (HTTP/1.0)
  after `message_stop` or the SDK hangs with everything parsed. (see learnings 2026-10-19).
- Measure prompt-shape changes with the fork's POST /v1/messages/count_tokens; a prefix gain is
  invisible on a cold start — confirm a warm bookmark (/slots) first (see learnings 2026-10-10).
- Headless Mac: strip com.apple.quarantine before an app's first launch, else it freezes at
  _dyld_start; `sudo killall syspolicyd` clears a stuck verdict (see learnings 2026-09-30).

## Tooling

- No node on the Mac: syntax-check webui with jsc `checkModuleSyntax` or tree-sitter (esprima chokes);
  rm is denied — retire files with a pointer stub (see learnings 2026-10-02).
- Headless browser check, no Review pane: serve the scratch dir over `http://` (ES modules reject `file://`) and drive
  the CACHED `~/Library/Caches/ms-playwright/*chrome-headless-shell*` — `--dump-dom` needs `--timeout`; async asserts
  need `--virtual-time-budget=6000` (rAF callbacks NEVER fire under it — flush rAF-paced UI via its own finalize). (see learnings 2026-10-16/17).
- `DOM.setFileInputFiles` (CDP) drives the real upload path on a HIDDEN file input, no `Runtime.enable` and no product
  change; for pure render checks skip the daemon — `dom.js` + the module under test beside a probe page on `http.server`. (see learnings 2026-10-20).
- `scratch/backlog`: take the next BK id from Board ∪ Archive IMMEDIATELY before filing (chat filings land uncommitted);
  recount `Counts:` from headings; never infer ids from the off-by-one BK-02x log lines; log continuations are 29 SPACES
  (headers col 31) — edit against `cat -vet`, never a rendered read (see learnings 2026-10-25).
- A bare DOM scaffold for anything importing the transcript chain needs `#chat-form` as well as `#chat-log`:
  `follow.js` observes it at import, so without it the page errors and the module never runs — the output just
  sits at its sentinel (see learnings 2026-10-30).
- A scratch harness driver under `scratch/<x>/` resolves the repo root with `parents[2]` (the jsunit driver's
  `parents[3]` is depth-3): a wrong depth makes `os.symlink` succeed silently and `http.server` 404 the module —
  the symptom is "page never reported", not "file missing" (see learnings 2026-10-30).
- Probe row counts: `run_probe.py` prints per-engine uniques only BELOW its floor (parse the
  `===== engine =====` blocks to quote a number), and scroll_follow folds `[engine/subtag]` prefixes into one row —
  reuse the runner's own rule before quoting its floor (see learnings 2026-10-30).
- A `@pytest.mark.parametrize` set built by FILTERING a fixture **skips** when the filter empties, and a loop over an
  empty fixture list asserts nothing — both green and wired to nothing (BK-037 hit both). Pin the selection count at
  collection time before trusting any fixture-driven test.
- Moving a block between the board and `Archive.md`: dump codepoints first — `≠`/`…`/`–` render ambiguously in BOTH
  `cat -vet` and a rendered read, so either view can hide a transcription change in an append-only file.
- Table-fit probes: compare a long token against the cell's CONTENT width (column − cell/code padding, ~25 px in the
  chat bubble) or count the token element's own line boxes — comparing against the column width flips verdicts on a
  25 px budget (BK-015 measured a `false` read while the token already sat in 2 line boxes).
- Close-out ticks: a criterion's explanatory prose is a claim like its numbers — quote the artifact's field + engine,
  not a blended paraphrase (BK-015's Archive said "overflows into the wrap" where its own JSON said
  `scrollWidth == clientWidth`; corrections ride `## Log` lines, see ccc2100).
