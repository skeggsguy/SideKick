"""sessions.memory_profile: the frozen always-on profile column (migration
014) and its store/session plumbing.

The text is rendered once per session and never re-rendered, so a mid-chat
fact save can't change a live conversation's system-prompt prefix. NULL is
the legacy marker: a pre-column session keeps the plain system prompt (the
old combined <memory> block already sits in its first message).
"""

from __future__ import annotations

from harness.core.session import Session
from harness.store.db import Database
from harness.store.sessions import SessionStore

MIGRATION = "014_session_memory_profile.sql"


# --- migration ----------------------------------------------------------------


async def _session_columns(db) -> set[str]:
    cur = await db.conn.execute("PRAGMA table_info(sessions)")
    return {row["name"] for row in await cur.fetchall()}


async def test_migration_014_adds_memory_profile_column(db):
    assert "memory_profile" in await _session_columns(db)
    cur = await db.conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (MIGRATION,)
    )
    assert await cur.fetchone() is not None


async def test_migration_upgrades_a_pre_column_database(tmp_path):
    """The live-DB path: agentd opens a DB migrated through 013 and the
    column appears, NULL for the rows that were already there."""
    path = tmp_path / "old.db"
    # Every open is closed in a finally: a Database left open leaks aiosqlite's
    # non-daemon thread and pytest's exit hangs forever (agentd gotcha).
    db = await Database.open(path)
    try:
        # Backdate the schema to the pre-014 shape.
        await db.conn.execute("ALTER TABLE sessions DROP COLUMN memory_profile")
        await db.conn.execute(
            "DELETE FROM schema_migrations WHERE name = ?", (MIGRATION,)
        )
        await db.conn.execute(
            "INSERT INTO sessions (id, channel, created_at, updated_at) "
            "VALUES ('legacy', 'cli', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        await db.conn.commit()
    finally:
        await db.close()

    db2 = await Database.open(path)
    try:
        assert "memory_profile" in await _session_columns(db2)
        cur = await db2.conn.execute(
            "SELECT memory_profile FROM sessions WHERE id = 'legacy'"
        )
        assert (await cur.fetchone())["memory_profile"] is None
    finally:
        await db2.close()


# --- store get/set ------------------------------------------------------------


async def test_memory_profile_round_trip(db):
    store = SessionStore(db)
    sid = await store.create()
    # A session that has never snapshotted reads as None, same as legacy.
    assert await store.get_memory_profile(sid) is None

    assert await store.set_memory_profile(sid, "<user_profile>facts</user_profile>")
    assert await store.get_memory_profile(sid) == "<user_profile>facts</user_profile>"

    # "" is a real snapshot ("snapshotted, no facts") and must survive the
    # round trip — it is what keeps step 3 from confusing empty with NULL.
    assert await store.set_memory_profile(sid, "")
    assert await store.get_memory_profile(sid) == ""

    # Unknown session: get is None, set reports the miss.
    assert await store.get_memory_profile("does-not-exist") is None
    assert await store.set_memory_profile("does-not-exist", "x") is False


async def test_set_memory_profile_does_not_bump_updated_at(db):
    """Session-list ordering is keyed on updated_at; freezing a profile is not
    activity (same reasoning as set_title_if_unset)."""
    store = SessionStore(db)
    sid = await store.create()
    sentinel = "2020-01-01T00:00:00+00:00"
    await db.conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?", (sentinel, sid)
    )
    await db.conn.commit()

    await store.set_memory_profile(sid, "frozen text")

    cur = await db.conn.execute(
        "SELECT updated_at, memory_profile FROM sessions WHERE id = ?", (sid,)
    )
    row = await cur.fetchone()
    assert row["updated_at"] == sentinel
    assert row["memory_profile"] == "frozen text"


# --- Session plumbing ---------------------------------------------------------


async def test_resume_loads_frozen_profile(db):
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("hi")
    await store.set_memory_profile(session.id, "<user_profile>facts</user_profile>")

    resumed = await Session.resume(store, session.id)

    assert resumed is not None
    assert resumed.memory_profile == "<user_profile>facts</user_profile>"


async def test_resume_legacy_session_profile_is_none(db):
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("pre-column turn")

    resumed = await Session.resume(store, session.id)

    assert resumed is not None
    assert resumed.memory_profile is None


def test_storeless_session_profile_defaults_none():
    assert Session().memory_profile is None
