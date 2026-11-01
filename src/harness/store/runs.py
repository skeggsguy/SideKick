"""Run telemetry: one row per agent run + a trace of everything it did.

`runs` answers "what did it cost and did it succeed"; `run_events` answers
"what exactly happened, step by step".
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

from ..core.types import Usage
from .db import Database, utcnow


class RunStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def start(
        self,
        session_id: str | None,
        model: str,
        trigger: str = "user",
        channel: str = "cli",
        playbook: str | None = None,
    ) -> str:
        run_id = uuid.uuid4().hex[:12]
        await self.db.conn.execute(
            "INSERT INTO runs (id, session_id, trigger, channel, playbook, status, "
            "model, started_at) VALUES (?, ?, ?, ?, ?, 'running', ?, ?)",
            (run_id, session_id, trigger, channel, playbook, model, utcnow()),
        )
        await self.db.conn.commit()
        return run_id

    async def finish(
        self, run_id: str, status: str, usage: Usage, cost_usd: float
    ) -> None:
        await self.db.conn.execute(
            "UPDATE runs SET status = ?, ended_at = ?, input_tokens = ?, "
            "output_tokens = ?, cache_read_tokens = ?, cache_write_tokens = ?, "
            "thinking_tokens = ?, cost_usd = ? WHERE id = ?",
            (
                status,
                utcnow(),
                usage.input_tokens,
                usage.output_tokens,
                usage.cache_read_input_tokens,
                usage.cache_creation_input_tokens,
                usage.thinking_tokens,
                cost_usd,
                run_id,
            ),
        )
        await self.db.conn.commit()

    async def sweep_orphans(self) -> int:
        """Mark every 'running' row left by a dead daemon 'killed'; returns
        how many. agentd calls this once at startup, before anything can
        start a run: a process killed mid-tool never reaches agent.py's
        finish(), so its row would read 'running' forever. CLI rows are left
        alone — a REPL turn can legitimately span a daemon restart."""
        cur = await self.db.conn.execute(
            "UPDATE runs SET status = 'killed', ended_at = ? "
            "WHERE status = 'running' AND channel != 'cli'",
            (utcnow(),),
        )
        await self.db.conn.commit()
        return cur.rowcount

    async def list(self, limit: int = 50) -> list[dict]:
        # LEFT JOIN sessions so a chat run can show its (auto-named) title
        # instead of the bare "user" trigger. session.title stays the single
        # source of truth — a rename reflects here with no duplicated state.
        cur = await self.db.conn.execute(
            "SELECT runs.*, s.title AS session_title FROM runs "
            "LEFT JOIN sessions s ON s.id = runs.session_id "
            "ORDER BY runs.started_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def get(self, run_id: str) -> dict | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def summary(self, days: int = 7) -> dict:
        """Token/cost totals for runs started in the last `days` days, grouped
        by source (playbook name when there is one, else the trigger kind:
        user / cron / watch / delegate). Feeds the cost-report playbook and
        the web UI."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = await self.db.conn.execute(
            "SELECT playbook, trigger, status, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, thinking_tokens, cost_usd "
            "FROM runs WHERE started_at >= ?",
            (since,),
        )
        rows = await cur.fetchall()

        token_keys = (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "thinking_tokens",
        )
        totals = {"runs": 0, "errors": 0, "cost_usd": 0.0}
        totals.update({k: 0 for k in token_keys})
        groups: dict[str, dict] = {}
        for row in rows:
            group = row["playbook"] or row["trigger"].partition(":")[0]
            bucket = groups.setdefault(
                group, {"group": group, "runs": 0, "errors": 0, "cost_usd": 0.0,
                        **{k: 0 for k in token_keys}}
            )
            for acc in (totals, bucket):
                acc["runs"] += 1
                acc["errors"] += 1 if row["status"] == "error" else 0
                acc["cost_usd"] += row["cost_usd"]
                for k in token_keys:
                    acc[k] += row[k]
        return {
            "days": days,
            "since": since,
            **totals,
            "by_group": sorted(
                groups.values(), key=lambda g: g["cost_usd"], reverse=True
            ),
        }

    async def events(self, run_id: str) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT ts, kind, data_json FROM run_events WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [
            {"ts": r["ts"], "kind": r["kind"], "data": json.loads(r["data_json"])}
            for r in await cur.fetchall()
        ]

    async def add_event(self, run_id: str, kind: str, data: dict) -> None:
        await self.db.conn.execute(
            "INSERT INTO run_events (run_id, ts, kind, data_json) VALUES (?, ?, ?, ?)",
            (run_id, utcnow(), kind, json.dumps(data, default=str)),
        )
        await self.db.conn.commit()

    async def session_compactions(self, session_id: str) -> list[dict]:
        """This session's compaction history, oldest first: the run_events
        kind='compaction' rows joined to runs for the session scope (the
        event names the run; the run names the session — a run without one
        is a background job's, never part of this transcript). Feeds the
        reload view's seam anchors (plan compaction-checkpoint-bookmark
        Step 5). `created_at` is the EVENT row's ts — full ISO with offset,
        unlike the checkpoint row's offset-less `[:19]` stamp, so JS Date
        parses it as UTC without help. Rows written before Step 3 carry no
        anchor_idx: a seam cannot anchor them, so they are dropped here
        rather than answered to the client unanchored."""
        cur = await self.db.conn.execute(
            "SELECT re.ts AS created_at, re.data_json AS data FROM run_events re "
            "JOIN runs r ON r.id = re.run_id "
            "WHERE r.session_id = ? AND re.kind = 'compaction' ORDER BY re.id",
            (session_id,),
        )
        out = []
        for row in await cur.fetchall():
            try:
                data = json.loads(row["data"])
            except (TypeError, ValueError):
                continue
            if not isinstance(data, dict) or type(data.get("anchor_idx")) is not int:
                continue
            out.append(
                {
                    # Dedupe identity for the live join (compaction.js): the
                    # same compaction may arrive BOTH here and in the event
                    # tail of a mid-turn reload. "" when the checkpoint
                    # write failed — anchor_idx still dedupes.
                    "checkpoint_id": data.get("checkpoint_id") or "",
                    "anchor_idx": data["anchor_idx"],
                    "created_at": row["created_at"],
                    "est_before": data.get("est_tokens_before"),
                    "est_after": data.get("est_tokens_after"),
                }
            )
        return out


async def trace_event(
    runs: "RunStore | None",
    run_id: str | None,
    kind: str,
    data: dict,
    *,
    source: str = "trace",
) -> None:
    """Best-effort run tracing: never raises — a failed trace write must not
    abort the turn it observes. Failures surface on stderr, tagged with the
    caller (`source`). No-op without a store or run id."""
    if runs is None or run_id is None:
        return
    try:
        await runs.add_event(run_id, kind, data)
    except Exception as e:  # noqa: BLE001 - tracing must not raise
        print(f"[{source}] trace {kind!r} failed: {e}", file=sys.stderr)
