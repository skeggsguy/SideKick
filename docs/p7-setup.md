# P7 — Polish: cost report, DB backups, eval tiers, runs CLI

The last phase of [PLAN-v2](PLAN-v2.md). Nothing here needs new credentials or
services — it's all built on the existing store and daemon.

## Weekly cost report

`playbooks/cost_report.md` reads the agent's own spend via the new `run_stats`
tool (token/cost totals from the `runs` table, grouped by playbook or trigger
kind) and flags anomalies — error spikes, a runaway source, or a cold prompt
cache. Schedule it once:

```bash
uv run harness trigger add "0 8 * * 1" cost_report   # Mondays 08:00
```

It runs supervised like any cron playbook and lands as a push notification.
Ad hoc, you can just ask the agent "what did you cost me this week?" — the
`run_stats` tool is in the regular catalog.

## Runs CLI

The web UI's runs viewer now has a terminal twin:

```bash
uv run harness runs list                # recent runs: status, source, tokens, cost
uv run harness runs show <run_id>       # one run's header + full event trace
uv run harness runs summary [--days N]  # same aggregation the cost report uses
```

The gateway exposes the aggregation too: `GET /api/runs/summary?days=7`.

## DB backups

agentd snapshots the database daily via SQLite's online backup API — safe
while the daemon is writing, no downtime. Defaults:

- Destination: `<db dir>/backups/harness-<utc stamp>.db`
  (so `~/.local/share/harness/backups/`; override with `HARNESS_BACKUP_DIR` —
  keep it on local APFS, never iCloud Drive or a network path)
- Rotation: newest 7 kept
- Cadence: hourly check, snapshot when the newest backup is >23 h old
  (stamp parsed from the filename, so clock jumps after sleep and copies don't fool it)
- Disable with `HARNESS_DB_BACKUP=off`

Manual snapshot (e.g. before a schema migration):

```bash
uv run harness db backup [--dir DIR] [--keep N]
```

**Restore:** stop agentd (Ctrl-C in its tmux session — docs/macos-host.md),
copy the backup over the live file, start agentd again:

```bash
cp ~/.local/share/harness/backups/harness-<stamp>.db ~/.local/share/harness/harness.db
rm -f ~/.local/share/harness/harness.db-wal ~/.local/share/harness/harness.db-shm
```

## Eval tiers

Two tiers, per the plan:

- **Offline** (default): `uv run pytest` — fixtures and mocks only, no API
  calls, no real accounts. This is what runs all the time.
- **Live smoke** (manual, before releases): `uv run pytest -m live` — real
  Anthropic API via `tests/test_live_smoke.py`. Verifies streaming, tool
  dispatch, usage tracking, and that the prompt cache is actually warm on a
  second turn (a cold cache means a prefix invalidator crept in). Builds
  agents with no store, so it never touches the live `harness.db`.
  `scripts/smoke_test.py` remains as the quick single-check variant.

Live tests are deselected by `addopts = "-m 'not live'"` in `pyproject.toml`,
so plain `pytest` stays offline even without remembering the flag.
