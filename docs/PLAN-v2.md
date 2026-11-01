# Plan v2: Proactive Personal Productivity Agent

> Supersedes the original Opus plan (`docs/PLAN.md`, removed 2026-09-28 — `git show
> da386d5:docs/PLAN.md`). Phase 0 of that plan is built and almost all of its code survives
> into v2 — this is an evolution of the architecture, not a rewrite of the code.
>
> **Host note (2026-09-28):** written for WSL2 on a 16 GB / 8 GB-VRAM PC; the harness now
> runs on a Mac mini. WSL-specific sections are historical.
>
> **Model note (2026-10-02):** the tiering below (Sonnet main / Haiku utility /
> Ollama-first narrow jobs) was superseded: the local lane is now MANDATORY
> (Qwen main + compaction), the on-device Apple Foundation Model does the
> utility jobs, and Sonnet 5 survives only as a per-chat opt-in. See CLAUDE.md
> → Commands → Models. Sonnet/Ollama mentions are historical.

This document has three parts:

1. **Analysis** — what the original plan got right, and the eleven gaps that cap it.
2. **The v2 plan** — a from-scratch design for an event-driven, memory-equipped, proactive
   personal agent, scalable well beyond the original scope.
3. **Comparison** — side-by-side, with diagrams, and why v2 is the better foundation.

Decisions locked during v2 planning:

| Area | Decision |
|---|---|
| Integration order | **Google first**, incl. **Calendar + Tasks**; Microsoft 365 later behind the same adapter pattern |
| Remote channel | **Web UI served over Tailscale to the iPad** (PWA + Web Push) — promoted from "Phase 6, later" to the Phase 3 milestone |
| Existing code | Kept — Phase 0 core is refactored to async, not discarded |

---

# Part 1 — Analysis of the original plan

## What it gets right (all retained in v2)

- **"An LLM, a loop, and enough tokens"** — the inner agent loop, provider-normalized
  types (`core/types.py`), and the tool abstraction are clean and correct. Verified by the
  working Phase 0 implementation.
- **Interface-agnostic core** — the single most important property for everything v2 adds.
- **Hybrid tool architecture** — in-process core tools + MCP for integrations.
- **Safety taxonomy** — READ_ONLY / MUTATING_LOCAL / OUTWARD / DESTRUCTIVE, with the key
  insight that Docker sandboxes code execution while approval gates cover outward actions;
  they are complementary, not alternatives.
- **Pragmatic toolchain** — uv, Python 3.12 pin, official SDKs over hand-rolled clients.

## The gaps — each one is a scaling ceiling

| # | Gap | Why it caps the project |
|---|-----|------------------------|
| 1 | **Reactive-only.** Every interaction starts with you typing into a REPL. No scheduler, no triggers, no watchers, no background runs. | A productivity agent's value is mostly what happens when you're *not* at the keyboard — morning briefings, reminders, "an urgent email just arrived". The original architecture has no place to put any of that. |
| 2 | **No memory.** Nothing persists about you: preferences, projects, contacts, routines. | The agent re-asks everything forever. It is a "personal" agent in name only. |
| 3 | **Durable state deferred to Phase 5.** Sessions live in RAM; there is no storage for jobs, approvals, or audit history. | The scheduler, deferred approvals, memory, and audit log *all* require a database. Deferring it forces rework of everything built before it. |
| 4 | **No calendar, no tasks.** Gmail/Drive/Sheets/Docs only. | A productivity agent without calendar and task access is a mail client. |
| 5 | **The local/cloud heuristic router is the weakest idea in the plan.** *[Superseded 2026-09-06 for the Mac mini — a large local model now runs the main agent; see .claude/decisions.md.]* | An 8 GB-VRAM 4–8B model is an unreliable generic tool-caller; "route simple prompts locally" yields flaky tool calls and a strictly worse product. Local models excel at *narrow* jobs (embed, classify, summarize), not orchestration. |
| 6 | **Tool-count scaling ignored.** Google + M365 + memory + scheduler tools ⇒ 50+ schemas in every request. | Context bloat and relevance noise — and a varying tool list silently invalidates prompt caching. |
| 7 | **Single monolithic loop.** No sub-agent delegation. | Long research or migration tasks monopolize a single context window. |
| 8 | **No context management.** No compaction for long sessions. | Long-running sessions die or degrade. |
| 9 | **No streaming, no prompt caching.** | Worse interactive UX, and 3–10× higher input-token cost once tool schemas + memory grow the prompt prefix. |
| 10 | **No observability or evals.** No run traces, no audit trail, no regression suite. | Undebuggable as it grows — and unacceptable once the agent acts autonomously. "What did it do while I was away?" must have an answer. |
| 11 | **Headless safety undefined.** Approval gates assume a human at the terminal. | A cron-fired run that hits an OUTWARD action has nowhere to put the prompt. Autonomy requires *deferred approvals*. |

