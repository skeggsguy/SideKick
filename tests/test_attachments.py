"""Image attachments: core plumbing (Step 1) and provider wire emission
(Step 2) of docs/plan-image-understanding.md, plus the document hint of
plan doc-attachments Step 2 (one line INSIDE the user text block; image
turns stay byte-identical).

Message.attachments is additive: it rides the JSON payload (asdict →
payload_json → _message_from_dict), so no DB migration is needed and legacy
rows without the key must load as []. The provider half pins the Anthropic
wire translation — image blocks after the text block, the moving cache
breakpoint never on an image — with a fake load_image; the media routes are a
later step.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import asdict

import pytest

from harness.core.session import Session
from harness.core.types import LLMResponse, Message
from harness.providers.anthropic_provider import (
    AnthropicProvider,
    _to_anthropic_message,
    _to_api_messages,
)
from harness.store.sessions import SessionStore, _message_from_dict

from test_agent_loop import ScriptedProvider, build_agent
from test_effort import _with_fake
from test_thinking import _cache_marks

ATTACHMENT = {
    "id": "ab12cd34ef56ab78",
    "mime": "image/png",
    "path": "/Users/tester/uploads/ab12cd34ef56ab78.png",
}

# Document attachment (plan doc-attachments Step 2): name + size ride the
# dict exactly as streaming.py persists them after resolving the upload.
DOC = {
    "id": "0f1e2d3c4b5a6978.pdf",
    "mime": "application/pdf",
    "path": "/Users/tester/uploads/0f1e2d3c4b5a6978.pdf",
    "name": "invoice.pdf",
    "size": 2_202_009,  # exactly 2.1 MB for the hint's renderer
}
DOC_HINT = (
    "[attached document: invoice.pdf — application/pdf, 2.1 MB — "
    "at /Users/tester/uploads/0f1e2d3c4b5a6978.pdf; read with read_document]"
)

# Fixture bytes — the fake loader never validates them and no real photo is
# ever read in an offline test.
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n fixture bytes, not a decodable image"
B64 = base64.b64encode(IMAGE_BYTES).decode()


def _att() -> list[dict]:
    return [dict(ATTACHMENT)]


def _loader(paths: list[str]) -> Callable[[str], bytes]:
    """Fake load_image: hands out IMAGE_BYTES and records the paths asked for."""

    def load_image(path: str) -> bytes:
        paths.append(path)
        return IMAGE_BYTES

    return load_image


def _image_block(mime: str = "image/png") -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": B64}}


# --- store round trip (the no-migration claim) ---------------------------------


async def test_user_attachments_round_trip_through_store(db):
    """asdict → append_message → load_messages preserves attachments exactly,
    in the payload JSON itself (additive field, schema untouched)."""
    store = SessionStore(db)
    sid = await store.create()
    await store.append_message(sid, 0, Message(role="user", text="what is this?", attachments=_att()))

    cur = await db.conn.execute(
        "SELECT payload_json FROM messages WHERE session_id = ? AND idx = 0",
        (sid,),
    )
    payload = json.loads((await cur.fetchone())["payload_json"])
    assert payload["attachments"] == _att()

    (loaded,) = await store.load_messages(sid)
    assert loaded == Message(role="user", text="what is this?", attachments=_att())


async def test_add_user_attachments_survive_resume(db):
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("describe this", attachments=_att())
    await session.add_assistant("a cat")

    resumed = await Session.resume(store, session.id)

    assert resumed is not None
    assert resumed.messages[0].attachments == _att()
    # Only user messages carry attachments; the assistant row stays [].
    assert resumed.messages[1].attachments == []


async def test_add_user_without_attachments_defaults_empty(db):
    """The additive default: existing callers (one positional arg) are
    unchanged and persist attachments == [], not None."""
    store = SessionStore(db)
    session = await Session.create(store)
    await session.add_user("plain turn")

    (loaded,) = await store.load_messages(session.id)
    assert loaded.attachments == []


# --- legacy rows (pre-attachments payloads) ------------------------------------


def test_message_from_dict_round_trips_attachments():
    msg = Message(role="user", text="hi", attachments=_att())
    assert _message_from_dict(asdict(msg)) == msg


def test_message_from_dict_legacy_row_has_no_attachments():
    """Rows persisted before images have no attachments key — and an explicit
    JSON null degrades the same way — so resume never trips on them."""
    legacy = {"role": "user", "text": "pre-image turn"}
    assert _message_from_dict(legacy).attachments == []
    nulled = {"role": "user", "text": "t", "attachments": None}
    assert _message_from_dict(nulled).attachments == []


# --- run_turn passthrough --------------------------------------------------------


class SpySession(Session):
    """Records the exact (text, attachments) each add_user call received —
    pins the run_turn → session wiring, not just the end state."""

    def __init__(self) -> None:
        super().__init__()
        self.add_user_calls: list[tuple[str, list[dict] | None]] = []

    async def add_user(
        self, text: str, attachments: list[dict] | None = None
    ) -> None:
        self.add_user_calls.append((text, attachments))
        await super().add_user(text, attachments)


async def test_run_turn_passes_attachments_to_add_user():
    provider = ScriptedProvider([LLMResponse(text="a photo of a cat")])
    agent = build_agent(provider, [])
    session = SpySession()

    await agent.run_turn(session, "what is this?", attachments=_att())

    assert session.add_user_calls == [("what is this?", _att())]
    (user_msg,) = [m for m in session.messages if m.role == "user"]
    assert user_msg.attachments == _att()
    # The provider sees the user Message carrying the attachments.
    assert provider.sent[0][-1].attachments == _att()


async def test_run_turn_without_attachments_is_unchanged():
    provider = ScriptedProvider([LLMResponse(text="hi")])
    agent = build_agent(provider, [])
    session = SpySession()

    await agent.run_turn(session, "hi")

    assert session.add_user_calls == [("hi", None)]
    (user_msg,) = [m for m in session.messages if m.role == "user"]
    assert user_msg.attachments == []


# --- provider wire emission (Step 2) ------------------------------------------


def test_user_text_plus_attachment_emits_image_block_after_text():
    paths: list[str] = []
    msg = Message(
        role="user",
        text="what is this?",
        # jpeg here, png in ATTACHMENT: pins media_type rides att["mime"].
        attachments=[{**ATTACHMENT, "mime": "image/jpeg"}],
    )
    out = _to_anthropic_message(msg, load_image=_loader(paths))
    assert out == {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            _image_block("image/jpeg"),
        ],
    }
    assert paths == [ATTACHMENT["path"]]


def test_multiple_attachments_emit_in_order_after_text():
    msg = Message(
        role="user",
        text="two shots",
        attachments=[
            {**ATTACHMENT, "id": "a", "path": "/u/a.jpg", "mime": "image/jpeg"},
            {**ATTACHMENT, "id": "b", "path": "/u/b.png"},
        ],
    )
    out = _to_anthropic_message(msg, load_image=_loader([]))
    assert out["content"] == [
        {"type": "text", "text": "two shots"},
        _image_block("image/jpeg"),
        _image_block("image/png"),
    ]


def test_history_breakpoint_marks_last_text_block_not_image():
    """The prefix-cache boundary: images sit OUTSIDE the cached prefix, so
    the moving mark stays on the last TEXT block — a mark on an image would
    re-bill its bytes on every cache write."""
    api = _to_api_messages(
        [
            Message(role="user", text="first"),
            Message(role="assistant", text="sure"),
            Message(role="user", text="and this?", attachments=_att()),
        ],
        thinking_on=False,
        load_image=_loader([]),
    )
    assert _cache_marks(api) == [(2, 0)], (
        "the breakpoint marks the LAST TEXT block, never an image — else the cloud "
        "lane re-bills image bytes each turn (the local fork ignores cache_control)"
    )
    assert api[2]["content"] == [
        {"type": "text", "text": "and this?", "cache_control": {"type": "ephemeral"}},
        _image_block(),
    ]


def test_pure_image_turn_emits_image_alone_and_stays_unmarked():
    api = _to_api_messages(
        [
            Message(role="user", text="earlier"),
            Message(role="assistant", text="ok"),
            Message(role="user", text=None, attachments=_att()),
        ],
        thinking_on=False,
        load_image=_loader([]),
    )
    assert api[2]["content"] == [_image_block()]
    # Unmarked beats marking an image: nothing is cached on attachment bytes.
    assert _cache_marks(api) == []


def test_empty_text_user_turn_stays_empty_string():
    """No attachments + empty text keeps the "" shape (the API rejects empty
    text blocks) — attachments are the only thing that flips it to blocks."""
    api = _to_api_messages(
        [Message(role="user", text=None)], thinking_on=False, load_image=_loader([])
    )
    assert api[0] == {"role": "user", "content": ""}


def test_loader_missing_emits_no_image_blocks_and_never_raises():
    """A caller that doesn't wire load_image gets the old shape, exactly."""
    api = _to_api_messages(
        [Message(role="user", text="hi", attachments=_att())], thinking_on=False
    )
    # The old shape exactly, moving breakpoint included.
    assert api[0]["content"] == [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}
    ]
    # Empty text + attachments + no loader: still "", no crash.
    assert _to_anthropic_message(Message(role="user", text="", attachments=_att())) == {
        "role": "user",
        "content": "",
    }


