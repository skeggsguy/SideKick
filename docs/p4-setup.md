# P4 setup — long-term memory

P4 gives the harness durable memory: facts about you and episode summaries,
stored in `harness.db` (migration 002), recalled at the start of every
session, and written both by the agent (`remember`/`forget` tools) and by an
automatic post-turn extraction job running on the on-device Apple Foundation
Model (`providers/apple_fm.py`; no cloud call, no API key).

Everything works out of the box in **degraded mode** (keyword recall). For
semantic recall, install Ollama and the embedding model — one-time, ~270 MB.

> **Status (Mac mini, 2026-10-18): Ollama installed** (brew service,
> `nomic-embed-text`) — recall is semantic. See docs/macos-host.md.

## 1. Install Ollama + the embedding model (optional but recommended)

```bash
brew install ollama && brew services start ollama
ollama pull nomic-embed-text
```

So models unload when idle, set these in the Ollama service's environment
(or your shell profile when running `ollama serve` by hand):

```bash
export OLLAMA_KEEP_ALIVE=5m
export OLLAMA_MAX_LOADED_MODELS=1
```

No config change in the harness is needed — it probes `OLLAMA_HOST`
(default `http://127.0.0.1:11434`) on each embed call and falls back
gracefully when Ollama is down. Facts stored while Ollama was off are
back-filled with vectors at agentd start, by the next extraction run, or by
hand: `uv run harness memory reindex` (`--all` re-embeds every active fact —
only needed after changing the embedding model). Note: once any vector exists,
search is vector-only, so an unindexed fact is invisible until back-filled.

## 2. What runs where

| Job | Model | Notes |
|---|---|---|
| Embeddings (index + recall) | `nomic-embed-text` via Ollama, 768-d | no cloud fallback — mixed-model vectors would corrupt similarity |
| Post-turn fact/episode extraction | on-device Apple Foundation Model (`HARNESS_APPLE_FM=off` disables) | runs after every completed turn; failures are traced to `run_events`, never break the turn |
| Vector store | `sqlite-vec` in the main `harness.db` | vec tables are created at runtime by `MemoryStore.setup()`, not by migrations |

## 3. CLI

```bash
uv run harness memory list [--category preference] [--all]
uv run harness memory search "report deadline"
uv run harness memory forget <id>
```

Forgetting deactivates a fact (kept for audit); only its vector index entry
is deleted.

## 4. Verify (PLAN-v2 P4 checklist)

1. In a REPL session, state a preference ("I prefer briefings before
   7:30am") and finish the turn. `uv run harness memory list` shows an
   extracted `preference` fact.
2. Start a **fresh** session and ask for a briefing — the agent reflects
   the preference unprompted (the `<memory>` block is injected into the
   first user turn; check `run_events` for `memory_injected`).
3. With Ollama running, `uv run harness memory search "when does Tom like
   his morning summary?"` surfaces the fact despite no keyword overlap.
4. `uv run pytest tests/test_memory.py` — offline suite, includes
   vector-path tests (auto-skipped if sqlite-vec can't load).
5. Live recall check (manual, once — shares the -np 1 :8080 server):
   `uv run python scripts/recall_check.py` drives one real local-lane turn
   on a temp copy of the live facts and passes if the agent calls `recall`
   before its first `web_search` (nothing outward executes).
