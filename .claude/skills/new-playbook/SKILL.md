---
name: new-playbook
description: Use when creating a new scheduled job or playbook template
---

# Creating a scheduled job — guided workflow

## Steps

1. **Decide the shape** — one-off/simple reminder? Inline prompt on the trigger row, no playbook file. Recurring with structure? A playbook template in `src/harness/playbooks/`.

2. **State the model out loud** (this is the core teaching):
   - **Playbook .md files are creation-time TEMPLATES**. The schedule_add tool / web UI copy prompt+tools into a self-contained trigger row.
   - Editing the .md later does NOT change existing jobs (only legacy name-rows resolve at fire time).
   - A row's tools beat frontmatter. Point at `architecture.md`'s scheduled-jobs entry for full context.

3. **Write the template** (if needed): frontmatter `tools:` line (comma-separated, deterministic — copy `src/harness/playbooks/morning_briefing.md`'s shape). Prompt body in house style: tight, explicit tool instructions. Apply the `convert_time` batching rule for calendar-shaped operations.

4. **Choose autonomy** via the matrix (§2/§4 of decisions 2026-07-07):
   - **Supervised** is the default; every tool call asks unless allowlisted.
   - **Autonomous** requires an explicit tools list (the allocation is the job's boundary — every profile is a closed set, nothing can join it mid-run) and is a *per-job* user opt-in — ask Tom, never default to it.
   - Self-rescheduling jobs rely on schedule_* being always-ask, which works in every mode — don't request DESTRUCTIVE-style handling.

5. **Create the row** — via chat (schedule_add), web UI Settings→Schedules, or `uv run harness trigger add`. Cron specs are LOCAL time.

6. **Verify-fire routing** — `harness trigger fire` POSTs to `http://127.0.0.1:$HARNESS_PORT/api/triggers/{id}/fire` via whichever daemon owns that port. **Two sanctioned recipes:**

   **(a) Inline iteration** (live daemon vs scratch DB):
   ```bash
   # first stop the live agentd: Ctrl-C in its tmux session (docs/macos-host.md)
   export HARNESS_DB_PATH=/tmp/scratch.db
   uv run harness trigger add --prompt "..." --tools memory_tools  # CLI inline
   uv run harness trigger fire <id>  # fires inline, CLI prompts for approval
   ```

   **(b) Scratch daemon** (parallel to live 8787). It uses REAL models: .env's `HARNESS_LOCAL_URL` points it at the live llama-server (tool calls still go through the approval gate), and every request there evicts the live chat's single cached prefix (~30s re-read on the next chat turn). Prefix with `HARNESS_LOCAL_URL=` to use the cloud model instead:
   ```bash
   HARNESS_PORT=8899 HARNESS_DB_PATH=/tmp/scratch.db uv run agentd &  # background
   SCRATCH_PID=$!
   export HARNESS_PORT=8899  # CLI routes fires to scratch daemon
   uv run harness trigger add --prompt "..."
   uv run harness trigger fire <id>
   kill $SCRATCH_PID  # cleanup — never pkill by name: it also matches the live agentd
   ```

   **Never fire through the live 8787 daemon while "testing"** — use recipe (a) or (b).

7. **Token preflight** — if the playbook's tools read real accounts (Gmail/Calendar/Drive — READ_ONLY auto-runs even supervised), **CONFIRM WITH TOM** before any fire. Acceptance-firing a real job is his call.

## Guardrails

- Never mark a job autonomous without Tom's explicit opt-in.
- Never rely on editing a playbook .md to change an existing job.
- Never fire through the live 8787 daemon while testing — use one of the two recipes above.
- Watchers are code-registered, not trigger rows — don't create a watch-kind row expecting it to run.