def test_malformed_attachments_skip_their_block_only():
    paths: list[str] = []
    msg = Message(
        role="user",
        text="mixed",
        attachments=[
            {"id": "no-path", "mime": "image/png"},
            {"id": "no-mime", "path": "/u/x.png"},
            "junk",
            ATTACHMENT,
        ],
    )
    out = _to_anthropic_message(msg, load_image=_loader(paths))
    assert out["content"] == [{"type": "text", "text": "mixed"}, _image_block()]
    assert paths == [ATTACHMENT["path"]]


def test_none_attachments_emit_text_only():
    """A hand-built Message(attachments=None) — direct construction skips
    _message_from_dict's normalization — must not TypeError in _image_blocks."""
    out = _to_anthropic_message(
        Message(role="user", text="x", attachments=None), load_image=lambda p: b""
    )
    assert out == {"role": "user", "content": [{"type": "text", "text": "x"}]}


def test_non_user_messages_never_gain_image_blocks():
    out = _to_anthropic_message(
        Message(role="assistant", text="hi", attachments=_att()), load_image=_loader([])
    )
    assert out == {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}


@pytest.mark.parametrize("lane", ["local", "sonnet"])
async def test_provider_reads_attachment_files_on_both_lanes(tmp_path, lane):
    """chat() supplies the real loader; the faked stream pins the exact wire
    message — bytes read from the attachment path, mark on the text block."""
    img = tmp_path / f"{ATTACHMENT['id']}.png"
    img.write_bytes(IMAGE_BYTES)
    provider = (
        AnthropicProvider(
            api_key="local",
            model="qwen3.8-flash-next",
            base_url="http://127.0.0.1:8080",
        )
        if lane == "local"
        else AnthropicProvider(
            api_key="x", model="claude-sonnet-5", thinking="adaptive"
        )
    )
    fake = _with_fake(provider)
    await provider.chat(
        "sys",
        [
            Message(
                role="user",
                text="what is this?",
                attachments=[{**ATTACHMENT, "path": str(img)}],
            )
        ],
        [],
    )
    assert fake.calls[-1]["messages"][0]["content"] == [
        {"type": "text", "text": "what is this?", "cache_control": {"type": "ephemeral"}},
        _image_block(),
    ]


