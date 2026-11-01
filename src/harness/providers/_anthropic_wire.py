"""Internal Message list <-> Anthropic content-block wire format, plus the
local lane's effort mapping. Pure translation — the provider class owns the
client, streaming and cost; this owns what goes on the wire.

Thinking-block policy lives with the translation: signatures round-trip
verbatim only inside the current tool loop (cloud) or always (local lane,
whose template re-renders prior reasoning and whose prefix cache depends on
it)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from ..core.types import Message

# Local lane: DEFAULT output tokens reserved for the answer beyond the
# thinking budget (the budget is a safety cap so a long think can't consume
# the whole output). Per-provider via _local_effort_params' answer_headroom.
LOCAL_ANSWER_HEADROOM = 4096


def _local_effort_params(
    effort: str, max_tokens: int, answer_headroom: int = LOCAL_ANSWER_HEADROOM
) -> dict[str, Any]:
    """Local-lane effort mapping. The fork's /v1/messages ignores adaptive /
    disabled / output_config and passes chat_template_kwargs through, so the
    template flags are ALWAYS explicit (its implicit default is xhigh). off
    renders no thinking; the others also cap thinking with an `enabled` budget
    so a long think can't consume the whole output. `answer_headroom` is that
    output reserve: compaction passes its full summary-band ceiling so the
    guaranteed think space never competes with a band-ceiling summary
    (Step 4); every other caller keeps the default."""
    # The template renders this instruction FIRST: changing effort mid-chat busts the prefix cache.
    if effort == "off":
        return {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    return {
        "thinking": {
            "type": "enabled",
            "budget_tokens": max(max_tokens - answer_headroom, 1024),
        },
        "extra_body": {
            "chat_template_kwargs": {
                "enable_thinking": True,
                "reasoning_effort": effort,
            }
        },
    }



_THINKING_BLOCK_TYPES = ("thinking", "redacted_thinking")


def _to_api_messages(
    messages: list[Message],
    thinking_on: bool,
    *,
    keep_all_thinking: bool = False,
    load_image: Callable[[str], bytes] | None = None,
) -> list[dict]:
    """Map the internal history to API messages. ``load_image`` reads an
    attachment's file bytes (the provider passes the real loader; translation
    itself stays pure); without it, user attachments emit no image blocks.
    Cloud: thinking blocks
    round-trip verbatim only within the current tool loop (after the last real
    user turn) and only while thinking is on; everywhere else they are stripped
    (rejected when thinking is disabled; ignored-but-billed cross-turn).

    Local lane (``keep_all_thinking``): ALWAYS kept, every turn, any effort.
    The Qwen template re-renders prior reasoning (preserve_thinking defaults
    true), so stripping it makes the prompt diverge at the first prior-turn
    assistant message and the hybrid model's server re-prefills everything
    after its nearest checkpoint — the whole conversation, every turn."""
    turn_start = max(
        (i for i, m in enumerate(messages) if m.role == "user"), default=-1
    )
    api = [
        _to_anthropic_message(
            m,
            keep_thinking=keep_all_thinking or (thinking_on and i > turn_start),
            load_image=load_image,
        )
        for i, m in enumerate(messages)
    ]
    _mark_history_breakpoint(api)
    return api


def _mark_history_breakpoint(api: list[dict]) -> None:
    """Moving cache breakpoint on the newest user-role API message (a plain
    user turn or the latest tool_result batch): everything before it becomes a
    cache READ on the next call, instead of re-billed input. Second of the 4
    allowed breakpoints (system+tools holds the first). Exactly one, always on
    the tail — mid-loop the mark advances with each tool_result, cross-turn the
    prior prefix stays byte-stable because thinking blocks are stripped and
    user content is always block-form. Never lands on a thinking block
    (user-role messages can't contain them) and never on an image block:
    attachment images sit AFTER the text block and must stay OUTSIDE the
    cached prefix — a mark on an image re-bills its bytes on every cache
    write. So the mark is the last TEXT block of the message; a tool_result
    batch has no text block and keeps marking its last block (unchanged); a
    text-less pure-image turn stays unmarked rather than marking an image.
    Exactly one mark, with that one exception."""
    for msg in reversed(api):
        if msg["role"] != "user":
            continue
        content = msg["content"]
        if isinstance(content, list) and content:
            idx = next(
                (
                    i
                    for i in range(len(content) - 1, -1, -1)
                    if content[i].get("type") == "text"
                ),
                None,
            )
            if idx is None:
                if content[-1].get("type") == "image":
                    return
                idx = len(content) - 1
            content[idx] = {**content[idx], "cache_control": {"type": "ephemeral"}}
        return


def _fmt_size(n: float) -> str:
    """Compact human size for the document hint — same one-decimal style
    read_document uses in its own cap message."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _doc_hint(att: dict) -> str | None:
    """One bracketed line telling the model a DOCUMENT is attached: its
    original name, mime, size, jail path and which tool reads it — or None
    for images (those ride real image blocks) and malformed entries.

    The line rides INSIDE the user text block (see _to_anthropic_message):
    a block added after the breakpoint target would move the cache boundary
    and re-bill the turn. It is a pure function of fields ALREADY persisted
    on the attachment dict (no fs/env/clock reads), so re-translating a
    history message reproduces its bytes exactly — the prefix cache depends
    on that. The jail path reaches the MODEL by design (on-demand reading,
    .claude/decisions 2026-10-19); the browser-facing route strips to
    {id, mime, name, size} before the wire (gateway/routes/resources.py)."""
    if not isinstance(att, dict):
        return None
    mime, path = att.get("mime"), att.get("path")
    if not mime or not path or mime.startswith("image/"):
        return None
    name = att.get("name") or att.get("id") or "attachment"
    tool = "read_file" if mime.startswith("text/") else "read_document"
    size = att.get("size")
    # isinstance guard: bool is an int subclass but True-as-bytes is nonsense,
    # and a legacy/hand-built dict may carry size=None or a str.
    size_part = (
        f", {_fmt_size(size)}" if isinstance(size, (int, float)) and size > 0 else ""
    )
    return f"[attached document: {name} — {mime}{size_part} — at {path}; read with {tool}]"


def _image_blocks(
    m: Message, load_image: Callable[[str], bytes] | None
) -> list[dict[str, Any]]:
    """Base64 image blocks for a user message's IMAGE attachments, in
    attachment order, each ``{"type": "image", "source": {"type": "base64",
    ...}}``. Non-image mimes (documents) emit no block here — they ride the
    text-block hint (_doc_hint); base64-ing a PDF as an image would be a
    malformed request. Bytes come from the injected loader (the provider
    reads the file jail's paths; tests feed fake bytes) so translation stays
    pure. A caller without a loader emits nothing; an attachment missing mime
    or path (or not even a dict) is skipped — never raise, never emit a
    malformed block."""
    if load_image is None:
        return []
    blocks: list[dict[str, Any]] = []
    # `or []`: a hand-built Message can carry attachments=None (the store's
    # _message_from_dict normalizes that, direct construction does not).
    for att in m.attachments or []:
        if not isinstance(att, dict):
            continue
        mime, path = att.get("mime"), att.get("path")
        if not mime or not path or not mime.startswith("image/"):
            continue
        # A deleted jail file RAISES here (no fallback), and the attachment is
        # persisted history: every later turn fails — never rm a live session's uploads.
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": base64.b64encode(load_image(path)).decode(),
                },
            }
        )
    return blocks


