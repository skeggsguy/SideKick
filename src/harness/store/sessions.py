"""Session persistence: conversation rows in `sessions` + `messages`.

Messages are stored as the full internal Message serialized to JSON, so the
wire shape can evolve without schema churn.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from ..core.types import Message, ToolCall, ToolResult
from .db import Database, utcnow


class SessionStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(self, channel: str = "cli", title: str | None = None) -> str:
        session_id = uuid.uuid4().hex[:12]
        now = utcnow()
        await self.db.conn.execute(
            "INSERT INTO sessions (id, channel, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, channel, title, now, now),
        )
        await self.db.conn.commit()
        return session_id

    async def set_title_if_unset(self, session_id: str, title: str) -> bool:
        """Auto-naming: set the title only when it is still NULL. Idempotent —
        a resumed/re-fired turn can't clobber a name the user already has.
        Deliberately does NOT bump updated_at (so the session list ordering
        doesn't jump when a title quietly lands). Returns True if it wrote."""
        cur = await self.db.conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ? AND title IS NULL",
            (title, session_id),
        )
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def update_title(self, session_id: str, title: str) -> bool:
        """Manual rename: set the title unconditionally. Returns False if the
        session doesn't exist."""
        cur = await self.db.conn.execute(
            "UPDATE sessions SET title = ? WHERE id = ?", (title, session_id)
        )
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def get_memory_profile(self, session_id: str) -> str | None:
        """The session's frozen always-on profile text, or None for a legacy
        (pre-column) session. Empty string means "snapshotted, no facts"."""
        cur = await self.db.conn.execute(
            "SELECT memory_profile FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cur.fetchone()
        return row["memory_profile"] if row else None

    async def set_memory_profile(self, session_id: str, text: str) -> bool:
        """Freeze the profile text for this session. Written once, on the
        first turn; never re-rendered. Takes str, never None — "snapshotted,
        no facts" is stored as "" so it stays distinct from a legacy NULL.
        Deliberately does NOT bump updated_at (so the session list ordering
        doesn't jump when a profile lands). Returns False if missing."""
        cur = await self.db.conn.execute(
            "UPDATE sessions SET memory_profile = ? WHERE id = ?", (text, session_id)
        )
        await self.db.conn.commit()
        return cur.rowcount > 0

    async def append_message(self, session_id: str, idx: int, message: Message) -> None:
        await self.db.conn.execute(
            "INSERT INTO messages (session_id, idx, role, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, idx, message.role, json.dumps(asdict(message)), utcnow()),
        )
        await self.db.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (utcnow(), session_id)
        )
        await self.db.conn.commit()

    async def append_step(
        self,
        session_id: str,
        a_idx: int,
        assistant: Message,
        t_idx: int,
        tool_msg: Message,
    ) -> None:
        """Persist an assistant tool_use message and its tool_result message
        under a single commit. All-or-nothing: if the process dies before the
        commit, neither row lands, so the store never holds a stranded tool_use."""
        await self.db.conn.execute(
            "INSERT INTO messages (session_id, idx, role, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, a_idx, assistant.role, json.dumps(asdict(assistant)), utcnow()),
        )
        await self.db.conn.execute(
            "INSERT INTO messages (session_id, idx, role, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, t_idx, tool_msg.role, json.dumps(asdict(tool_msg)), utcnow()),
        )
        await self.db.conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?", (utcnow(), session_id)
        )
        await self.db.conn.commit()

    # POSITIONAL list: never index it by store idx (a hole shifts every later
    # row) — load_messages_indexed carries the idxes.
    async def load_messages(self, session_id: str) -> list[Message]:
        rows = await self.load_messages_indexed(session_id)
        return [m for _, m in rows]

    async def load_messages_indexed(
        self, session_id: str
    ) -> list[tuple[int, Message]]:
        """The messages paired with their STORE idx, oldest first. Resume
        needs the idxes: a checkpoint row addresses rows by store idx, and
        position and idx only coincide while the table has no holes — one
        failed append the caller swallowed leaves a permanent hole, because
        UNIQUE (session_id, idx) never refills the gap."""
        cur = await self.db.conn.execute(
            "SELECT idx, payload_json FROM messages WHERE session_id = ? "
            "ORDER BY idx",
            (session_id,),
        )
        rows = await cur.fetchall()
        return [
            (row["idx"], _message_from_dict(json.loads(row["payload_json"])))
            for row in rows
        ]

    # --- compaction checkpoints (session_checkpoints, one row per session) ---

    async def load_checkpoint(self, session_id: str) -> dict | None:
        """The session's current compaction checkpoint, or None. A dict of the
        row: id, covers_upto_idx, anchor_idx, summary_text, summarized,
        est_before, est_after, created_at."""
        cur = await self.db.conn.execute(
            "SELECT id, covers_upto_idx, anchor_idx, summary_text, summarized, "
            "est_before, est_after, created_at FROM session_checkpoints "
            "WHERE session_id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def save_checkpoint(
        self,
        session_id: str,
        *,
        covers_upto_idx: int,
        anchor_idx: int,
        summary_text: str,
        summarized: bool,
        est_before: int,
        est_after: int,
        created_at: str | None = None,
    ) -> str:
        """Replace the session's checkpoint (a later compaction supersedes the
        previous one; covers_upto_idx marks the frozen opening's end and never
        moves, anchor_idx grows monotonically — guaranteed by the single
        writer, whose view indexes are strictly increasing). Returns the new
        row id. created_at records when this summary froze and rides the
        framing text, so the caller renders the checkpoint message once and
        resume re-renders byte-identically; omit it for a bare utcnow write."""
        checkpoint_id = uuid.uuid4().hex[:12]
        await self.db.conn.execute(
            "INSERT OR REPLACE INTO session_checkpoints (session_id, id, "
            "covers_upto_idx, anchor_idx, summary_text, summarized, est_before, "
            "est_after, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                checkpoint_id,
                covers_upto_idx,
                anchor_idx,
                summary_text,
                1 if summarized else 0,
                est_before,
                est_after,
                created_at or utcnow(),
            ),
        )
        await self.db.conn.commit()
        return checkpoint_id

    async def exists(self, session_id: str) -> bool:
        cur = await self.db.conn.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        )
        return await cur.fetchone() is not None

    async def get(self, session_id: str) -> dict | None:
        # The single-row twin of list()'s SELECT — PWA boot-restore needs the
        # row for an id the capped list cannot see (routes/resources.py).
        cur = await self.db.conn.execute(
            "SELECT id, channel, title, created_at, updated_at FROM sessions "
            "WHERE id = ?",
            (session_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list(self, limit: int = 50) -> list[dict]:
        cur = await self.db.conn.execute(
            "SELECT id, channel, title, created_at, updated_at FROM sessions "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in await cur.fetchall()]

    async def latest_session_id(self) -> str | None:
        cur = await self.db.conn.execute(
            "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
        )
        row = await cur.fetchone()
        return row["id"] if row else None


def _message_from_dict(d: dict) -> Message:
    return Message(
        role=d["role"],
        text=d.get("text"),
        tool_calls=[ToolCall(**tc) for tc in d.get("tool_calls") or []],
        # Rows written while BK-001's tool images were live (reverted
        # 2026-10-25) carry an `images` key ToolResult no longer has — drop it.
        tool_results=[
            ToolResult(**{k: v for k, v in tr.items() if k != "images"})
            for tr in d.get("tool_results") or []
        ],
        # Rows persisted before extended thinking have no content_blocks.
        content_blocks=d.get("content_blocks") or [],
        # Same for attachments (pre-image rows) — no migration, additive field.
        attachments=d.get("attachments") or [],
    )
