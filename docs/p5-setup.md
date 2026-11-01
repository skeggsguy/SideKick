# P5 setup — Gmail watcher + local triage

P5 makes the harness proactive about email: the daemon polls Gmail for new
mail every ~2 minutes, triages it **locally** (urgent / actionable /
newsletter / ignore), and only urgent or actionable mail raises an agent
run and an iPad push. Newsletters never cause a run, and most mail never
leaves the machine.

> **Status (2026-10-02): triage runs on the on-device Apple Foundation
> Model** (`providers/apple_fm.py`) — no Ollama and no cloud call needed.
> `HARNESS_APPLE_FM=off` disables it; Ollama's `qwen3:4b` remains the dormant
> alternative behind the same `Classifier` interface. See docs/macos-host.md.

## Ollama (optional, dormant)

- Only memory embeddings (`nomic-embed-text`) still want it; triage does not.
- Install: `brew install ollama && brew services start ollama`, with
  `OLLAMA_KEEP_ALIVE=5m` and `OLLAMA_MAX_LOADED_MODELS=1` in its environment.

## How the watcher works

```
agentd ── every 120s ──▶ users.history.list(startHistoryId=cursor)
                              │  new INBOX message ids (cursor in watch_state)
                              ▼
                    local triage (Apple Foundation Model, JSON-schema
                    constrained; never raises — last_error + skip on failure)
                              │  urgent | actionable          newsletter/ignore
                              ▼                                  └─ logged, no run
                    WatchHit on the EventBus
                              ▼
                    supervised run on playbooks/email_triage.md
                    (reads the full mail via gmail_read, judges again)
                              ▼
                    Web Push to the iPad
```

- First poll just records the current historyId — no backlog flood.
- A cursor Gmail no longer recognizes (history is kept ~a week) resets to
  "now" instead of failing.
- If both classifiers are unavailable, mail is raised as `actionable`
  (fail-visible, never silently dropped) and the error is in the daemon log.
- Disable with `HARNESS_GMAIL_WATCHER=off`; the watcher also stays off when
  there is no cached Google token.

## Weekly memory hygiene

`playbooks/memory_hygiene.md` runs Sundays 18:00 (a cron trigger):
lists all facts via `recall` (no query = list everything), merges
duplicates, forgets stale/contradicted facts, pushes a short summary.

## Verify (PLAN-v2 P5 checklist)

1. With `uv run agentd` running, send yourself an email with an urgent
   subject from another account/phone — within ~3 minutes the daemon log
   shows `[watcher:gmail] urgent (local): ...` and the iPad gets a push.
2. A newsletter arrival logs `newsletter (local)` and causes **no** run
   (no new row in `runs`).
3. `(local)` in those log lines is the proof triage ran on-box (Apple FM).
   `(cloud)` no longer exists as a fallback tier; a failed triage logs
   `triage failed` and raises as actionable (`(fallback)`).
4. `uv run pytest tests/test_watchers.py` — offline suite for classifier,
   watcher, and orchestrator wiring.