**The pattern:** the original plan scales **tools** (more integrations on a chat REPL) but not
**behavior** (proactivity, memory, autonomy) or **operations** (durable state, observability,
cost). Those three are architectural properties — they must be designed in from the start,
because they cannot be retrofitted cheaply.

---

# Part 2 — The v2 plan

## Architecture

One long-lived asyncio daemon (`agentd`) built around an **event bus**. Every way of invoking
the agent — you typing, a cron firing, a new email arriving, an approval being granted — is
just an event. Interactive chat is one event type among several; proactivity is not bolted
on, it is the same path.

```
   Interfaces                      agentd (one asyncio process)
 ┌─────────────┐  events  ┌─────────────────────────────────────────────┐
 │ CLI REPL    │────────▶ │              EventBus (asyncio)             │
 │ Web UI ◀────┼─Tailscale│                    │                        │
 │  (iPad PWA) │────────▶ │                    ▼                        │
 └─────────────┘          │              Orchestrator                   │
 ┌─────────────┐          │   event → AgentRun(session, autonomy,      │
 │ Scheduler   │────────▶ │   tool profile, model tier); max 2 conc.   │
 │  cron ticks │          │                    │                        │
 │ Watchers    │          │                    ▼                        │
 │  gmail/gcal │────────▶ │   Agent loop (async, streaming)            │
 └─────────────┘          │   provider ⇄ tools ⇄ PolicyEngine          │
                          │        │                │                   │
                          │        ▼                ▼                   │
                          │   SQLite store: sessions · runs · audit ·  │
                          │   approvals · triggers · memory (+vectors) │
                          └─────────────────────────────────────────────┘

   Models                          Tools
 ┌──────────────────────┐   ┌──────────────────────────────────────────┐
 │ Claude Sonnet —      │   │ in-process: files · bash · sandbox ·     │
 │   orchestrator       │   │   memory · schedule                      │
 │ Claude Haiku —       │   │ MCP client → google_workspace server     │
 │   cheap utility jobs │   │   (Gmail·Calendar·Drive·Sheets·Docs·     │
 │ Ollama (local GPU) — │   │    Tasks) → ms365 server (later)         │
 │   embed · classify · │   └──────────────────────────────────────────┘
 │   summarize ONLY     │
 └──────────────────────┘
```

Everything runs in one process: FastAPI rides on `agentd` via lifespan for the web UI and
health endpoints, the scheduler is an asyncio task, watchers are asyncio tasks. No message
broker, no worker fleet — right-sized for a single-user laptop, with the event bus giving the
*shape* that scales when more channels/triggers/integrations are added.

## Key subsystems

### 1. Event-driven core (`core/events.py`, `core/orchestrator.py`)

Event types: `UserMessage`, `CronFired`, `WatchHit`, `ApprovalDecision`. The orchestrator
turns each into an `AgentRun` carrying: a session, an **autonomy level** (§5), a **tool
profile** (§8), and a **model tier**. A semaphore caps concurrent runs (2) to bound RAM and
API spend.

### 2. Durable state first (`store/`)

Single SQLite file — `~/.local/share/harness/harness.db` — WAL mode, `aiosqlite`,
hand-written SQL migrations (no ORM). One file, one backup, and vectors live in the same
transactions as the facts they index.

