# Architecture notes

Deeper wiring than the one-line layout map in CLAUDE.md. Keep curated and current —
read before planning or changing how parts fit. Remove entries that stop being true. Full text
of shortened notes: .claude/learnings-archive.md (grep the date).

- Every agent turn flows through core/agent.py: provider stream → tool dispatch via
  registry → policy check (core/policy.py) before execution → events traced to the store.
- File jail (core/paths.py): writes only inside allowed roots (default <repo>/scratch +
  HARNESS_SANDBOX_MOUNT; HARNESS_FILE_ROOTS overrides), enforced in the tools and the MCP
  server's _paths.py. Reads are global minus a secrets denylist. No host bash — sandbox_run is
  the only code execution, file-area roots mounted read-only (see learnings 2026-07-03).
- Thinking (Sonnet 5 opt-in): deltas stream via on_thinking_delta/done; raw blocks ride
  Message.content_blocks, verbatim within a tool loop, stripped cross-turn on cloud only.
  Params are model-gated; stop_reason max_tokens ends the turn via on_truncated (see learnings 2026-07-03).
- Effort (off|low|medium|xhigh): HARNESS_EFFORT is the default, run_turn(effort=) overrides per
  turn (web picker → /api/chat). HARNESS_LOCAL_URL builds the MAIN + compaction providers on the
  local lane (max_retries=0, 1800s, $0) with explicit chat_template_kwargs; no URL = exit.
  Utilities (triage, prediction, titles, extraction) run on Apple FM (see learnings 2026-09-28).
- Lanes: the fork treats requests without `X-Lane: code` as chat, which cuts code requests and
  holds them 300s. Every app agent rides CHAT — agentd's background runs too (2026-10-25), so a chat
  waits behind a run's in-flight call; the provider's code lane (cut re-send, LANE_CUT_RETRIES) stays
  a supported option no caller selects (see learnings 2026-09-28).
- The policy approver is an injected async callable; the CLI provides y/n/always.
  Non-interactive runs with no approver deny MUTATING_LOCAL/OUTWARD/DESTRUCTIVE.
- Durable state is one SQLite file (default ~/.local/share/harness/harness.db), schema
  in store/migrations/, applied in order, no ORM.
- Tools are async. MCP servers in mcp/servers.toml spawn over stdio at startup; each remote
  tool gets an ActionClass from config (unlisted ⇒ OUTWARD). MCPManager owns all connections via
  one AsyncExitStack (same-task open/close).
- Two composition roots call composition.build_agent(): main.py (CLI, interactive) and
  gateway/agentd.py (one agent per run, autonomy from the event, deferred approver wired in;
  supervised runs park gated calls on approvals rows for the web UI/push).
- Daemon flow: scheduler tick → CronFired → orchestrator (semaphore 2) → run. Completion push
  fires for WATCHER runs only; cron jobs alert via the notify tool; a cron/watcher run that RAISES
  pushes once ("<title> — run failed") and re-raises. agentd start marks dead non-CLI `running`
  runs rows `killed` (RunStore.sweep_orphans). Cron evaluates LOCAL time,
  timestamps stored UTC (see learnings 2026-06-12).
- Scheduled jobs are self-contained trigger rows (prompt + autonomy + tools); playbooks are
  creation-time templates; legacy name-rows resolve at fire via playbooks.resolve_job() —
  the ONE resolution path; bad autonomy downgrades to supervised (see learnings 2026-07-07).
- Autonomy matrix (policy.py): interactive (prompt + allowlist), supervised (deferred, no
  allowlist, DESTRUCTIVE denied), autonomous (allowlist over a CLOSED tool allocation, OUTWARD
  defers), restricted (read-only); ALWAYS_ASK_TOOLS (schedule_*) always park (see learnings 2026-07-07).
- Trigger management surfaces all share tools/schedule_tools.validate_job: schedule_* tools,
  REST /api/triggers, web UI Settings→Schedules, CLI trigger add.
