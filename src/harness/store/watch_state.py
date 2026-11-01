"""Watcher cursors (e.g. the Gmail historyId) — one row per watcher."""

from __future__ import annotations

from .db import Database, utcnow


class WatchStateStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, watcher: str) -> str | None:
        cur = await self.db.conn.execute(
            "SELECT cursor FROM watch_state WHERE watcher = ?", (watcher,)
        )
        row = await cur.fetchone()
        return row["cursor"] if row is not None else None

    async def set(self, watcher: str, cursor: str) -> None:
        await self.db.conn.execute(
            "INSERT INTO watch_state (watcher, cursor, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(watcher) DO UPDATE SET cursor = excluded.cursor, "
            "updated_at = excluded.updated_at",
            (watcher, cursor, utcnow()),
        )
        await self.db.conn.commit()