```sql
-- 001_core.sql
CREATE TABLE sessions(id TEXT PRIMARY KEY, channel TEXT, title TEXT, created_at, updated_at);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, idx INT, role TEXT,
                      payload_json TEXT, created_at);          -- full Message serialized
CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT,
                  trigger TEXT,                                -- 'user' | 'cron:<id>' | 'watch:<id>'
                  channel TEXT, playbook TEXT,
                  status TEXT,                                 -- running | ok | error | killed
                  model TEXT, started_at, ended_at,
                  input_tokens INT, output_tokens INT, cache_read_tokens INT, cost_usd REAL);
CREATE TABLE run_events(id INTEGER PRIMARY KEY, run_id TEXT, ts, kind TEXT, data_json TEXT);
CREATE TABLE audit_log(id INTEGER PRIMARY KEY, run_id TEXT, ts, tool TEXT, args_json TEXT,
                       classification TEXT,                    -- READ_ONLY|MUTATING_LOCAL|OUTWARD|DESTRUCTIVE
                       decision TEXT,                          -- auto | approved | denied | deferred
                       result_digest TEXT, is_error INT);
CREATE TABLE approvals(id TEXT PRIMARY KEY, run_id TEXT, tool TEXT, args_json TEXT,
                       status TEXT,                            -- pending | approved | denied | expired
                       channel_ref TEXT, created_at, resolved_at);

-- 002_memory.sql
CREATE TABLE facts(id INTEGER PRIMARY KEY, content TEXT,
                   category TEXT,                              -- profile|preference|project|contact|routine
                   confidence REAL, source_run_id TEXT, active INT DEFAULT 1,
                   created_at, updated_at);
CREATE VIRTUAL TABLE vec_facts USING vec0(fact_id INTEGER, embedding float[768]);
CREATE TABLE episodes(id INTEGER PRIMARY KEY, run_id TEXT, summary TEXT, created_at);
CREATE VIRTUAL TABLE vec_episodes USING vec0(episode_id INTEGER, embedding float[768]);

-- 003_triggers.sql
CREATE TABLE triggers(id TEXT PRIMARY KEY,
                      kind TEXT,                               -- cron | watch
                      spec TEXT,                               -- '30 7 * * 1-5' | watcher name
                      playbook TEXT, channel TEXT, autonomy TEXT,
                      enabled INT, last_fired_at, created_at);
CREATE TABLE watch_state(watcher TEXT PRIMARY KEY, cursor TEXT, updated_at);  -- e.g. Gmail historyId
```

### 3. Memory (`memory/`)

```
  WRITE                                       READ
  ─────                                       ────
  mid-run: `remember` tool ──┐                run start:
  post-run: extraction job   ├──▶ facts  ──▶  always-on profile/preference facts (~600 tok)
   (Haiku/local model emits  │    episodes    + top-8 by vector similarity to the input
    facts + episode summary; │       │              │
    cosine-dedupe > 0.92)  ──┘       ▼              ▼
                              sqlite-vec      injected as a <memory> block in the FIRST
  hygiene: `forget` tool +    (768-d, local   USER TURN — never the system prompt, so the
  weekly merge/contradiction   nomic-embed)   cached prefix stays byte-stable
  cron playbook
```

Embeddings: `nomic-embed-text` via Ollama (768-dim, ~270 MB, graceful CPU fallback).

**Memory note (2026-10-10):** the split above is superseded — the always-on half now
renders once per session into a frozen `<user_profile>` section at the END of the
system prompt (`sessions.memory_profile`, NULL = legacy); only the top-8-by-similarity
half plus the live counts still ride the first user turn. See `.claude/decisions.md`.

### 4. Local model: demoted from router to utility worker (`providers/ollama_provider.py`)

The original plan's `router.py` is **not built**. The local model never runs the agent loop.
It exposes exactly three narrow functions, each with a cloud-Haiku fallback when Ollama is
off:

| Function | Used for | Why local wins here |
|---|---|---|
| `embed(texts)` | memory indexing/retrieval | free, private, tiny model |
| `classify(text, labels)` | email triage (urgent/actionable/newsletter/ignore) *before* anything reaches the cloud | privacy + cost: most mail never leaves the machine |
| `summarize(text, max_words)` | long tool outputs, episode summaries | low-stakes, latency-insensitive |