- Live output: interactive chat streams on its own /api/chat SSE; a disconnect DETACHES (never cancels) and later
  events reroute, tagged session_id, via RunBroadcaster → /api/events (exactly one sink per event); background runs
  publish there too. `gw.busy_reason` holds both 409 guards (`active_chats`; `background_sessions` for sessions a
  cron/watcher run owns); Stop = POST /api/chat/{sid}/stop; publish() is sync, drop-on-full. See learnings 2026-07-13.
- Resume contract (pwa-accurate-resume): every chat-run event is stamped `seq` (per RUN — restarts at 1) + `run_id` in
  chat_run.py's `emit()` and recorded in a per-run `EventRing` (65,536); `GET /api/chat/{sid}/live?since=N` →
  `{active, seq, oldest_seq, gap, events}`, `gap` the only overflow detector; the ring dies with the run, and
  background events carry no seq. See learnings 2026-10-30.
- Resume read: `GET /api/sessions/{sid}/messages` → `{messages, live, compactions}` in ONE fetch; `live` is the ring
  joined at `ChatRun.persisted_seq`, so the tail holds only what the rows don't; liveness is one predicate in
  `gateway/live.py`. See learnings 2026-10-30.
- Web rendering: follow.js owns all chat-log scrolling; transcript.js paces text ≈0.6s behind arrival. Run lifecycle
  has ONE owner, webui/runstate.js (transitions RETURN effect tokens; app.js/events.js route and apply them; the phase
  is derived); the module graph is a DAG (pinned). resync.js holds the verdict half (T27 resyncVerdict + the
  transitions that ask it), writing only through runstate exports; watch.js is the observe timer. BK-034: a run
  another device started is the third Stop source (`ui.observed`), and an attached sender's run publishes nothing on
  /api/events, so the observer polls /live every 2 s. BK-036: a started stream's silent end arms the resync (T14 →
  T15), and a T27 live gap-fill for the screen arms Stop if no source holds it (T33). Tiers: tests/test_jsunit.py runs
  the real modules (DOM/rAF-free) in headless Chrome; probes are the browser tier. On-device debug: a debug manifest's `start_url` carries
  `?recoverdebug=1` (BK-022). See learnings 2026-10-30.
- Vendor OAuth: gateway/connections.py registry (per-vendor build_auth_url/exchange/status);
  /api/oauth/{vendor}/start|callback with single-use CSRF state; redirect = HARNESS_PUBLIC_URL;
  Google live, MS365 stub; the MCP server self-heals on the new token (see learnings 2026-06-19).
- Watchers: WatcherRunner tasks in agentd publish WatchHit; Gmail polls users.history.list
  every 120s from a watch_state cursor, triages on Apple FM, publishes urgent/actionable only;
  WATCH_PLAYBOOKS maps hit → playbook (supervised, push on completion). Code-registered with
  env toggles, not trigger rows (see learnings 2026-06-12).
- Memory: facts/episodes (migration 002); vec0 tables created at runtime. Recall = Ollama KNN
  or ranked keywords (no cloud embeddings). Always-on profile/preference facts freeze once per
  session into the system prompt's END (<user_profile>); similar-8 + live counts ride the first
  user turn's <memory>. MemoryExtractor (Apple FM) runs after each turn, never raises (see learnings 2026-06-12).
- Tool scaling: the registry is the full catalog; each Agent exposes an ActiveToolset. A
  profile (frontmatter/trigger tools) is CLOSED and turns grouping off. Without one, tool groups
  (core/tool_groups.py, default on locally) swap deferrable tools for load_tools/call_tool
  meta-tools; _dispatch unwraps call_tool first (see learnings 2026-06-12).
- Compaction (core/context.py): past ~100k est. tokens the context becomes frozen opening + a byte-frozen
  `<context_checkpoint>` summary + verbatim tail from a clean cut, persisted in session_checkpoints (migration 015) and
  rebuilt byte-identically on resume — the llama prefix cache survives, it fires again only after real growth, and
  messages stays full history. One-time cost ~2 min per crossing. See learnings 2026-10-22.
- P7: RunStore.summary feeds run_stats / `harness runs` / GET /api/runs/summary; store/backup.py
  snapshots daily (backup_loop never exits — supervise() treats exit as fatal); live eval tier =
  `pytest -m live` (see learnings 2026-06-16).
