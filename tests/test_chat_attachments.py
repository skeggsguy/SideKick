"""Chat route wiring for image + document attachments (plan image-attachments
Step 4, doc-attachments Steps 2 & 5). Step 5 adds the seam (the hint's jail
path is one the real copy_file reads) plus a live-tier fixture-PDF turn.

POST /api/chat resolves attachment_ids to the stored {id, mime, path, name,
size} shape BEFORE the run starts (unknown id -> 404, no agent built), and
start_chat_run passes them through to run_turn. The messages route answers
with {id, mime, name, size} only — the server-internal path never reaches
the browser, and the persisted user text stays clean of the wire hint.
(Compaction-checkpoint-bookmark Step 5 extended this file's pinned shape:
rows gained `idx`, the envelope gained `compactions` — see the two Step-5
assertions below.)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values, load_dotenv

from harness.composition import build_agent
from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus
from harness.core.session import Session
from harness.core.types import Message
from harness.gateway import media
from harness.gateway.app import Gateway, create_app
from harness.gateway.routes import streaming as streaming_routes
from harness.gateway.routes.chat_run import ChatRequest, start_chat_run
from harness.providers._anthropic_wire import _doc_hint
from harness.store.approvals import ApprovalStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore
from harness.tools.files import CopyFileTool

from test_gateway import FakeAgent
from test_media import _PDF_BYTES, _jpeg_bytes
from test_read_document import FIXTURE_MARKER, make_fixture_pdf

# The live test's skip check reads .env WITHOUT loading it (the trap is
# documented in test_live_smoke): a module-level load_dotenv() would leak the
# real HARNESS_LOCAL_URL into every offline test. The load lives in _live.
_LIVE_ENV = {**dotenv_values(), **os.environ}


class AttachmentAgent(FakeAgent):
    """FakeAgent that forwards attachments into add_user the way the real
    Agent does (Step 1), so a completed run persists them for the round trip.
    Records the full run_turn kwargs like the base fake."""

    async def run_turn(self, session, user_input, **kwargs):
        self.kwargs = kwargs
        await session.add_user(user_input, kwargs.get("attachments"))
        await session.add_assistant("a cat")
        return "a cat"


@pytest.fixture
def file_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    return tmp_path


@pytest.fixture
def _live():
    """Loads .env for the live test ONLY — see the _LIVE_ENV note above."""
    load_dotenv()


@pytest.fixture
async def client(db, file_root):
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: FakeAgent(),
    )
    app = create_app(gw)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    ) as c:
        c.gw = gw
        yield c


async def _drain_chat(client, body: dict) -> list[dict]:
    events = []
    async with client.stream("POST", "/api/chat", json=body) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


# ---- ChatRequest schema -------------------------------------------------------


def test_chat_request_attachment_ids_schema():
    # Exact equality per the request-shape rule: default is [], explicit
    # values ride through unchanged.
    assert ChatRequest(text="hi").attachment_ids == []
    assert ChatRequest(text="hi", attachment_ids=["ab.png"]).attachment_ids == [
        "ab.png"
    ]


# ---- start_chat_run passthrough -------------------------------------------------


async def test_start_chat_run_forwards_attachments_to_run_turn(client):
    agent = AttachmentAgent()
    client.gw.agent_factory = lambda autonomy: agent
    atts = [
        {
            "id": "ab12cd34ef56ab78.png",
            "mime": "image/png",
            "path": "/srv/uploads/ab12cd34ef56ab78.png",
        }
    ]
    run = await start_chat_run(client.gw, "what is this?", None, attachments=atts)
    await run.task

    assert agent.kwargs["attachments"] == atts
    (user,) = [m for m in run.session.messages if m.role == "user"]
    assert user.attachments == atts


async def test_start_chat_run_without_attachments_passes_none(client):
    """The unchanged no-attachment path: run_turn gets attachments=None, the
    user row persists attachments == [] (the Step 1 default)."""
    agent = AttachmentAgent()
    client.gw.agent_factory = lambda autonomy: agent
    run = await start_chat_run(client.gw, "hi", None)
    await run.task

    assert agent.kwargs["attachments"] is None
    (user,) = [m for m in run.session.messages if m.role == "user"]
    assert user.attachments == []


# ---- POST /api/chat resolution --------------------------------------------------


async def test_chat_unknown_attachment_404s_before_any_agent(client):
    """The resolve happens before start_chat_run: a factory that would raise
    proves no agent was built, and the sessions list proves no session was
    created. Malformed and well-formed-but-missing ids answer the same 404."""
    built: list = []

    def factory(autonomy, **kw):
        built.append(autonomy)
        raise AssertionError("agent factory must not run")

    client.gw.agent_factory = factory
    for bad in ("zz.png", "deadbeefdeadbeef.png", "../../uploads/x.jpg"):
        resp = await client.post(
            "/api/chat", json={"text": "hi", "attachment_ids": [bad]}
        )
        assert resp.status_code == 404, bad
        assert "unknown attachment" in resp.json()["detail"], bad
    assert built == []
    assert (await client.get("/api/sessions")).json() == []


async def test_chat_one_bad_id_among_good_ones_still_404s(client, file_root):
    """All-or-nothing: an unknown id next to a saved one is still a 404 with
    no agent — a half-resolved attachment list must never reach the model."""
    saved = media.save_image(_jpeg_bytes())
    built: list = []

    def factory(autonomy, **kw):
        built.append(autonomy)
        raise AssertionError("agent factory must not run")

    client.gw.agent_factory = factory
    resp = await client.post(
        "/api/chat",
        json={"text": "hi", "attachment_ids": [saved["id"], "0" * 15 + "a.png"]},
    )
    assert resp.status_code == 404
    assert built == []


async def test_chat_attachment_vanishing_before_the_size_stat_is_404(
    client, monkeypatch
):
    """size is stat()ed AFTER resolve(); a file deleted in that window must
    answer the same clean 404 as an unknown id, not a 500."""
    saved = media.save_image(_jpeg_bytes())
    real_resolve = streaming_routes.resolve

    def resolve_then_vanish(att_id):
        info = real_resolve(att_id)
        if info is not None:
            Path(info["path"]).unlink()  # the resolve -> stat race window
        return info

    monkeypatch.setattr(streaming_routes, "resolve", resolve_then_vanish)
    resp = await client.post(
        "/api/chat", json={"text": "hi", "attachment_ids": [saved["id"]]}
    )
    assert resp.status_code == 404
    assert "unknown attachment" in resp.json()["detail"]


# ---- round trip: upload -> chat -> persisted -> messages route ------------------


async def test_attachment_round_trip_messages_route_never_leaks_path(client):
    saved = (await client.post("/api/media", content=_jpeg_bytes())).json()
    agent = AttachmentAgent()
    client.gw.agent_factory = lambda autonomy: agent

    events = await _drain_chat(
        client, {"text": "what is this?", "attachment_ids": [saved["id"]]}
    )
    assert events[-1]["type"] == "done"
    session_id = events[0]["session_id"]

    # What run_turn received is exactly the stored shape: no url key, the
    # path is the resolved file (str-coerced) the upload wrote, name is the
    # sidecar value ("" for images), size the stored bytes.
    resolved = media.resolve(saved["id"])
    assert agent.kwargs["attachments"] == [
        {
            "id": saved["id"],
            "mime": "image/jpeg",
            "path": str(resolved["path"]),
            "name": "",
            "size": resolved["path"].stat().st_size,
        }
    ]

    resp = await client.get(f"/api/sessions/{session_id}/messages")
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # Shape extension (compaction-checkpoint-bookmark Step 5): every row
    # carries its STORE idx (the space compaction anchors address), and the
    # envelope gained `compactions` — [] until a compaction row exists.
    assert [m["idx"] for m in msgs] == [0, 1]
    assert resp.json()["compactions"] == []
    user, assistant = msgs
    assert user["attachments"] == [
        {
            "id": saved["id"],
            "mime": "image/jpeg",
            "name": "",
            "size": resolved["path"].stat().st_size,
        }
    ]
    assert set(user["attachments"][0]) == {"id", "mime", "name", "size"}  # exact keys
    assert assistant["attachments"] == []
    # The leak pin in raw-bytes form: neither the field name nor the jail
    # location appears anywhere in the payload.
    assert '"path"' not in resp.text
    assert '"url"' not in resp.text
    assert "uploads" not in resp.text


async def test_doc_attachment_round_trip_carries_name_and_size(client):
    """Plan doc-attachments Step 2: an uploaded document's sidecar name and
    byte size ride resolve -> run_turn -> store -> messages route. The jail
    path stays server-side, and so does the model hint: the PERSISTED user
    text is clean — the hint is built at wire translation only, so it can
    never paint into a bubble or seed the egress ledger."""
    saved = (
        await client.post(
            "/api/media", content=_PDF_BYTES, params={"name": "Invoice #7.pdf"}
        )
    ).json()
    agent = AttachmentAgent()
    client.gw.agent_factory = lambda autonomy: agent

    events = await _drain_chat(
        client, {"text": "what are the terms?", "attachment_ids": [saved["id"]]}
    )
    assert events[-1]["type"] == "done"
    session_id = events[0]["session_id"]

    resolved = media.resolve(saved["id"])
    assert resolved["name"] == "Invoice_7.pdf"  # sanitized on read (Step 1)
    assert agent.kwargs["attachments"] == [
        {
            "id": saved["id"],
            "mime": "application/pdf",
            "path": str(resolved["path"]),
            "name": "Invoice_7.pdf",
            "size": len(_PDF_BYTES),  # raw bytes, no re-encode
        }
    ]

    resp = await client.get(f"/api/sessions/{session_id}/messages")
    user = resp.json()["messages"][0]
    assert user["text"] == "what are the terms?"  # hint NOT persisted
    assert user["attachments"] == [
        {
            "id": saved["id"],
            "mime": "application/pdf",
            "name": "Invoice_7.pdf",
            "size": len(_PDF_BYTES),
        }
    ]
    assert '"path"' not in resp.text
    assert '"url"' not in resp.text
    assert "uploads" not in resp.text


async def test_legacy_attachment_rows_gain_empty_name_and_zero_size(client):
    """Rows persisted before Step 2 carry {id, mime, path} only; the messages
    route fills name/size with defaults — and still never leaks the path."""
    sid = await client.gw.sessions.create()
    await client.gw.sessions.append_message(
        sid,
        0,
        Message(
            role="user",
            text="old row",
            attachments=[
                {
                    "id": "ab12cd34ef56ab78.png",
                    "mime": "image/png",
                    "path": "/srv/uploads/ab12cd34ef56ab78.png",
                }
            ],
        ),
    )
    resp = await client.get(f"/api/sessions/{sid}/messages")
    msgs = resp.json()["messages"]
    assert msgs[0]["attachments"] == [
        {"id": "ab12cd34ef56ab78.png", "mime": "image/png", "name": "", "size": 0}
    ]
    assert '"path"' not in resp.text
    assert "/srv/" not in resp.text


async def test_message_rows_carry_store_idx_not_position(client):
    """Step 5's pin for `idx`: the table can hold holes (UNIQUE
    (session_id, idx) never refills a gap a swallowed append left), so the
    wire row must carry the STORE idx the seam anchors address — array
    position would silently misanchor every reload seam. Rows at 0 and 2
    (hole at 1) answer idx [0, 2], never [0, 1]."""
    sid = await client.gw.sessions.create()
    for idx in (0, 2):
        await client.gw.sessions.append_message(
            sid, idx, Message(role="user", text=f"row {idx}")
        )
    resp = await client.get(f"/api/sessions/{sid}/messages")
    body = resp.json()
    assert [m["idx"] for m in body["messages"]] == [0, 2]
    assert body["compactions"] == []


# ---- Seam: the hint's path is a real path (plan doc-attachments Step 5) ----


async def test_hint_path_is_a_real_copy_file_source(client, file_root):
    """"Save that to the scratchpad as invoice.pdf" is the model calling
    copy_file on the very path the hint showed it. End to end through the
    real parts: POST /api/media upload, real _doc_hint, real CopyFileTool —
    the path extracted FROM THE HINT STRING lands a byte-identical copy
    beside uploads/, inside the write root."""
    pdf = make_fixture_pdf()
    saved = (
        await client.post(
            "/api/media", content=pdf, params={"name": "fixture invoice.pdf"}
        )
    ).json()
    resolved = media.resolve(saved["id"])
    hint = _doc_hint(
        {
            "id": saved["id"],
            "mime": resolved["mime"],
            "path": str(resolved["path"]),
            "name": resolved["name"],
            "size": len(pdf),
        }
    )
    assert hint is not None
    path_in_hint = hint.rsplit(" — at ", 1)[1].split("; read with ")[0]

    dest = file_root / "invoice.pdf"  # beside uploads/, like scratch/invoice.pdf
    out = await CopyFileTool().run({"src": path_in_hint, "dest": str(dest)})

    assert out.startswith("Copied"), out
    assert dest.read_bytes() == pdf  # binary-safe: the real container survived


# ---- Live tier (`uv run pytest -m live`): the one real-model doc turn ------


@pytest.mark.live
@pytest.mark.skipif(
    not _LIVE_ENV.get("HARNESS_LOCAL_URL"),
    reason="HARNESS_LOCAL_URL not set (the live tier drives the local server)",
)
async def test_live_document_turn_reads_the_pdf_and_quotes_it(_live, file_root):
    """The plan's Step 5 gate: a GENERATED fixture PDF attached through
    run_turn must draw a read_document call from the local model (the hint
    line is the only thing pointing at the file) and the answer must quote
    the fixture text. file_root keeps the fixture out of the real uploads."""
    agent, _ = build_agent()
    tool_calls: list[str] = []
    agent.on_tool_call = lambda name, args: tool_calls.append(name)

    pdf = make_fixture_pdf()
    saved = media.save_document(pdf, "fixture-invoice.pdf")
    assert saved is not None
    att = {
        "id": saved["id"],
        "mime": saved["mime"],
        "path": saved["path"],
        "name": saved["name"],
        "size": len(pdf),
    }

    # wait_for bounds the turn: a wedged shared slot fails the test instead
    # of leaving this process holding :8080 forever (gotchas: orphan runs).
    answer = await asyncio.wait_for(
        agent.run_turn(
            Session(),
            "I attached an invoice PDF. What are the payment terms? "
            "Quote the payment terms line from the document exactly.",
            attachments=[att],
        ),
        timeout=600,
    )

    assert "read_document" in tool_calls, (
        f"the model never opened the document; answer was: {answer!r}"
    )
    assert FIXTURE_MARKER in (answer or ""), (
        f"the answer does not quote the fixture text: {answer!r}"
    )