Escalation is **structural** (utility model → cloud orchestrator), not a heuristic guess
about prompt difficulty.

### 5. Safety for autonomy (`core/policy.py`)

The four action classes are kept and now declared on every `Tool`. New in v2: an **autonomy
level per run**, set by the orchestrator from the event that triggered it:

| Autonomy | Used for | READ_ONLY | MUTATING_LOCAL | OUTWARD | DESTRUCTIVE |
|---|---|---|---|---|---|
| `interactive` | CLI / web chat turns | auto | prompt y/n/always | prompt | prompt |
| `supervised` | cron / watcher runs | auto | deferred approval | deferred approval | **deny** |
| `restricted` | sub-runs, eval runs | auto | deny | deny | deny |

**Deferred approval** is the headless mechanism:

```
 cron run hits gmail_send ──▶ run PAUSES ──▶ approvals row (pending)
                                                  │
                              iPad push ◀── web UI approvals inbox
                                                  │  ✅ / ❌
 run RESUMES with tool executed ◀── ApprovalDecision event
 (or "denied by user" tool_result; 30-min timeout ⇒ denied)
```

The persisted allowlist ("always" answers, keyed by tool + arg-pattern) is consulted before
deferring — so after one approval, the morning briefing becomes fully autonomous. The Docker
sandbox (`docker run --rm --network none`, workdir-only mount) is retained for model-written
code.

### 6. Web UI over Tailscale (`gateway/`, `webui/`)

FastAPI mounted inside `agentd`: streaming chat (SSE/WebSocket), sessions list, **approvals
inbox**, runs/trace viewer, memory browser. Served via **`tailscale serve`** → automatic
HTTPS on the tailnet, zero public exposure, no port forwarding (everything is outbound or
tailnet-only — ideal under WSL2 NAT). HTTPS enables installing it as a **PWA on the iPad
with Web Push** (iOS 16.4+) for briefing and approval notifications. Frontend: one
lightweight page (htmx or Preact + Vite — final call at build time; no heavy framework).

### 7. Provider upgrades (`providers/anthropic_provider.py`)

`AsyncAnthropic`, `messages.stream()`, `cache_control` breakpoint after system + tools,
usage extraction on every response. Model tiers: `claude-sonnet-5` as orchestrator
(adaptive thinking on, effort-controlled), `claude-haiku-4-5` for utility jobs and sub-runs. Cache health is *verified*, not assumed:
if `cache_read_input_tokens` is zero on a repeat run, a silent prefix invalidator crept in.

### 8. Tool scaling (`core/registry.py`, `tools/tool_search.py`)

Three stages, applied as the tool count grows:

1. **Tool profiles** (Phase 3): each playbook/run gets a fixed, deterministic subset —
   a briefing run sends ~20 schemas, not 60. Deterministic per profile ⇒ cache-stable.
2. **`search_tools` meta-tool** (Phase 6): the long tail of rarely-used tools is discovered
   on demand and appended (preserving the cached prefix).
   **RETIRED 2026-08-11** — disuse; profiles are now always closed.
3. **Sub-run delegation** (Phase 6): a `delegate` tool spawns a Haiku child run with
   `restricted` autonomy and its own context window; the result returns as a tool_result.
   **RETIRED 2026-08-11** — disuse. (`browser_delegate`/Lane B is a different tool
   and remains live; historical `delegate:<id>` runs stay in the DB.)

### 9. Observability + evals (`store/`, `evals/`)

- `run_events`: full trace of every run (model calls, tool calls, policy decisions, errors)
  — `harness runs show <id>` and the web UI runs viewer.
- `audit_log`: append-only, separate from traces — answers "what did the agent *do* to the
  world, and who approved it."
- Per-run token/cost columns; a weekly cost-report playbook.
- `evals/`: pytest scenarios in two tiers — **offline** (policy engine, triage
  classification, memory retrieval over fixtures, watcher dedupe) and **live smoke**
  (`@pytest.mark.live`, run manually before releases).

