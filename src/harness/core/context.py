"""Client-side context compaction.

A long session eventually costs more to resend every turn than it is worth.
When the estimated size of the history crosses a budget, the MIDDLE is
summarized (utility model) and the sent context becomes three parts: the
frozen OPENING (first exchange, verbatim, oversized tool results truncated),
a synthesized <context_checkpoint> message (framing + band-sized summary),
and the verbatim TAIL from the cut onward. A cut may land on a user text
message OR on a complete assistant tool_use/tool_result step boundary — so a
single long tool-loop turn is cuttable (the a88873342e1f trap) — and an
assistant tool_use is never separated from its tool_result.

The summary is persisted as a session checkpoint (summary text + store
indexes), so a resumed session rebuilds the SAME compacted view byte for byte
instead of re-summarizing every turn — stable prefix bytes are what keep the
local server's chat-slot cache warm (the per-turn cold re-prefill, learnings
2026-10-20). A later compaction folds the previous summary into the new
summarizer input and replaces the row; the messages table stays full history
(decision 2026-06-12).

The summarization half — the size band, the prompt, the transcript rendering
and the summary call — lives in core/summarize.py; this module drives it from
Compactor and re-exports those names for the established import surface.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict

from ..providers.base import LLMProvider
from ..store.db import utcnow
from ..store.runs import RunStore, trace_event
from .session import (
    SYNTH_IDX,
    Checkpoint,
    Session,
    checkpoint_message_text,
    freeze_opening,
)
from .summarize import (
    CHARS_PER_TOKEN,
    PIECE_CHARS,  # noqa: F401  (re-export: consumers import from here)
    SUMMARY_BAND_MAX,  # noqa: F401
    SUMMARY_BAND_MIN,
    SUMMARY_TARGET_SHARE,  # noqa: F401
    TRANSCRIPT_CHARS,  # noqa: F401
    _SUMMARY_BLOCK,  # noqa: F401
    render_transcript,
    summarize_middle,
    summary_system,  # noqa: F401
    summary_target_tokens,
)
from .types import Message, Usage

# Compact once the history estimate crosses this.
BUDGET_TOKENS = 100_000
# Keep at least this much recent conversation verbatim.
KEEP_TOKENS = 15_000

#: The live compaction hook the Agent carries (core/agent.py) and passes into
#: maybe_compact: ("start", None) immediately before the summarizer call,
#: ("done", info) after the trace row lands. The chat route renders the pair
#: as the compaction_start / compaction SSE frames (gateway/routes/_sse.py),
#: and "start" MUST fire only once compaction is certain — the caller cannot
#: know that before the call (a cut may turn out illegal), and an early
#: "start" without a "done" strands the live "Compacting…" indicator.
OnCompaction = Callable[[str, "dict | None"], None]


def _message_chars(m: Message) -> int:
    n = len(m.text or "")
    for tc in m.tool_calls:
        n += len(tc.name) + len(str(tc.arguments))
    for tr in m.tool_results:
        n += len(tr.content)
    # Thinking text rides in content_blocks (text/tool_use there duplicate the
    # derived fields above, so count only the thinking parts). It is resent as
    # input within the current tool loop, so it belongs in the budget.
    for b in m.content_blocks:
        if b.get("type") == "thinking":
            n += len(b.get("thinking") or "")
    return n


def estimate_tokens(messages: list[Message]) -> int:
    return sum(_message_chars(m) for m in messages) // CHARS_PER_TOKEN


def _opening_end(messages: list[Message]) -> int:
    """View position where the frozen opening ends: the first message after
    index 0 that starts a new user turn. The opening is the whole first
    exchange (question, tool calls, results, answer), never cut mid-pair —
    pairs sit between user turns, so this boundary is always safe."""
    i = 1
    while i < len(messages) and messages[i].role != "user":
        i += 1
    return i


def _is_cut_point(messages: list[Message], i: int) -> bool:
    """Legal cut positions: a user TEXT message, or an assistant message
    whose complete tool_use/tool_result pair rides along with the tail.
    Anything else could split an exchange the API requires together."""
    m = messages[i]
    if m.role == "user":
        return bool(m.text)
    if m.role == "assistant" and m.tool_calls:
        nxt = messages[i + 1] if i + 1 < len(messages) else None
        return (
            nxt is not None
            and nxt.role == "tool"
            and all(
                tc.id in {tr.tool_call_id for tr in nxt.tool_results}
                for tc in m.tool_calls
            )
        )
    return False


class Compactor:
    def __init__(
        self,
        summarizer: LLMProvider | None = None,
        budget_tokens: int = BUDGET_TOKENS,
        keep_tokens: int = KEEP_TOKENS,
    ) -> None:
        self.summarizer = summarizer
        self.budget_tokens = budget_tokens
        self.keep_tokens = keep_tokens

    async def maybe_compact(
        self,
        session: Session,
        run_id: str | None = None,
        runs: RunStore | None = None,
        total: Usage | None = None,
        on_compaction: OnCompaction | None = None,
    ) -> dict | None:
        """Compact the session in place when it is over budget. Returns a
        trace payload describing what happened, or None when nothing did.
        Never raises into the agent turn — a failed summary degrades to an
        elision note and the error travels in the trace. When the session has
        a store, the summary is persisted as a checkpoint row so a resume
        rebuilds this exact view instead of re-summarizing.

        With the reporting plumbing wired (the agent loop passes all of it;
        bare calls stay pure inspection + rewrite), a compaction reports
        itself: it traces its OWN run_events row (like MemoryExtractor),
        folds the summarizer's tokens into the run `total`, and drives the
        `on_compaction` hook — ("start", None) right before the slow
        summarizer call, ("done", info) after the row lands. The row shape is
        additive: pre-Step-3 readers (checkpoint tests, old traces) are
        untouched; Step 3 added checkpoint_id, anchor_idx (true store idx)
        and summary_usage."""
        messages = session.messages
        before = estimate_tokens(messages)
        if before <= self.budget_tokens:
            return None
        view_idxs = session.store_idxs
        # Where the opening ends / the elided middle starts. A session that is
        # already compacted keeps its frozen opening — the checkpoint message
        # sits at its end (marked by SYNTH_IDX), and the middle starts after
        # it. A fresh view's opening is the first exchange.
        cp_pos = self._checkpoint_position(session, messages, view_idxs)
        middle_start = cp_pos + 1 if cp_pos >= 0 else _opening_end(messages)
        # No second user turn yet: the opening IS the whole history, so there
        # is no elidable middle and no legal cut — same no-op as before this
        # step. (A first turn can be arbitrarily long; the opening is frozen
        # whole by design, so compaction cannot shrink it.)
        if middle_start >= len(messages):
            return None
        # The cut must start at or after middle_start+1 so the summarized
        # middle is non-empty, and never touches the opening or checkpoint.
        cut = self._cut_index(messages, middle_start + 1)
        if cut < middle_start + 1 or cut >= len(messages):
            return None

        middle = messages[middle_start:cut]
        prev = session.checkpoint.summary_text if session.checkpoint else None
        target = summary_target_tokens(estimate_tokens(middle))
        # The live signal starts exactly when the slow call does: with a
        # legal cut in hand, compaction is certain — the only earlier point
        # that could say "start" is before this call, where an uncuttable
        # history would silently strand it.
        if on_compaction is not None:
            on_compaction("start", None)
        summary, summarized, error, usage = await self._summarize(
            middle, prev, target
        )
        # The timestamp rides BOTH the framing text and the stored row, so
        # resume re-renders byte-identically (one renderer, one clock read).
        created_at = utcnow()[:19]
        checkpoint_msg = Message(
            role="user",
            text=checkpoint_message_text(summary, created_at),
        )
        # Map the cut through the view's store indexes: view position is not
        # store idx once a resume has repaired a legacy orphan.
        anchor_idx = view_idxs[cut]
        if cp_pos >= 0:
            opening, opening_idxs = messages[:cp_pos], view_idxs[:cp_pos]
            covers_upto = session.checkpoint.covers_upto_idx
        else:
            opening = freeze_opening(messages[:middle_start])
            opening_idxs = view_idxs[:middle_start]
            covers_upto = view_idxs[middle_start]
        session.replace_history(
            opening + [checkpoint_msg] + messages[cut:],
            opening_idxs + [SYNTH_IDX] + view_idxs[cut:],
        )
        after = estimate_tokens(session.messages)

        checkpoint = Checkpoint(
            id="",
            covers_upto_idx=covers_upto,
            anchor_idx=anchor_idx,
            summary_text=summary,
            summarized=summarized,
            est_before=before,
            est_after=after,
            created_at=created_at,
        )
        checkpoint_error = await self._persist_checkpoint(session, checkpoint)

        info = {
            "messages_summarized": len(middle),
            "est_tokens_before": before,
            "est_tokens_after": after,
            "summarized": summarized,
            "summary_target": target,
            # Honest trace (Step 3): which row this compaction wrote ("" when
            # the session has no store or the write failed — checkpoint_error
            # says which) and where the seam sits in the STORE. The reload UI
            # anchors its divider from anchor_idx, never from a fake message.
            "checkpoint_id": checkpoint.id,
            "anchor_idx": anchor_idx,
            # What compaction actually cost: the summarizer's own tokens,
            # once silently dropped. They ride the run totals just below.
            "summary_usage": asdict(usage) if usage else None,
        }
        if error:
            info["summary_error"] = error
        if checkpoint_error:
            info["checkpoint_error"] = checkpoint_error
        if total is not None and usage is not None:
            total.add(usage)
        # Best-effort, never raises (store/runs.trace_event) — and the hook
        # fires after the row so a live frame never races ahead of its record.
        await trace_event(runs, run_id, "compaction", info, source="agent")
        if on_compaction is not None:
            on_compaction("done", info)
        return info

    @staticmethod
    def _checkpoint_position(
        session: Session, messages: list[Message], view_idxs: list[int]
    ) -> int:
        """View position of the synthesized checkpoint message, or -1 when
        the view is uncompacted. The SYNTH_IDX marker on the view's store
        indexes is the ONLY reliable sign; a view assembled without them (a
        test assigning `messages` directly) reports positional indexes and
        will be read as uncompacted — nothing in src does that, and a caller
        that starts to must pass real indexes or it will re-freeze an already
        truncated opening (nested markers, cache break)."""
        if session.checkpoint is not None and SYNTH_IDX in view_idxs:
            return view_idxs.index(SYNTH_IDX)
        return -1

    async def _persist_checkpoint(
        self, session: Session, checkpoint: Checkpoint
    ) -> str | None:
        """Store the checkpoint row (when the session has a store) and make it
        the session's current one — the PREVIOUS SUMMARY source for the next
        compaction. A store-less session keeps it in memory only. A failed
        write must not abort the turn — the compacted view is already live, so
        it degrades to an in-memory checkpoint (the resume re-compacts) and the
        error travels in the trace, same as a failed summary."""
        error = None
        if session.store is not None and session.id is not None:
            try:
                checkpoint.id = await session.store.save_checkpoint(
                    session.id,
                    covers_upto_idx=checkpoint.covers_upto_idx,
                    anchor_idx=checkpoint.anchor_idx,
                    summary_text=checkpoint.summary_text,
                    summarized=checkpoint.summarized,
                    est_before=checkpoint.est_before,
                    est_after=checkpoint.est_after,
                    created_at=checkpoint.created_at,
                )
            except Exception as e:  # noqa: BLE001 - never raise into the turn
                error = str(e)
        session.checkpoint = checkpoint
        return error

    def _cut_index(self, messages: list[Message], lo: int) -> int:
        """A legal cut index at or above `lo` such that everything from it on
        stays under-ish keep_tokens — preferring the latest legal boundary
        (user text, or a complete tool_use/tool_result step). 0 means no
        valid cut exists. `lo` keeps the frozen opening and the checkpoint
        message out of reach."""
        keep_chars = self.keep_tokens * CHARS_PER_TOKEN
        acc = 0
        boundary = 0
        # A `lo` at or past the end leaves nothing to cut (the middle would be
        # empty) — returning 0 keeps the caller's "no cut exists" path hit
        # instead of indexing past the view.
        if lo >= len(messages):
            return 0
        for i in range(len(messages) - 1, -1, -1):
            acc += _message_chars(messages[i])
            if acc >= keep_chars:
                boundary = i
                break
        for i in range(max(boundary, lo), lo - 1, -1):
            if _is_cut_point(messages, i):
                return i
        return 0

    async def _summarize(
        self,
        middle: list[Message],
        prev_summary: str | None = None,
        target_tokens: int = SUMMARY_BAND_MIN,
    ) -> tuple[str, bool, str | None, Usage | None]:
        """Delegation to summarize.summarize_middle — the implementation
        moved there (Step 1 split); kept so the summarizer is still reached
        through the Compactor."""
        return await summarize_middle(
            self.summarizer, middle, prev_summary, target_tokens
        )

    def _render(self, middle: list[Message]) -> str:
        """Delegation to summarize.render_transcript (the implementation
        moved there)."""
        return render_transcript(middle)
