"""DB backup job (P7): online snapshot, rotation, and the daily staleness check."""

from __future__ import annotations

import sqlite3

from harness.store.backup import backup_database, backup_if_stale, latest_backup


async def test_backup_is_a_valid_snapshot(db, tmp_path):
    await db.conn.execute(
        "INSERT INTO sessions (id, channel, created_at, updated_at) "
        "VALUES ('s1', 'cli', '2026-06-12', '2026-06-12')"
    )
    await db.conn.commit()

    dest = await backup_database(db, tmp_path / "backups")
    assert dest.exists()
    with sqlite3.connect(dest) as conn:
        rows = conn.execute("SELECT id FROM sessions").fetchall()
    assert rows == [("s1",)]


async def test_backup_rotation_prunes_oldest(db, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    for stamp in ("20200101-000000", "20200102-000000", "20200103-000000"):
        (backup_dir / f"harness-{stamp}.db").touch()

    dest = await backup_database(db, backup_dir, keep=2)
    remaining = sorted(p.name for p in backup_dir.glob("harness-*.db"))
    assert remaining == ["harness-20200103-000000.db", dest.name]


async def test_backup_if_stale_skips_when_fresh(db, tmp_path):
    backup_dir = tmp_path / "backups"
    first = await backup_if_stale(db, backup_dir)
    assert first is not None
    assert await backup_if_stale(db, backup_dir) is None  # just taken — fresh


async def test_backup_if_stale_fires_when_old(db, tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    (backup_dir / "harness-20200101-000000.db").touch()

    dest = await backup_if_stale(db, backup_dir)
    assert dest is not None
    assert latest_backup(backup_dir) == dest
