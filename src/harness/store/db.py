"""SQLite connection + migration runner.

One database file (WAL mode) holds all durable state. Migrations are plain
.sql files in store/migrations/, applied in name order and tracked in
schema_migrations. The DB must live on a local filesystem (APFS), never
iCloud Drive or a network mount — WAL needs local file locking.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def default_db_path() -> Path:
    env = os.environ.get("HARNESS_DB_PATH")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "harness" / "harness.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn

    @classmethod
    async def open(cls, path: Path | str) -> Database:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        db = cls(conn)
        await db._migrate()
        return db

    async def _migrate(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        cur = await self.conn.execute("SELECT name FROM schema_migrations")
        applied = {row["name"] for row in await cur.fetchall()}

        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.name in applied:
                continue
            await self.conn.executescript(sql_file.read_text(encoding="utf-8"))
            await self.conn.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (sql_file.name, utcnow()),
            )
        await self.conn.commit()

    async def close(self) -> None:
        # Skipping this hangs process exit silently (aiosqlite's thread is
        # non-daemon) — tests that open a Database close it in try/finally.
        await self.conn.close()