def _to_anthropic_message(
    m: Message,
    keep_thinking: bool = False,
    load_image: Callable[[str], bytes] | None = None,
) -> dict[str, Any]:
    """Convert one internal Message to an Anthropic API message dict.

    Assistant messages carrying raw ``content_blocks`` are sent verbatim
    (required for thinking-block signatures to validate). ``keep_thinking``
    is True only for messages inside the current tool loop with thinking on;
    otherwise thinking/redacted_thinking blocks are stripped.
    """
    if m.role == "user":
        # Always block-form (not a bare string) so the serialized shape is
        # identical whether or not this message carries the moving cache
        # breakpoint — a string-vs-block flip between calls would change the
        # prefix bytes and defeat the cache. Image blocks go AFTER the text
        # block so the cached prefix (up to the text block, see
        # _mark_history_breakpoint) excludes the attachment bytes. Document
        # hints append INSIDE the text block (one line each) — never a new
        # block after the breakpoint target — and are a pure function of the
        # persisted attachment fields, so history re-serializes byte-identic
        # every call. Image-only turns gain neither hints nor blocks: their
        # wire bytes are unchanged from the pre-document shape.
        images = _image_blocks(m, load_image)
        hints = [h for att in (m.attachments or []) if (h := _doc_hint(att))]
        text = "\n".join([m.text, *hints]) if m.text else "\n".join(hints)
        if not text:
            # Empty text stays a string (the API rejects empty text blocks;
            # empty user turns shouldn't exist) unless it's pure attachment.
            return {"role": "user", "content": images if images else ""}
        return {
            "role": "user",
            "content": [{"type": "text", "text": text}, *images],
        }

    if m.role == "assistant":
        if m.content_blocks:
            blocks = (
                m.content_blocks
                if keep_thinking
                else [
                    b
                    for b in m.content_blocks
                    if b.get("type") not in _THINKING_BLOCK_TYPES
                ]
            )
            if blocks:
                return {"role": "assistant", "content": blocks}
            # Everything was thinking (shouldn't be persisted, but be safe):
            # fall through to the legacy reconstruction.
        content: list[dict[str, Any]] = []
        if m.text:
            content.append({"type": "text", "text": m.text})
        for tc in m.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )
        return {"role": "assistant", "content": content}

    if m.role == "tool":
        # Tool results are delivered back to Claude as a user-role message
        # containing tool_result blocks.
        content = [
            {
                "type": "tool_result",
                "tool_use_id": tr.tool_call_id,
                "content": tr.content,
                "is_error": tr.is_error,
            }
            for tr in m.tool_results
        ]
        return {"role": "user", "content": content}

    raise ValueError(f"Unknown message role: {m.role}")
