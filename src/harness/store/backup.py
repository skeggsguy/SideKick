"""Rotating on-disk backups of the harness database.

Uses SQLite's online backup API (via aiosqlite), which is safe while the
daemon is writing — no need to stop agentd. Backups land next to the live
DB by default (same local filesystem) and rotate: the oldest are
deleted once there are more than `keep`. Restore = stop agentd, copy a
backup file over the live DB, start agentd.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from .db import Database, default_db_path

DEFAULT_KEEP = 7
# The daily job backs up when the newest backup is older than this. Slightly
# under 24h so a daemon restarted at roughly the same time each day doesn't
# skip a day.
STALE_AFTER = timedelta(hours=23)


def default_backup_dir() -> Path:
    env = os.environ.get("HARNESS_BACKUP_DIR")
    if env:
        return Path(env).expanduser()
    return default_db_path().parent / "backups"


def latest_backup(backup_dir: Path) -> Path | None:
    backups = sorted(backup_dir.glob("harness-*.db"))
    return backups[-1] if backups else None


async def backup_database(
    db: Database, backup_dir: Path, keep: int = DEFAULT_KEEP
) -> Path:
    """Write a consistent snapshot of the live DB and prune old backups."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"harness-{stamp}.db"
    target = await aiosqlite.connect(dest)
    try:
        await db.conn.backup(target)
    finally:
        await target.close()
    for old in sorted(backup_dir.glob("harness-*.db"))[:-keep]:
        old.unlink()
    return dest


async def backup_if_stale(
    db: Database, backup_dir: Path, keep: int = DEFAULT_KEEP
) -> Path | None:
    """Back up unless a recent backup already exists (idempotent daily job).

    Freshness comes from the timestamp in the filename, not mtime — mtime
    survives neither copies nor clock jumps after a sleep/resume.
    """
    newest = latest_backup(backup_dir)
    if newest is not None:
        stamp = newest.stem.removeprefix("harness-")
        try:
            taken = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            taken = None
        if taken is not None and datetime.now(timezone.utc) - taken < STALE_AFTER:
            return None
    return await backup_database(db, backup_dir, keep=keep)