## What happens to the existing code

| Existing file | Disposition |
|---|---|
| `core/types.py` | Kept; add `Usage` (token/cache counts) + message metadata |
| `core/registry.py` | Kept; add `profile(names)` for tool profiles |
| `core/agent.py` | Refactored to async + streaming + policy check in dispatch; loop logic unchanged |
| `core/session.py` | Refactored to be store-backed (same API) |
| `providers/base.py`, `anthropic_provider.py` | Made async + streaming + caching; message translation kept verbatim |
| `tools/base.py`, `tools/files.py` | Kept; add `classification` attribute + async `run` |
| `interfaces/cli.py` | Kept as one channel among several (`prompt_async`) |
| `main.py` | Split into `harness` (interactive CLI) and `gateway/agentd.py` (daemon) |
| `router.py` (planned, unbuilt) | **Dropped** |

## New module layout (delta to `src/harness/`)

```
src/harness/
  core/        orchestrator.py  policy.py  context.py  events.py
  store/       db.py  migrations/*.sql  runs.py  sessions.py  audit.py  approvals.py
  memory/      embeddings.py  store.py  extraction.py  retrieval.py
  scheduler/   scheduler.py  watchers/{base,gmail,gcal}.py
  gateway/     agentd.py  app.py   webui/ (static SPA)
  providers/   ollama_provider.py
  tools/       bash.py  sandbox.py  memory_tools.py  schedule_tools.py  tool_search.py
  mcp/         client.py  manager.py  servers.toml
  playbooks/   morning_briefing.md  email_triage.md  weekly_review.md
  evals/       runner.py  scenarios/
mcp_servers/
  google_workspace/  server.py auth.py gmail.py calendar.py drive.py sheets.py docs.py tasks.py
  ms365/             (later — same adapter pattern, MSAL device-code flow)
```

## Tech choices (opinionated)

| Decision | Choice | Why |
|---|---|---|
| Concurrency | asyncio end-to-end | Anthropic SDK, MCP SDK, FastAPI, pollers are all natively async; threads would add only bugs |
| Web layer | FastAPI + uvicorn inside `agentd`; `tailscale serve` for HTTPS | zero public exposure; HTTPS unlocks PWA + Web Push on the iPad |
| Scheduler | custom ~80-line tick loop + `croniter`; triggers in SQLite | APScheduler duplicates our state store and has awkward asyncio integration; we need custom sleep/catch-up semantics on WSL2 anyway |
| Vector store | `sqlite-vec` extension | ~zero RAM (critical at 16 GB); transactional with the data it indexes; brute-force KNN over <50k vectors is milliseconds — Chroma would be a second RAM-hungry process for nothing |
| Embeddings | Ollama `nomic-embed-text` | small (~270 MB), local, private, CPU-fallback fine |
| Local LM | `qwen3:4b`-class at Q4, **optional** | utility roles only; `OLLAMA_KEEP_ALIVE=5m`, `OLLAMA_MAX_LOADED_MODELS=1`; Haiku covers every job when Ollama is off |
| DB access | `aiosqlite` + raw SQL migrations | schemas are small and stable; no ORM weight |
| Google | own FastMCP server, official `google-api-python-client` + `InstalledAppFlow` OAuth | per the original plan — now including **Calendar and Tasks** |
| Gmail watcher | poll `historyId` delta every ~2 min | no public webhook endpoint needed behind WSL2/Tailscale |
| Cloud models | `claude-sonnet-5` orchestrator (adaptive thinking) / `claude-haiku-4-5` utility | cost/latency tiering with one provider implementation; updated 2026-07 from Sonnet 4.6 |

## Phasing — each phase independently runnable, visible value early