# --- document hint inside the text block (plan doc-attachments Step 2) --------


def test_doc_hint_rides_inside_the_user_text_block():
    """One bracketed line (name + mime + size + jail path + tool pointer)
    appended to the text — never a second block, and the loader is never
    called: a PDF must never be base64'd into an image block."""
    paths: list[str] = []
    msg = Message(role="user", text="what are the terms?", attachments=[dict(DOC)])
    out = _to_anthropic_message(msg, load_image=_loader(paths))
    assert out == {
        "role": "user",
        "content": [
            {"type": "text", "text": f"what are the terms?\n{DOC_HINT}"}
        ],
    }
    assert paths == []


def test_text_document_hint_points_at_read_file():
    msg = Message(
        role="user",
        text="summarize",
        attachments=[{**DOC, "mime": "text/markdown", "name": "notes.md",
                      "size": 2200}],
    )
    out = _to_anthropic_message(msg, load_image=_loader([]))
    (block,) = out["content"]
    assert block["text"] == (
        "summarize\n[attached document: notes.md — text/markdown, 2.1 KB — "
        f"at {DOC['path']}; read with read_file]"
    )
    assert "read_document" not in block["text"]


def test_doc_hint_falls_back_to_id_and_omits_missing_size():
    """A hand-built or pre-name row still gets a usable hint: id as the
    label, no size segment when size is absent."""
    msg = Message(
        role="user",
        text="t",
        attachments=[{"id": "abc123.pdf", "mime": "application/pdf",
                      "path": "/u/abc123.pdf"}],
    )
    out = _to_anthropic_message(msg, load_image=_loader([]))
    (block,) = out["content"]
    assert block["text"] == (
        "t\n[attached document: abc123.pdf — application/pdf — "
        "at /u/abc123.pdf; read with read_document]"
    )


