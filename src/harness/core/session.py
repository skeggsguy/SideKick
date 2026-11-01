"""Conversation state for a single session, persisted as it grows.

Backed by SessionStore when one is provided (messages are written as they are
appended, so quit/resume keeps full history). With store=None the session is
in-memory only — used by tests and throwaway runs.

A compacted session's view has THREE parts: the frozen OPENING (the first
exchange, verbatim with oversized tool results truncated), the synthesized
<context_checkpoint> message (framing + summary), and the verbatim TAIL from
the cut onward. The checkpoint row stores the summary bytes plus the store
indexes locating the two boundaries, so resume rebuilds the SAME view
byte-identically instead of loading the whole history and re-summarizing.
A hole in the messages table (a failed append whose error the caller
swallowed) voids that rebuild — resume detects it, loads full history by the
rows' real indexes, and lets the session re-compact.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace

from ..store.sessions import SessionStore
from .types import Message, ToolCall, ToolResult

CHECKPOINT_OPEN = "<context_checkpoint>"
CHECKPOINT_CLOSE = "</context_checkpoint>"

#: Store index of the synthesized checkpoint message — it is not a row in the
#: messages table, and -1 can never collide with a real store index.
SYNTH_IDX = -1

#: Opening tool results over this many chars are truncated when the opening
#: freezes. Deterministic (length + head only, no clock/random) so every
#: resume re-truncates the stored bytes to the SAME bytes.
OPENING_TOOL_RESULT_CHARS = 1_500


def checkpoint_message_text(summary: str, created_at: str) -> str:
    """The synthesized <context_checkpoint> message of a compacted view. ONE
    renderer for the live compaction and the resume rebuild — the bytes must
    be identical across both, or the llama.cpp prefix cache dies on the first
    turn after a reload. created_at rides the text (and is stored with the
    row) so the framing states when the middle was compacted and stays
    byte-frozen forever."""
    return (
        f"{CHECKPOINT_OPEN}\n"
        f"The middle of this conversation was compacted by a local model at "
        f"{created_at} to fit the context window. The messages above are the "
        f"real, verbatim start of the conversation — the opening user turn is "
        f"the authoritative statement of the task. The messages below are the "
        f"real, verbatim recent turns. Everything between them was elided "
        f"into the summary below; its facts (numbers, paths, decisions) may "
        f"be stale or wrong — re-read files and check current state rather "
        f"than trusting the summary.\n\n"
        f"{summary}\n"
        f"{CHECKPOINT_CLOSE}"
    )


def _truncate_result(content: str) -> str:
    if len(content) <= OPENING_TOOL_RESULT_CHARS:
        return content
    return (
        content[:OPENING_TOOL_RESULT_CHARS]
        + f"\n[… tool result truncated in the frozen opening: showing "
        f"{OPENING_TOOL_RESULT_CHARS} of {len(content)} characters — re-run "
        f"the tool or re-read the file if the full output matters]"
    )


def freeze_opening(messages: list[Message]) -> list[Message]:
    """The opening part of a compacted view: the first exchange kept verbatim
    except that oversized tool RESULTS are deterministically truncated (they
    are the bytes that explode). Pure function of the stored bytes — applied
    once at compaction and re-applied identically on every resume, so the
    prefix bytes never change. Never mutates the inputs."""
    return [
        replace(
            m,
            tool_results=[
                replace(tr, content=_truncate_result(tr.content))
                for tr in m.tool_results
            ],
        )
        if m.tool_results
        else m
        for m in messages
    ]


@dataclass
class Checkpoint:
    """A persisted compaction summary plus the store indexes that locate it.
    The compacted view keeps rows [0, covers_upto_idx) verbatim as the opening
    (with oversized tool results truncated) and rows [anchor_idx, ...) as the
    tail; the summary covers the elided middle in between,
    covers_upto_idx ≤ idx < anchor_idx. covers_upto_idx is FROZEN once written
    (the opening never moves); anchor_idx grows with each later compaction."""

    id: str
    covers_upto_idx: int
    anchor_idx: int
    summary_text: str
    summarized: bool
    est_before: int
    est_after: int
    created_at: str = ""


class Session:
    def __init__(
        self, store: SessionStore | None = None, session_id: str | None = None
    ) -> None:
        self.store = store
        self.id = session_id
        self.messages: list[Message] = []
        # The always-on profile facts, frozen when the session's first turn
        # ran; appended to the end of the system prompt for every later call.
        # None = legacy/never-snapshotted session → plain system prompt.
        # A store-less Session() only ever has this plain default.
        self.memory_profile: str | None = None
        # Storage index for the next appended message. Tracked separately from
        # len(messages) because compaction shrinks the in-memory list while
        # the store keeps (and must keep) the full history.
        self._next_idx = 0
        #: Store index of each message in the current view (kept parallel to
        #: `messages`; a resume that sanitizes legacy orphans leaves holes, so
        #: view position != store idx and compaction must map through this).
        #: The synthesized <context_checkpoint> message carries SYNTH_IDX —
        #: it is not a store row.
        self._idxs: list[int] = []
        #: The persisted compaction checkpoint this view was built from (or
        #: was just written), None when the conversation is uncompacted.
        self.checkpoint: Checkpoint | None = None

    @classmethod
    async def create(
        cls, store: SessionStore, channel: str = "cli", title: str | None = None
    ) -> "Session":
        session_id = await store.create(channel=channel, title=title)
        return cls(store=store, session_id=session_id)

    @classmethod
    async def resume(
        cls, store: SessionStore, session_id: str | None = None
    ) -> "Session | None":
        """Resume by id, or the most recently active session when id is None."""
        if session_id is None:
            session_id = await store.latest_session_id()
            if session_id is None:
                return None
        elif not await store.exists(session_id):
            return None
        session = cls(store=store, session_id=session_id)
        rows = await store.load_messages_indexed(session_id)
        stored = [m for _, m in rows]
        idxs = [i for i, _ in rows]
        checkpoint = _checkpoint_from_row(await store.load_checkpoint(session_id))
        # A compacted session resumes from its checkpoint: opening summary +
        # tail, rebuilt byte-identically to the live post-compaction view (the
        # merged message and the egress seeding both depend on same-bytes).
        # The checkpoint row addresses rows by STORE idx, and _checkpoint_view
        # slices by list position — sound only while idx and position coincide.
        # A hole in the table (a failed append the CLI swallowed; UNIQUE
        # (session_id, idx) keeps the gap open forever) breaks that, so a gap
        # drops the checkpoint view entirely: full load addressed by the
        # rows' real idxes, which re-compacts from honest history instead of
        # resuming a silently shifted view. A corrupt anchor past the stored
        # history falls back the same way.
        contiguous = idxs == list(range(len(stored)))
        if (
            checkpoint is not None
            and contiguous
            and 0 <= checkpoint.anchor_idx < len(stored)
        ):
            session.checkpoint = checkpoint
            session.messages, session._idxs = _checkpoint_view(checkpoint, stored)
        else:
            if checkpoint is not None and not contiguous:
                print(
                    f"session {session_id}: messages table has a gap; dropped "
                    "the compaction checkpoint view — resuming at full "
                    "history, which will re-compact",
                    file=sys.stderr,
                )
            # The store may hold a half-written turn — an assistant tool_use whose
            # tool_result never landed (a turn that died between the two writes).
            # Resending that to the API is a hard 400, so repair the in-memory
            # view. The store is append-only and untouched; only this session's
            # working copy is trimmed.
            session.messages, session._idxs = sanitize_pairs(stored, idxs=idxs)
        # Past the TRUE max idx, not len(stored): with a hole, len() names an
        # idx that already exists and every later append hits UNIQUE.
        session._next_idx = (max(idxs) + 1) if idxs else 0
        # The frozen profile rides along so a resumed session sends exactly
        # the system prompt it started with. NULL (legacy row) stays None.
        session.memory_profile = await store.get_memory_profile(session_id)
        return session

    async def add_user(
        self, text: str, attachments: list[dict] | None = None
    ) -> None:
        await self._append(
            Message(role="user", text=text, attachments=attachments or [])
        )

    async def add_assistant(
        self,
        text: str | None,
        tool_calls: list[ToolCall] | None = None,
        content_blocks: list[dict] | None = None,
    ) -> None:
        await self._append(
            Message(
                role="assistant",
                text=text,
                tool_calls=tool_calls or [],
                content_blocks=content_blocks or [],
            )
        )

    async def add_step(
        self,
        text: str | None,
        tool_calls: list[ToolCall],
        results: list[ToolResult],
        content_blocks: list[dict] | None = None,
    ) -> None:
        """Append an assistant tool_use message and its tool_result message as
        one unit. The store writes both rows under a single commit, so a turn
        that dies mid-dispatch can never leave the assistant tool_use stranded
        without its results (the corruption that 400s the next API call)."""
        assistant = Message(
            role="assistant",
            text=text,
            tool_calls=tool_calls,
            content_blocks=content_blocks or [],
        )
        tool_msg = Message(role="tool", tool_results=results)
        a_idx = self._next_idx
        t_idx = self._next_idx + 1
        self._next_idx += 2
        self._sync_idxs()
        self.messages.append(assistant)
        self._idxs.append(a_idx)
        self.messages.append(tool_msg)
        self._idxs.append(t_idx)
        if self.store is not None and self.id is not None:
            await self.store.append_step(self.id, a_idx, assistant, t_idx, tool_msg)

    def replace_history(
        self, messages: list[Message], idxs: list[int] | None = None
    ) -> None:
        """Swap the in-memory view (compaction), with the store index of each
        kept message. The store is untouched — it remains the full, append-only
        record of the conversation."""
        self.messages = messages
        self._idxs = list(idxs) if idxs is not None else []

    @property
    def store_idxs(self) -> list[int]:
        """Store index per message in the current view. A view assembled by
        direct assignment (tests, store-less runs) reports positional indexes,
        which is exactly right when there is no store."""
        if len(self._idxs) == len(self.messages):
            return list(self._idxs)
        return list(range(len(self.messages)))

    def _sync_idxs(self) -> None:
        """Keep _idxs parallel to messages. A view built by direct assignment
        has no real indexes; resync positionally before recording a real one."""
        if len(self._idxs) != len(self.messages):
            self._idxs = list(range(len(self.messages)))

    async def _append(self, message: Message) -> None:
        idx, self._next_idx = self._next_idx, self._next_idx + 1
        self._sync_idxs()
        self.messages.append(message)
        self._idxs.append(idx)
        if self.store is not None and self.id is not None:
            await self.store.append_message(self.id, idx, message)


def sanitize_history(messages: list[Message]) -> list[Message]:
    """Return a structurally valid copy: every assistant tool_use is immediately
    followed by its tool_result message, with no stranded tool_use and no orphan
    tool_result. A turn that dies mid-dispatch strands a tool_use, and once later
    turns pile on it ends up mid-history, not just at the tail — exactly what the
    API rejects with 'tool_use ids were found without tool_result blocks'. The
    stored history is never rewritten; this only cleans the in-memory view."""
    return sanitize_pairs(messages)[0]


def sanitize_pairs(
    messages: list[Message],
    start_idx: int = 0,
    idxs: list[int] | None = None,
) -> tuple[list[Message], list[int]]:
    """sanitize_history plus the store index of every kept message — the view
    must know which store row each message came from, because a repaired hole
    (a dropped legacy orphan) shifts every later view position off its idx.

    `idxs` is the true store idx of each input row, when the caller has them
    (Session.resume always passes the rows' real indexes, so a hole in the
    table is carried through faithfully). The default assumes the list is the
    contiguous run start_idx..start_idx+n-1 — true only while the table itself
    has no missing rows."""
    out: list[Message] = []
    out_idxs: list[int] = []
    row_idx = idxs if idxs is not None else range(start_idx, start_idx + len(messages))
    n = len(messages)
    i = 0
    while i < n:
        m = messages[i]
        if m.role == "assistant" and m.tool_calls:
            nxt = messages[i + 1] if i + 1 < n else None
            if nxt is not None and nxt.role == "tool":
                out.append(m)  # complete tool_use/tool_result pair
                out_idxs.append(row_idx[i])
                out.append(nxt)
                out_idxs.append(row_idx[i + 1])
                i += 2
                continue
            # Orphaned tool_use: keep any assistant prose (drop the dangling
            # calls), but drop the message entirely if that leaves it empty —
            # the API rejects an assistant message with no content. The raw
            # content_blocks are dropped too: they embed the stranded tool_use
            # (and any thinking block's signature would dangle with it).
            if m.text:
                out.append(Message(role="assistant", text=m.text))
                out_idxs.append(row_idx[i])
            i += 1
            continue
        if m.role == "tool":
            i += 1  # a tool_result with no owning tool_use in front of it
            continue
        out.append(m)
        out_idxs.append(row_idx[i])
        i += 1
    return out, out_idxs


def _checkpoint_from_row(row: dict | None) -> Checkpoint | None:
    if row is None:
        return None
    # A corrupt row (covers past the anchor) must not fabricate an opening out
    # of summarized rows — clamp, so the view stays a valid opening+tail split.
    return Checkpoint(
        id=row["id"],
        covers_upto_idx=min(row["covers_upto_idx"], row["anchor_idx"]),
        anchor_idx=row["anchor_idx"],
        summary_text=row["summary_text"],
        summarized=bool(row["summarized"]),
        est_before=row["est_before"],
        est_after=row["est_after"],
        created_at=row["created_at"],
    )


def _checkpoint_view(
    checkpoint: Checkpoint, stored: list[Message]
) -> tuple[list[Message], list[int]]:
    """The compacted view rebuilt from stored bytes: the frozen opening (rows
    below covers_upto_idx, tool results re-truncated identically), the
    synthesized checkpoint message, and the sanitized tail from the anchor.
    The elided middle is never even deserialized."""
    opening, opening_idxs = sanitize_pairs(
        stored[: checkpoint.covers_upto_idx]
    )
    merged = Message(
        role="user",
        text=checkpoint_message_text(
            checkpoint.summary_text, checkpoint.created_at
        ),
    )
    tail, tail_idxs = sanitize_pairs(
        stored[checkpoint.anchor_idx :], start_idx=checkpoint.anchor_idx
    )
    return (
        freeze_opening(opening) + [merged] + tail,
        opening_idxs + [SYNTH_IDX] + tail_idxs,
    )
