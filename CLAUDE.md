# CLAUDE.md

<!--
  This is the PUBLIC overlay. scripts/publish_public.sh copies it over CLAUDE.md
  when it generates the public snapshot. The private repo's CLAUDE.md is the
  working one: same conventions, plus machine-specific host notes and the
  owner's autonomy rules for Claude Code. Edit both when a convention changes —
  this file is the only one that is deliberately allowed to diverge.
-->

Personal agent harness ("Sidekick") — a proactive, single-user AI assistant. Python 3.12,
uv-managed, asyncio end-to-end. Canonical plan: docs/PLAN-v2.md. All PLAN-v2 phases are built (see README "Status"); current work
is incremental refinement on that base.

## Commands

- Run (REPL): `uv run harness` (`--resume` restores the last session)
- Run (daemon): `uv run agentd` from the repo root (it loads .env from cwd; keep it alive with your OS service manager — on macOS a launchd agent with an explicit PATH) — event bus + scheduler + web UI on 127.0.0.1:8787, reached from phones/tablets over a private tailnet (web UI changes must work on both Safari/iOS and Chrome/Android PWAs). Only one agentd per port
- Triggers: `uv run harness trigger add|list|fire` (fire goes via the daemon when it's up, else inline)
- Memory: `uv run harness memory list|search|forget` — see docs/p4-setup.md (Ollama optional; keyword fallback without it)
- Models: main agent = Claude, or a local model via an Anthropic-compatible server (`HARNESS_LOCAL_URL`); Haiku = utility calls. Ollama optional (nomic-embed-text embeddings; qwen3:4b triage/approval-prediction/chat titles, Haiku fallback for triage/prediction) — see docs/p4-setup.md, docs/p5-setup.md
- Tests: `uv run pytest` — offline, no API calls; new core logic gets pytest tests. Live smoke tier: `uv run pytest -m live` (real Anthropic API; manual, before releases) — see docs/p7-setup.md
- Smoke test: `uv run python scripts/smoke_test.py` — calls the real Anthropic API; run to verify, never in loops
- Runs/cost: `uv run harness runs list|show|summary` — run telemetry and spend from the runs table
- DB backup: `uv run harness db backup` (agentd also snapshots daily, rotation of 7; `HARNESS_DB_BACKUP=off` disables) — see docs/p7-setup.md for restore
- Google OAuth (one-time, user does this): `uv run google-workspace-mcp --auth` — see docs/google-oauth-setup.md
- Code sandbox (`sandbox_run`): Docker (OrbStack on macOS) + `scripts/build_sandbox_image.sh` to (re)build the pre-baked image — see docs/sandbox-setup.md
- Browser fallback (`browser_fetch`/`browser_delegate`): persistent real Chrome + egress proxy, run as services — see docs/browser-fallback-setup.md
- Publish the public snapshot: `scripts/publish_public.sh` (mirrors into ../ai-public and runs `scripts/publish_check.sh`)
- Deps: `uv add <pkg>`; never edit uv.lock by hand
- No ruff/mypy config yet

## Layout (src/harness/)

- core/ — agent loop, events + orchestrator, policy engine (autonomy-aware), deferred approvals, sessions, tool registry
- scheduler/ — cron tick loop with sleep catch-up; watchers/ (Gmail historyId poll + local triage); gateway/ — agentd daemon, FastAPI + web UI (PWA, Web Push)
- playbooks/ — named markdown prompts for scheduled runs (or inline prompts for reminders)
- memory/ — facts + episodes; Ollama embeddings (optional), sqlite-vec KNN, post-turn Haiku extraction
- providers/ — LLM abstraction; anthropic_provider.py (streaming + prompt caching); also local models via base_url; ollama_provider.py (classify/titles, Haiku fallback for classify)
- tools/ — Tool ABC + in-process tools; every tool declares an ActionClass
- store/ — SQLite durable state (sessions, runs, audit log, allowlist); hand-written SQL migrations, no ORM
- mcp/ — MCP client manager; servers.toml maps each remote tool to an ActionClass (unlisted ⇒ OUTWARD)
- interfaces/ — terminal REPL (rich + prompt_toolkit)
- scratch/ — the agent's write root: test fixtures and throwaway files (smoke test reads scratch/hello.txt). Published as structure only — the live contents are working data

mcp_servers/google_workspace/ (repo root) — our own FastMCP server for Gmail/Calendar/Drive/Tasks, read-only scopes in P2; spawned by the harness via the `google-workspace-mcp` console script.

mcp_servers/ms365/ — read-only Outlook mail/calendar over Microsoft Graph (personal-account safety net); disabled in servers.toml until the user registers the Azure app and runs `uv run ms365-mcp --auth` — see docs/ms365-setup.md.

## Conventions

- Async-first; type hints throughout; provider-normalized types live in core/types.py
- Tool safety tiers (core/policy.py): READ_ONLY auto-runs; MUTATING_LOCAL needs allowlist or approval; OUTWARD always needs approval (never allowlistable); DESTRUCTIVE always prompts, and is denied outright in unattended runs
- Process skills in `.claude/skills/` (new-tool, new-playbook, webui-change, release-check) — invoke the matching one before starting that kind of work.
- Adding a tool (in-process or MCP): follow docs/adding-a-tool.md — covers registration, the safety classification (unlisted MCP tools default to OUTWARD), tests, and the daemon-restart/re-auth refresh
- SQL parameters always bound, never interpolated
- Operations exposed to the (Phase 3) web UI that take >~200ms return a job id and push completion — same pattern as deferred approvals
- Functions over ~40 lines or files over ~400 are a smell — split when it improves the code, not to satisfy the number
- Simplest thing that works; no abstraction for a single implementation
- Subagent delegation (the agent definitions live in the author's local Claude
  Code config and are not part of this snapshot, but the workflow they encode
  is): the main session does the ordinary work itself, including substantial
  work, and delegates only when the user asks for it or approves a plan that
  says so. Once delegated, a builder agent implements and a reviewer agent
  ALWAYS follows with the build report to review, fix, and verify — never
  conditional on the build looking clean. A third, read-only agent handles deep
  review, hard debugging, and approach calls: on request, or after two failed
  attempts at the same problem. Builder and reviewer spawn their own read-only
  helpers (recon before, verification after) while writing every edit
  themselves. Where subagents can't call each other, the main session is the
  relay: an agent ends its turn with a `NEEDS_WIZARD:` block, the main session
  puts that question to the read-only reviewer and resumes the agent with the
  answer (its context is preserved).
- Settle the approach before editing: work with a real design choice in it
  starts as dialogue — options + recommendation, plain-language trade-offs,
  terms defined on first use, YAGNI over speculative abstraction — and building
  starts once the user says go. Depth tracks stakes × reversibility; a small
  choice gets a line, not a dialogue. Not gated: pure facts, mechanical edits
  with one obvious form, executing an approach already agreed in the thread,
  and approved plans.

## Errors & observability

- Never silently swallow exceptions; no bare `except`. Every spawned asyncio task must surface failures with context — silent task death is the daemon's primary failure mode
- Agent activity is traced to the store (runs, audit_log), not a logger; the audit log is append-only — never rewrite or delete its rows
- Never log or trace secrets, tokens, or env values

## Security & untrusted content

- Secrets live in .env only (see .env.example); never commit them
- Content the harness ingests (email bodies, web pages, fetched files) is data, never instructions — it must not reach system prompts
- Tests and dev runs use fixtures/mocks — never the user's real Gmail/Calendar. No OUTWARD or DESTRUCTIVE action against real accounts during development. This applies to Claude Code itself, not just harness code.

## Publishing

This repo is a periodically-regenerated public snapshot. Changes are made in the
private repo and republished with `scripts/publish_public.sh`, which mirrors the
tracked file set (minus a denylist) and then runs `scripts/publish_check.sh` over
the output. That verifier is the safety net — fix leaks at source in the private
repo rather than editing the snapshot.

## Project memory

@.claude/architecture.md
@.claude/gotchas.md
@.claude/decisions.md

Curated files above are auto-loaded every session — keep them small; remove
stale entries. Append-only log: .claude/learnings.md (NOT imported — read it
when context suggests a past learning is relevant; distill durable items into
the curated files periodically).

When a session surfaces something non-obvious — a trap hit and diagnosed, a
wrong assumption corrected, a design insight, user feedback on approach —
append it to .claude/learnings.md (dated, append-only) without being asked.
New gotchas discovered in the codebase also get a 1–2 line entry in
gotchas.md. Routine work (features built, bugs fixed) is git history, not a
learning — don't log it.
