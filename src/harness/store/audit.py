"""Append-only audit trail + the persisted approval allowlist.

The audit log is deliberately separate from run_events: traces are for
debugging, the audit log answers "what did the agent do to the world and who
approved it".
"""

from __future__ import annotations

import json

from .db import Database, utcnow


class AuditStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def log(
        self,
        run_id: str | None,
        tool: str,
        args: dict,
        classification: str,
        decision: str,
        result_digest: str | None,
        is_error: bool,
    ) -> None:
        await self.db.conn.execute(
            "INSERT INTO audit_log (run_id, ts, tool, args_json, classification, "
            "decision, result_digest, is_error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                utcnow(),
                tool,
                json.dumps(args, default=str),
                classification,
                decision,
                result_digest,
                int(is_error),
            ),
        )
        await self.db.conn.commit()


class AllowlistStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # NOTE: this store is a dumb persistence primitive — it does NOT check the
    # tool's ActionClass. The safety property (only MUTATING_LOCAL is
    # allowlistable) lives in the callers: the /api/allowlist route 422s a
    # non-MUTATING_LOCAL tool, and PolicyEngine.check ignores allowlist entries
    # for any non-MUTATING_LOCAL class at call time (the real backstop). Any new
    # caller of add() MUST re-verify the class first.
    # `pattern` is reserved for future arg-scoped grants; everything uses '*'
    # today, and contains() matches tool-wide (any pattern) by design.
    async def add(self, tool: str, pattern: str = "*") -> None:
        await self.db.conn.execute(
            "INSERT OR IGNORE INTO allowlist (tool, pattern, created_at) "
            "VALUES (?, ?, ?)",
            (tool, pattern, utcnow()),
        )
        await self.db.conn.commit()

    # Per TOOL, never per group: tool groups only affect grammar exposure, so
    # "that group is allowlisted so its writes auto-run" is a category error.
    async def contains(self, tool: str) -> bool:
        cur = await self.db.conn.execute(
            "SELECT 1 FROM allowlist WHERE tool = ?", (tool,)
        )
        return await cur.fetchone() is not None

    async def remove(self, tool: str, pattern: str = "*") -> bool:
        """Revoke a standing 'always allow' grant. Returns True if a row was
        removed (so the route can 404 cleanly)."""
        cur = await self.db.conn.execute(
            "DELETE FROM allowlist WHERE tool = ? AND pattern = ?", (tool, pattern)
        )
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def list(self) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT tool, pattern, created_at FROM allowlist ORDER BY tool"
        )
        return [dict(row) for row in await cur.fetchall()]