```
 P1 foundation      P2 integrations    P3 ★ MILESTONE        P4–P5 intelligence    P6–P7 scale
 ┌──────────────┐   ┌─────────────┐   ┌───────────────────┐  ┌────────────────┐   ┌──────────────┐
 │ async core   │──▶│ MCP client  │──▶│ agentd + scheduler│─▶│ memory         │──▶│ tool profiles│
 │ SQLite store │   │ Google MCP  │   │ web UI on iPad    │  │ gmail watcher  │   │ sub-agents   │
 │ streaming +  │   │ (read-first)│   │ MORNING BRIEFING  │  │ local triage   │   │ compaction   │
 │ caching      │   │ gmail/gcal/ │   │ deferred approvals│  │                │   │ M365 · evals │
 │ gates+sandbox│   │ tasks/drive │   │                   │  │                │   │ dashboards   │
 └──────────────┘   └─────────────┘   └───────────────────┘  └────────────────┘   └──────────────┘
```

- **P1 — Durable async core.** Async refactor of agent/provider/CLI; streaming; prompt
  caching; `store/` with migrations 001; session persistence + `harness --resume`;
  `policy.py` interactive gates; `bash`/`edit_file`/`sandbox_run` tools; run traces + cost.
  *Verify:* chat streams token-by-token; quit→resume keeps history; `edit_file` prompts
  y/n/always and the allowlist persists; a second identical run shows non-zero
  `cache_read_input_tokens`; sandboxed `curl` fails (no network).
- **P2 — MCP + Google Workspace (read-first).** MCP client validated against the reference
  filesystem server; `google_workspace` server with OAuth: `gmail_search/read`,
  `calendar_list/get`, `drive_list/read`, `tasks_list`; OUTWARD writes (`gmail_send`,
  `event_create`, `tasks_add`, …) gated.
  *Verify:* "what's on my calendar tomorrow and any unread important mail?" answered in the
  CLI; `gmail_send` to self fires an approval prompt.
- **P3 — Gateway + scheduler + web UI → Morning Briefing milestone.** `agentd` daemon:
  EventBus, orchestrator, tick scheduler + triggers table, FastAPI web UI (streaming chat +
  approvals inbox) over `tailscale serve`, iPad PWA + Web Push,
  `playbooks/morning_briefing.md`, `schedule_tools.py` (the agent creates its own
  reminders), autonomy levels live.
  *Verify:* `harness trigger add "30 7 * * 1-5" morning_briefing` → briefing notification on
  the iPad next morning (and immediately via `harness trigger fire`); "remind me Friday 4pm
  to submit the report" creates a trigger that fires; an OUTWARD action from a cron run
  appears in the approvals inbox and resumes on ✅.
- **P4 — Memory.** Ollama + nomic-embed-text + sqlite-vec + migrations 002;
  `remember/recall/forget` tools; post-run extraction; context injection.
  *Verify:* state a preference → a fresh session reflects it unprompted; `harness memory
  list` shows extracted facts; retrieval surfaces the relevant fact for a topical question.
- **P5 — Watchers + local triage.** Gmail `historyId` watcher (~2-min poll) → local-model
  triage → only urgent/actionable raises a run + iPad push; weekly memory-hygiene cron.
  *Verify:* self-sent "URGENT" mail → push within ~3 min; newsletters cause no run (check
  `run_events`); triage runs locally (GPU/CPU, not cloud).
- **P6 — Scale-out.** Tool profiles + `search_tools` and sub-run delegation (both
  **retired 2026-08-11**, disuse); client-side
  compaction in `context.py`; Microsoft 365 MCP server (read-only connector for a
  personal Microsoft account); richer web UI (runs + memory
  browsers).
  *Verify:* a profiled briefing run sends <25 schemas (trace); a long task spawns a visible
  sub-run with its own audit rows; a 100+-turn session compacts and stays coherent.
- **P7 — Polish.** Eval suite filled out; weekly cost-report playbook; DB backup job; docs.

## WSL2 / 16 GB RAM risks (historical — pre-Mac host)

1. **Memory pressure is risk #1.** `.wslconfig`: `memory=10GB`, `autoMemoryReclaim=gradual`.
   sqlite-vec instead of Chroma; `OLLAMA_KEEP_ALIVE=5m`, `OLLAMA_MAX_LOADED_MODELS=1` so the
   4B model unloads when idle. Budget: agentd ~150 MB, Ollama idle ~400 MB, peak ~3.5 GB
   with the 4B model loaded. Prefer docker-ce inside WSL over Docker Desktop (saves ~2 GB of
   Windows-side VM).
