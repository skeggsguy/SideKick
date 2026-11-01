from __future__ import annotations

import pytest

from harness.store.db import Database


def gotcha(handle: str, detail: str) -> str:
    """Failure prefix for gotcha-pin tests: the detail carries the trap's why (full history:
    grep .claude/learnings-archive.md for the handle or the test name)."""
    return f"GOTCHA REGRESSION [{handle}] {detail}"


@pytest.fixture
async def db(tmp_path):
    """A migrated, throwaway SQLite database on a local disk (pytest tmp dir)."""
    database = await Database.open(tmp_path / "test.db")
    yield database
    await database.close()
