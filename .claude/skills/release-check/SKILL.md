---
name: release-check
description: Use before restarting agentd after provider/agent-loop/policy changes
---

# Pre-release verification — guided workflow

"Release" = before restarting the daemon after a significant merge. No version scheme; the decision at the bottom (Open question 1) is Tom's call.

## Steps

1. **Pre-flight** — `git status` clean-ish, intended branch.

2. **Pending-migrations check** (fully mechanical):
   ```bash
   sqlite3 "file:$HOME/.local/share/harness/harness.db?mode=ro" "SELECT name FROM schema_migrations;"
   ls src/harness/store/migrations/*.sql
   ```
   Diff the two. If any pending (names in .sql but not the query output), **STOP**:
   - Take a raw backup **first** (`sqlite3 <db> ".backup <dest>"` — never a harness CLI command, which itself applies migrations):
     ```bash
     sqlite3 "$HOME/.local/share/harness/harness.db" ".backup /tmp/harness-backup-$(date +%s).db"
     ```
   - Verify integrity on the copy:
     ```bash
     sqlite3 /tmp/harness-backup-*.db "PRAGMA integrity_check;"
     ```
   - Get Tom's sign-off per CLAUDE.md's always-ask rule before proceeding.

3. **`uv run pytest`** — full offline suite green.

4. **CONFIRM WITH TOM**, then:
   ```bash
   uv run pytest -m live
   ```
   (Real Anthropic API; storeless agents; real spend.)

5. **CONFIRM WITH TOM**, then:
   ```bash
   uv run python scripts/smoke_test.py
   ```
   (Requires `scratch/hello.txt` intact; never in loops.)

6. **Apply migrations deliberately** (only if step 2 found pending ones and Tom signed off):
   The next live-DB open executes them — that's this step:
   ```bash
   uv run harness db backup
   ```
   Immediately re-run step 2's read-only query and confirm schema_migrations now lists the new files. A migration is a named event in the run log, not a side effect.

7. **Restart agentd** (docs/macos-host.md → Restart agentd) — verify `~/Library/Logs/harness/agentd.log` shows `listening` with no traceback. Spot-check the web UI loads.

8. **Report** — a one-screen summary: suites run, spend-bearing steps taken/skipped, migrations applied (with before/after schema_migrations names) or none, daemon state.

## Guardrails

- Never run steps 4–5 without the explicit confirm.
- Never rerun a red live test until it's green offline (each rerun costs money — diagnose offline first).
- The audit log is append-only — no cleanup ideas.
- Never open the live DB read-write before step 6.
