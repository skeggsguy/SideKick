"""Triggers: the durable schedule. The scheduler reads these every tick;
the agent (via schedule tools), the CLI, and the web UI write them.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

from .db import Database, utcnow

#: Sentinel distinguishing "omit this field" from an explicit None in update().
_UNSET = object()


@dataclass
class Trigger:
    id: str
    kind: str  # cron | watch
    spec: str  # cron expression, or watcher name
    playbook: str  # playbook name, or inline prompt for one-off reminders
    channel: str
    autonomy: str
    enabled: bool
    one_shot: bool
    last_fired_at: str | None
    created_at: str
    label: str | None = None  # short display name; None ⇒ fall back to playbook
    #: Per-job tool allocation. None ⇒ no allocation: playbook frontmatter
    #: (legacy name rows) or the full toolset. A set allocation overrides
    #: frontmatter at fire time.
    tools: list[str] | None = None


def _row_to_trigger(row) -> Trigger:
    return Trigger(
        id=row["id"],
        kind=row["kind"],
        spec=row["spec"],
        playbook=row["playbook"],
        channel=row["channel"],
        autonomy=row["autonomy"],
        enabled=bool(row["enabled"]),
        one_shot=bool(row["one_shot"]),
        last_fired_at=row["last_fired_at"],
        created_at=row["created_at"],
        label=row["label"],
        tools=json.loads(row["tools"]) if row["tools"] else None,
    )


def _dump_tools(tools: list[str] | None) -> str | None:
    """Serialize an allocation: deduped but order-preserving (a deterministic
    tool list keeps the prompt-cache prefix byte-stable across runs)."""
    if not tools:
        return None
    return json.dumps(list(dict.fromkeys(tools)))


class TriggerStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        kind: str,
        spec: str,
        playbook: str,
        channel: str = "web",
        autonomy: str = "supervised",
        one_shot: bool = False,
        label: str | None = None,
        tools: list[str] | None = None,
    ) -> str:
        trigger_id = uuid.uuid4().hex[:8]
        await self.db.conn.execute(
            "INSERT INTO triggers (id, kind, spec, playbook, channel, autonomy, "
            "enabled, one_shot, created_at, label, tools) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
            (trigger_id, kind, spec, playbook, channel, autonomy, int(one_shot),
             utcnow(), label, _dump_tools(tools)),
        )
        await self.db.conn.commit()
        return trigger_id

    async def get(self, trigger_id: str) -> Trigger | None:
        cur = await self.db.conn.execute(
            "SELECT * FROM triggers WHERE id = ?", (trigger_id,)
        )
        row = await cur.fetchone()
        return _row_to_trigger(row) if row else None

    async def list(self, enabled_only: bool = False) -> list[Trigger]:
        sql = "SELECT * FROM triggers"
        if enabled_only:
            sql += " WHERE enabled = 1"
        cur = await self.db.conn.execute(sql + " ORDER BY created_at")
        return [_row_to_trigger(r) for r in await cur.fetchall()]

    async def mark_fired(self, trigger_id: str, fired_at: str | None = None) -> None:
        """Record a firing; one-shot triggers (reminders) disable themselves."""
        await self.db.conn.execute(
            "UPDATE triggers SET last_fired_at = ?, "
            "enabled = CASE WHEN one_shot = 1 THEN 0 ELSE enabled END "
            "WHERE id = ?",
            (fired_at or utcnow(), trigger_id),
        )
        await self.db.conn.commit()

    async def set_enabled(self, trigger_id: str, enabled: bool) -> None:
        await self.db.conn.execute(
            "UPDATE triggers SET enabled = ? WHERE id = ?",
            (int(enabled), trigger_id),
        )
        await self.db.conn.commit()

    async def update(
        self,
        trigger_id: str,
        *,
        spec: str | None = None,
        playbook: str | None = None,
        one_shot: bool | None = None,
        label: str | None = _UNSET,  # type: ignore[assignment]
        autonomy: str | None = None,
        tools: list[str] | None = _UNSET,  # type: ignore[assignment]
    ) -> None:
        """Partial update: only the fields you pass are written. label and
        tools use a sentinel so passing None explicitly clears them (distinct
        from omitting them)."""
        sets: list[str] = []
        params: list[object] = []
        if spec is not None:
            sets.append("spec = ?")
            params.append(spec)
        if playbook is not None:
            sets.append("playbook = ?")
            params.append(playbook)
        if one_shot is not None:
            sets.append("one_shot = ?")
            params.append(int(one_shot))
        if label is not _UNSET:
            sets.append("label = ?")
            params.append(label)
        if autonomy is not None:
            sets.append("autonomy = ?")
            params.append(autonomy)
        if tools is not _UNSET:
            sets.append("tools = ?")
            params.append(_dump_tools(tools))
        if not sets:
            return
        params.append(trigger_id)
        await self.db.conn.execute(
            f"UPDATE triggers SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.db.conn.commit()

    async def delete(self, trigger_id: str) -> None:
        await self.db.conn.execute(
            "DELETE FROM triggers WHERE id = ?", (trigger_id,)
        )
        await self.db.conn.commit()
