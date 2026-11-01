<img src="assets/sidekick-mark.svg" alt="" width="124" align="left" hspace="16">

# Sidekick

A proactive, single-user AI agent: it chats when you want it to, and acts for you when you
don't — morning briefings, reminders, email triage, calendar awareness — reachable from a
terminal and from a phone or tablet. It runs as one asyncio daemon on a machine you own.

<br clear="left">

**This is a published personal system, not a product.** It is the assistant I actually run,
snapshotted into a public repo because the design is more interesting in the open than in a
private one. There is no five-minute quickstart, no hosted version, and no support. Read
["What running this actually takes"](#what-running-this-actually-takes) before you decide to
try it — the honest answer is that it wants a machine, not an afternoon.

It is also built almost entirely with [Claude Code](https://claude.com/claude-code), and the
repo is deliberately open about that: `CLAUDE.md`, `.claude/skills/`, and the append-only
`.claude/learnings.md` are the actual working files, not a tidied-up retrospective. (The
multi-agent definitions those files refer to live in local Claude Code config and are not
part of this snapshot.) If you are interested in what long-running agent-assisted
development looks like in practice, that is probably the most useful thing here.

MIT licensed. See [`LICENSE`](LICENSE).

## Status

All phases of [`docs/PLAN-v2.md`](docs/PLAN-v2.md) are built:
the event-driven daemon, Google Workspace integration, the web UI with deferred approvals,
long-term memory, the Gmail watcher with local triage, tool/context scaling, and the P7
polish pass (weekly cost report, daily DB backups, offline + live eval tiers). The
Microsoft 365 connector is built but paused (disabled in `mcp/servers.toml`). Setup guides
live in `docs/` (`p4-setup.md` … `p7-setup.md`, plus one per integration).

And as of 2026-10-02, **the harness runs on my hardware**: a local Qwen model drives every
chat and background run, the on-device Apple Foundation Model handles the small utility
jobs, and no API key sits on the critical path of an ordinary day. Claude Sonnet survives
as a deliberate, per-message opt-in for the turns a 64 GB Mac mini can't carry — one cloud
door, opened by choice, with everything else local.

## High-level design (v2)

```
   iPad PWA / phone ──┐                    terminal REPL ──┐
   (tailnet, SSE)     ▼                                   ▼
              ┌────────────────────────────────────────────────┐
              │                     agentd                     │
              │  web UI · event bus · orchestrator · scheduler │
              │  watchers (gmail) · deferred approvals · push  │
              └───────────┬──────────────────────┬─────────────┘
                          ▼                      ▼
                 ┌────────────────┐     ┌─────────────────┐
                 │   agent loop   │     │  SQLite (WAL)   │
                 │ policy engine  │     │ sessions · runs │
                 │ tools (MCP)    │     │ audit · memory  │
                 └───────┬────────┘     └─────────────────┘
                         │  one provider per job, per turn
        ┌────────────────┼──────────────────────┐
        ▼                ▼                      ▼
  local Qwen        on-device Apple FM     Claude Sonnet 5
  main + compaction triage · titles ·      the one cloud door,
  (llama.cpp fork)  predict · extract      opened per chat
```

- **Event-driven core:** every invocation — a typed message, a cron firing, a "new email"
  watcher hit, an approval decision — is an event into one orchestrator. Proactivity is the
  same code path as chat, not a bolt-on.
- **Durable state first:** one SQLite file (WAL) holds sessions, runs, audit log, approvals,
  triggers, and memory (vectors via `sqlite-vec`).
- **Memory:** persistent facts/preferences + episodic summaries, extracted post-turn by
  the on-device Apple FM and injected per run; keyword recall by default, vector recall if
  you stand up an embeddings server (Ollama `nomic-embed-text`).
- **Models, tiered, local-first:** the main agent and compaction run on a large local
  model served over an Anthropic-compatible API (`HARNESS_LOCAL_URL` — today Qwen
  3.8-Flash-Next on a llama.cpp fork); the on-device Apple Foundation Model does the
  narrow utility jobs (email triage, approval prediction, chat titles, memory extraction);
  **Claude Sonnet 5 is an opt-in chat model** for the turns the local lane can't carry —
  picked per message in the UI, never a silent fallback.
- **Integrations via MCP:** its own Google Workspace MCP server first (Gmail, **Calendar**,
  Drive, Sheets, Docs, **Tasks**) with official APIs + OAuth; Microsoft 365 behind the same
  adapter.
- **Interfaces:** a CLI REPL plus a FastAPI web UI served over a private tailnet (installable
  PWA with Web Push for briefings and approvals).
- **Safety:** action classes (read-only / mutating / outward / destructive) × per-run
  autonomy levels (interactive / supervised / restricted); **deferred approvals** let
  background runs pause and wait for a ✅ from a phone; a Docker sandbox for model-written
  code; an append-only audit log.
- **Toolchain:** Python 3.12 via `uv`, asyncio end-to-end, streaming + prompt caching,
  per-run cost tracking.

## What running this actually takes

The REPL starts with nothing but a local model server (no API key at all):

```bash
uv sync
echo 'HARNESS_LOCAL_URL=http://127.0.0.1:8080' > .env
uv run harness
```

Any Anthropic-compatible server works — a llama.cpp build exposing `/v1/messages`, or an
OpenAI-proxying gateway. The daemon treats `HARNESS_LOCAL_URL` as a hard requirement and
exits with guidance without it; the local model is the main agent, not a fallback tier.

Every integration below is genuinely optional — absent, the corresponding tools simply
don't appear in the catalog. But the *system* — the thing that wakes up and does something
useful before you ask — is the sum of them, and each is real setup:

| For | You need |
|---|---|
| The daemon, scheduler, web UI | A machine that stays on. Runs on a **Mac mini**; keeping the daemon supervised (launchd) is still on the to-do list. It started life on WSL2 under Windows |
| Reaching the UI from a phone | **Tailscale**, plus `tailscale serve` on the host. There is no public ingress and no auth story beyond the tailnet and a bearer token |
| Gmail, Calendar, Drive, Tasks | A **Google Cloud project** with OAuth consent configured and a **Web application** OAuth client — a genuine half-hour of console clicking (`docs/google-oauth-setup.md`) |
| Memory embeddings (optional) | An OpenAI-compatible embeddings server (e.g. Ollama with `nomic-embed-text`). Without it, recall is keyword-only |
| A local main model (**required**) | An Anthropic-compatible server (e.g. a llama.cpp build with `/v1/messages`) and `HARNESS_LOCAL_URL` |
| Email triage, approval prediction, chat titles, memory extraction | Nothing you install — the **on-device Apple Foundation Model** (`apple-fm-sdk`, macOS 26+ on Apple Silicon; `HARNESS_APPLE_FM=off` to disable) |
| The opt-in Sonnet chat model | An **`ANTHROPIC_API_KEY`**; without one the Sonnet picker option stays hidden |
| `sandbox_run` | **Docker** (OrbStack on macOS), plus a pre-baked image built once |
| `browser_fetch` / `browser_delegate` | A persistent real **Chrome** driven over CDP, behind a local egress proxy |
| Web Push notifications | HTTPS (the tailnet cert), a VAPID keypair the daemon generates, and a browser that will install the PWA |
| Outlook mail/calendar | An **Azure app registration** (disabled by default) |

Costs are real too — even when the money is electricity: every run is timed, tokenized,
and costed into the runs table. `uv run harness runs summary` exists because that mattered,
and it is what proved the local lane was worth building.

## Where to look

- [`docs/PLAN-v2.md`](docs/PLAN-v2.md) — the canonical design, phase by phase
- [`CLAUDE.md`](CLAUDE.md) — the layout map, conventions, and safety tiers
- [`.claude/learnings.md`](.claude/learnings.md) — an append-only log of things that went
  wrong and why; the least polished and most useful file in the repo
- [`.claude/gotchas.md`](.claude/gotchas.md) — the traps, each pinned by a test
- [`src/harness/core/policy.py`](src/harness/core/policy.py) — the safety model, if you only
  read one source file