2. **Laptop sleep / clock drift.** The daemon isn't running with the lid closed, and WSL2's
   clock can jump on resume. The scheduler therefore does **catch-up**: on each tick, fire
   any trigger whose previous cron time > `last_fired_at` (one catch-up max per trigger,
   flagged `late=true` so a briefing can say "while you were away"). Auto-start agentd via a
   systemd user unit (WSL2 supports systemd) + Windows Task Scheduler at logon.
3. **Networking.** Everything is outbound or tailnet-only: Tailscale serve for the UI, Gmail
   polling (no webhooks), OAuth localhost redirect. FastAPI never binds a public interface.
4. **SQLite WAL.** Keep the DB on ext4 (`~/...`), never under `/mnt/c` (9p locking breaks WAL).

---

# Part 3 — Comparison: why v2 is the better foundation

## The shape difference in one picture

```
 ORIGINAL PLAN (request/response)         V2 (event-driven)
 ┌──────┐    ┌──────────┐                 ┌────────┐ ┌─────────┐ ┌────────┐
 │ You  │───▶│  Agent   │                 │  You   │ │Scheduler│ │Watchers│
 │ type │◀───│  loop    │                 └───┬────┘ └────┬────┘ └───┬────┘
 └──────┘    └────┬─────┘                     ▼           ▼          ▼
                  ▼                        ┌──────────────────────────────┐
             tools/models                  │   EventBus → Orchestrator    │
                                           │   → policy-gated agent runs  │
 Agent acts ONLY when you type.            │   → durable state + memory   │
 Web UI = "later". Memory = none.          └──────────────────────────────┘
 State = RAM.                              Agent acts FOR you, all day.
```

## Side-by-side

| Dimension | Original plan | v2 plan | Why it matters |
|---|---|---|---|
| Invocation | You type | Any event: message, cron, watcher, approval | Proactivity is the product, not a feature |
| Memory | None | Facts + episodes, vector retrieval, auto-extraction, hygiene | "Personal" requires remembering you |
| State | RAM until Phase 5 | SQLite from Phase 1 | Scheduler, approvals, audit, and memory all need it; deferring forces rework |
| Calendar / Tasks | Absent | Gmail + **Calendar + Tasks** + Drive/Sheets/Docs | The core productivity surface |
| Local model | Generic router (flaky tool-caller) | Utility worker: embed / classify / summarize; structural escalation | Plays to 8 GB-VRAM strengths; private + free email triage |
| Headless safety | Undefined | Autonomy levels + deferred approvals + allowlist + audit log | Autonomy without losing control |
| Remote access | Web UI at "Phase 6 (later)" | Tailscale web UI + iPad PWA at Phase 3, tied to the briefing milestone | Matches how it will actually be used |
| Tool scaling | One flat list forever | Profiles → search_tools → sub-agent delegation (the latter two retired 2026-08-11, disuse; closed profiles remain) | Survives 10+ integrations without context bloat |
| Cost / performance | No streaming, no caching | Streaming, prompt caching, per-run cost tracking, model tiers | 3–10× cheaper inputs at scale; spend is visible |
| Debuggability | None | Run traces, append-only audit log, eval suite | Required the moment it acts unattended |

## Why v2 wins

The original plan optimizes the **inner loop** — and gets it right; v2 keeps nearly all of
that code. But it scales along only one axis: more tools attached to a chat REPL. The
properties that make a personal agent genuinely cutting-edge — **acting unprompted,
remembering you, reachable from your iPad, auditable while autonomous** — are architectural
properties of the **outer loop**, and the original plan has no outer loop.

v2 makes exactly three foundational moves — the **event bus**, **SQLite-first state**, and
the **autonomy-aware policy engine** — which are the things that cannot be retrofitted
cheaply. Everything v2 drops (the heuristic router) or defers (M365, sub-agents, compaction)
is cheap to add later behind interfaces that already exist.

The practical difference: **a morning briefing pushed to your iPad by Phase 3**, instead of
a bigger REPL by Phase 6.