def test_multiple_docs_one_hint_line_each_in_order():
    doc2 = {**DOC, "id": "b.pdf", "name": "b.pdf", "path": "/u/b.pdf",
            "size": 999}
    msg = Message(role="user", text="two docs", attachments=[dict(DOC), doc2])
    out = _to_anthropic_message(msg, load_image=_loader([]))
    (block,) = out["content"]
    lines = block["text"].split("\n")
    assert lines[0] == "two docs"
    assert lines[1].startswith("[attached document: invoice.pdf")
    assert lines[2].startswith(
        "[attached document: b.pdf — application/pdf, 999 B — at /u/b.pdf"
    )


def test_malformed_doc_entries_emit_no_hint_line():
    msg = Message(
        role="user",
        text="mixed",
        attachments=["junk", {"id": "x.pdf"},
                     {"mime": "application/pdf", "path": ""}],
    )
    out = _to_anthropic_message(msg, load_image=_loader([]))
    assert out["content"] == [{"type": "text", "text": "mixed"}]


def test_image_turn_with_name_and_size_keys_is_byte_identical():
    """The regression pin: an image attachment that CARRIES the new name/size
    keys serializes exactly like one without them — the document step adds
    nothing to image turns."""
    plain = _to_api_messages(
        [Message(role="user", text="look", attachments=_att())],
        thinking_on=False, load_image=_loader([]),
    )
    with_extras = _to_api_messages(
        [Message(role="user", text="look",
                 attachments=[{**ATTACHMENT, "name": "photo.jpg", "size": 4096}])],
        thinking_on=False, load_image=_loader([]),
    )
    assert with_extras == plain


def test_doc_and_image_mixed_marks_text_block_only():
    """Breakpoint invariant survives the mix: the mark stays on the (now
    hint-carrying) text block, the image sits after it OUTSIDE the cached
    prefix, and only the image is loaded."""
    paths: list[str] = []
    api = _to_api_messages(
        [
            Message(role="user", text="first"),
            Message(role="assistant", text="sure"),
            Message(role="user", text="terms and a photo?",
                    attachments=[dict(DOC), dict(ATTACHMENT)]),
        ],
        thinking_on=False,
        load_image=_loader(paths),
    )
    assert _cache_marks(api) == [(2, 0)]
    assert api[2]["content"] == [
        {"type": "text", "text": f"terms and a photo?\n{DOC_HINT}",
         "cache_control": {"type": "ephemeral"}},
        _image_block(),
    ]
    assert paths == [ATTACHMENT["path"]], (
        "a document reached the image loader — _image_blocks must admit image/* "
        "only (docs ride the text hint); a base64'd PDF is a malformed request"
    )


def test_empty_text_with_doc_hint_becomes_hint_only_text_block():
    """Hint-less text would strand the document; a hint-only text block is
    the safe shape (the mark then lands on cheap text, never on an image)."""
    api = _to_api_messages(
        [Message(role="user", text=None, attachments=[dict(DOC)])],
        thinking_on=False, load_image=_loader([]),
    )
    assert api[0]["content"] == [
        {"type": "text", "text": DOC_HINT,
         "cache_control": {"type": "ephemeral"}}
    ]


def test_doc_hint_re_serializes_byte_identical_across_calls():
    """Prefix-cache stability: the hint is a pure function of the PERSISTED
    attachment fields, so a history message contributes identical bytes to
    every later call (no fs/env/clock reads during translation)."""
    msg = Message(role="user", text="terms?", attachments=[dict(DOC)])
    first = json.dumps(
        _to_api_messages([msg], thinking_on=False, load_image=_loader([])),
        sort_keys=True,
    )
    second = json.dumps(
        _to_api_messages([msg], thinking_on=False, load_image=_loader([])),
        sort_keys=True,
    )
    assert first == second


async def test_doc_name_and_size_round_trip_through_store(db):
    """name + size ride the payload JSON like every other attachment key —
    no migration, exact equality after resume."""
    store = SessionStore(db)
    sid = await store.create()
    await store.append_message(
        sid, 0, Message(role="user", text="read this", attachments=[dict(DOC)])
    )
    cur = await db.conn.execute(
        "SELECT payload_json FROM messages WHERE session_id = ? AND idx = 0",
        (sid,),
    )
    payload = json.loads((await cur.fetchone())["payload_json"])
    assert payload["attachments"] == [DOC]
    (loaded,) = await store.load_messages(sid)
    assert loaded.attachments == [DOC]
