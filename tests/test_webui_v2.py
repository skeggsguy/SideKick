"""v2 web UI surfaces: tools-security/allowlist, approvals history, trigger
enable, session rename, chat auto-naming, and the CSRF same-origin guard.

SSE endpoints are NOT read line-by-line here — httpx ASGITransport buffers an
unbounded stream (test_gateway's test_chat_stream_pings_while_the_model_is_silent
drains the body instead). The chat `done` event is finite, so
the auto-naming test reads it fine."""

from __future__ import annotations

import json
import re

import httpx
import pytest

from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus
from harness.core.naming import SessionNamer
from harness.gateway.app import Gateway, create_app
from harness.gateway.preview import ApprovalPreviews
from harness.providers.ollama_provider import OllamaProvider
from harness.store.approvals import ApprovalStore
from harness.store.audit import AllowlistStore, AuditStore
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore

from tests.test_gateway import FakeAgent
from conftest import gotcha


CATALOG = [
    {"name": "read_file", "description": "Read a file", "classification": "READ_ONLY", "remote": False},
    {"name": "edit_file", "description": "Edit a file", "classification": "MUTATING_LOCAL", "remote": False},
    {"name": "gmail_send", "description": "Send mail", "classification": "OUTWARD", "remote": True},
    {"name": "schedule_add", "description": "Schedule a job", "classification": "MUTATING_LOCAL", "remote": False},
]


class StubLocal:
    """Stands in for OllamaProvider in SessionNamer tests."""

    def __init__(self, result: str | None) -> None:
        self._result = result
        self.last_error = None

    async def title(self, text, instruction, max_chars=60):  # noqa: D401
        return self._result


def _make_app(db, **overrides):
    kwargs = dict(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy: FakeAgent(),
    )
    kwargs.update(overrides)
    gw = Gateway(**kwargs)
    return create_app(gw), gw


def _client(app):
    # Default same-origin Origin header: browsers send one on every non-GET
    # (the guard rejects Origin-less unsafe methods without a bearer token).
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"origin": "http://t"}
    )


# ---- OllamaProvider.title -------------------------------------------------

async def test_ollama_title_sanitizes_and_bounds():
    def handler(request):
        body = {"message": {"content": json.dumps({"title": "  Plan\nthe   trip  "})}}
        return httpx.Response(200, json=body)

    p = OllamaProvider(transport=httpx.MockTransport(handler))
    title = await p.title("plan a trip to japan", "instruction")
    assert title == "Plan the trip"  # whitespace collapsed


async def test_ollama_title_none_on_error():
    def handler(request):
        return httpx.Response(500)

    p = OllamaProvider(transport=httpx.MockTransport(handler))
    assert await p.title("hi", "instruction") is None


# ---- SessionNamer ---------------------------------------------------------

async def test_session_namer_writes_once(db):
    sessions = SessionStore(db)
    sid = await sessions.create(channel="web")
    namer = SessionNamer(StubLocal("My Chat"), sessions)

    assert await namer.name(sid, "first message") == "My Chat"
    listed = await sessions.list()
    assert listed[0]["title"] == "My Chat"

    # Idempotent: a second call must not clobber the existing title.
    assert await namer.name(sid, "another message") is None


async def test_session_namer_degrades_when_local_down(db):
    sessions = SessionStore(db)
    sid = await sessions.create(channel="web")
    namer = SessionNamer(StubLocal(None), sessions)
    assert await namer.name(sid, "x") is None
    assert (await sessions.list())[0]["title"] is None


# ---- allowlist store ------------------------------------------------------

async def test_allowlist_remove_and_list(db):
    store = AllowlistStore(db)
    await store.add("edit_file")
    assert await store.contains("edit_file")
    assert [r["tool"] for r in await store.list()] == ["edit_file"]
    assert await store.remove("edit_file") is True
    assert await store.remove("edit_file") is False
    assert not await store.contains("edit_file")


# ---- tools-security endpoints --------------------------------------------

async def test_tools_endpoint_marks_allowlistable(db):
    app, _ = _make_app(db, allowlist=AllowlistStore(db), tool_catalog=CATALOG)
    async with _client(app) as c:
        data = (await c.get("/api/tools")).json()
    by = {t["name"]: t for t in data["tools"]}
    assert by["edit_file"]["allowlistable"] is True
    assert by["gmail_send"]["allowlistable"] is False
    assert by["read_file"]["allowlistable"] is False
    # Always-ask schedule tools: MUTATING_LOCAL but never allowlistable.
    assert by["schedule_add"]["allowlistable"] is False
    assert by["schedule_add"]["always_ask"] is True
    assert by["edit_file"]["always_ask"] is False
    assert data["orphans"] == []


async def test_allowlist_rejects_outward(db):
    app, _ = _make_app(db, allowlist=AllowlistStore(db), tool_catalog=CATALOG)
    async with _client(app) as c:
        # OUTWARD can never be allowlisted — the server guard mirrors policy.py.
        assert (await c.post("/api/allowlist", json={"tool": "gmail_send"})).status_code == 422
        # Neither can always-ask schedule tools, MUTATING_LOCAL or not.
        assert (await c.post("/api/allowlist", json={"tool": "schedule_add"})).status_code == 422
        assert (await c.post("/api/allowlist", json={"tool": "nope"})).status_code == 404
        assert (await c.post("/api/allowlist", json={"tool": "edit_file"})).status_code == 200
        data = (await c.get("/api/tools")).json()
        assert next(t for t in data["tools"] if t["name"] == "edit_file")["currently_allowlisted"]
        assert (await c.delete("/api/allowlist/edit_file")).status_code == 200
        assert (await c.delete("/api/allowlist/edit_file")).status_code == 404


async def test_api_tools_serves_snapshot_never_builds_agent(db):
    """GET /api/tools must serve a cached snapshot taken at startup, never
    build an Agent on the request. Building an agent per request would:
    (1) pollute the classification cache with MCP metadata, (2) waste tokens
    on a real API call, (3) violate the architecture (tool registry is
    immutable post-startup).

    Pinned in tests/test_webui_v2.py."""
    def _boom(*args, **kwargs):
        raise AssertionError(gotcha(
            "api-tools-startup-snapshot",
            "GET /api/tools must serve the startup snapshot, never build an Agent (POST /api/allowlist "
            "422s from it; policy.py's call-time check stays the backstop — keep both)"
        ))

    app, _ = _make_app(db, allowlist=AllowlistStore(db), tool_catalog=CATALOG, agent_factory=_boom)
    async with _client(app) as c:
        r = await c.get("/api/tools")
    assert r.status_code == 200
    data = r.json()
    assert len(data["tools"]) == len(CATALOG)
    names = {t["name"] for t in data["tools"]}
    expected = {c["name"] for c in CATALOG}
    assert names == expected


# ---- approvals history ----------------------------------------------------

async def test_approvals_history(db):
    store = ApprovalStore(db)
    app, _ = _make_app(db, approval_store=store)
    aid = await store.create(None, "edit_file", {"path": "x"}, "MUTATING_LOCAL")
    await store.resolve(aid, "approved")
    async with _client(app) as c:
        hist = (await c.get("/api/approvals/history")).json()
        pending = (await c.get("/api/approvals")).json()
    assert len(hist) == 1 and hist[0]["status"] == "approved"
    assert hist[0]["tool"] == "edit_file"
    # Derived source/why fields ride the history payload (run_id=None →
    # unknown origin, but the keys must be present for the UI).
    assert hist[0]["source_kind"] == "unknown"
    assert hist[0]["why_gated"]  # MUTATING_LOCAL always gets an explanation
    assert pending == []  # resolved row no longer pending


# ---- trigger enable -------------------------------------------------------

async def test_trigger_enable_disable(db):
    triggers = TriggerStore(db)
    app, _ = _make_app(db, triggers=triggers)
    tid = await triggers.add("cron", "* * * * *", "pb")
    async with _client(app) as c:
        assert (await c.post(f"/api/triggers/{tid}/disable")).status_code == 200
        assert not (await triggers.get(tid)).enabled
        assert (await c.post(f"/api/triggers/{tid}/enable")).status_code == 200
        assert (await triggers.get(tid)).enabled
        assert (await c.post("/api/triggers/nope/enable")).status_code == 404


# ---- session rename -------------------------------------------------------

async def test_session_rename(db):
    sessions = SessionStore(db)
    app, _ = _make_app(db, sessions=sessions)
    sid = await sessions.create(channel="web")
    async with _client(app) as c:
        r = await c.patch(f"/api/sessions/{sid}", json={"title": "  Renamed  here  "})
        assert r.status_code == 200 and r.json()["title"] == "Renamed here"
        assert (await c.patch(f"/api/sessions/{sid}", json={"title": "   "})).status_code == 422
        assert (await c.patch("/api/sessions/nope", json={"title": "x"})).status_code == 404


# ---- single-session GET (pwa-accurate-resume boot-restore) ----------------

async def test_session_get_single_answers_gone_as_null(db):
    """GET /api/sessions/{id} is the PWA boot-restore existence check: the
    picker's list is capped at the 50 most recent (store/sessions.py), so a
    miss there cannot prove an older stored id DELETED. The answer is the
    session row (picker shape + `active`) or NULL — 200 + null, never 404:
    an agentd older than this route answers such GETs with FastAPI's own 404,
    and the client must tell "server says gone" (forget lastChat) from
    "server cannot say" (keep it). The null body IS the contract — pinned so
    a later REST-purist 404-ification cannot silently re-arm that bug."""
    sessions = SessionStore(db)
    app, gw = _make_app(db, sessions=sessions)
    sid = await sessions.create(channel="web")
    await sessions.update_title(sid, "Old chat")
    async with _client(app) as c:
        r = await c.get(f"/api/sessions/{sid}")
        assert r.status_code == 200
        row = r.json()
        assert (row["id"], row["title"], row["channel"]) == (sid, "Old chat", "web")
        assert {"created_at", "updated_at"} <= set(row)
        assert row["active"] is False
        # unknown id → 200 + null, NOT 404 (the deletion verdict vs no-route)
        r = await c.get("/api/sessions/nope")
        assert r.status_code == 200 and r.json() is None
        # live turn flags the row (route reads membership; a bare key suffices)
        gw.active_chats[sid] = object()
        assert (await c.get(f"/api/sessions/{sid}")).json()["active"] is True


# ---- chat auto-naming (finite done event) ---------------------------------

async def _chat_done(client, body):
    events = []
    async with client.stream("POST", "/api/chat", json=body) as resp:
        async for line in resp.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events[-1]


async def test_chat_done_carries_autoname(db):
    sessions = SessionStore(db)
    namer = SessionNamer(StubLocal("Generated Title"), sessions)
    app, _ = _make_app(db, sessions=sessions, namer=namer)
    async with _client(app) as c:
        done = await _chat_done(c, {"text": "hi", "session_id": None})
    assert done["type"] == "done" and done["title"] == "Generated Title"


async def test_chat_autoname_traced_on_turn_run(db):
    # The namer gets the turn's run_id, so naming shows up in run_events
    # (it used to be called without one and the trace silently no-oped).
    sessions, runs = SessionStore(db), RunStore(db)
    run_id = await runs.start(None, "fake-model", channel="web")

    class RunAgent(FakeAgent):
        async def run_turn(self, session, user_input, **kwargs):
            out = await super().run_turn(session, user_input, **kwargs)
            self.last_run = {"run_id": run_id}
            return out

    namer = SessionNamer(StubLocal("Generated Title"), sessions, runs)
    app, _ = _make_app(db, sessions=sessions, runs=runs, namer=namer,
                       agent_factory=lambda autonomy: RunAgent())
    async with _client(app) as c:
        await _chat_done(c, {"text": "hi", "session_id": None})
    kinds = [e["kind"] for e in await runs.events(run_id)]
    assert "session_named" in kinds


async def test_chat_fallback_title_when_namer_none(db):
    sessions = SessionStore(db)
    namer = SessionNamer(StubLocal(None), sessions)  # Ollama "down"
    app, _ = _make_app(db, sessions=sessions, namer=namer)
    async with _client(app) as c:
        done = await _chat_done(c, {"text": "Plan a trip to Tokyo", "session_id": None})
    # Degrades to a first-message snippet, not None / "New chat" hex.
    assert done["title"] == "Plan a trip to Tokyo"
    assert (await sessions.list())[0]["title"] == "Plan a trip to Tokyo"


async def test_early_title_arrives_before_turn_complete(db):
    """The title task starts from the on_run_start hook, so on a new chat the
    `title` event lands WHILE the answer is still streaming — the fix for
    chats sitting on "New chat" until after run_turn's extraction tail.
    (FakeAgent never fires hooks; this hook-firing fake pins the real path.)"""
    sessions = SessionStore(db)
    namer = SessionNamer(StubLocal("Early Title"), sessions)

    class HookAgent(FakeAgent):
        async def run_turn(self, session, user_input, **kwargs):
            if self.on_run_start is not None:
                self.on_run_start("run-early")
            if self.on_model_call is not None:
                self.on_model_call()
            out = await super().run_turn(session, user_input, **kwargs)
            if self.on_answer_complete is not None:
                self.on_answer_complete()
            return out

    app, _ = _make_app(db, sessions=sessions, namer=namer,
                       agent_factory=lambda autonomy: HookAgent())
    async with _client(app) as c:
        events = []
        async with c.stream("POST", "/api/chat", json={"text": "hi"}) as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))

    kinds = [e["type"] for e in events]
    assert set(kinds) == {
        "session", "model_wait", "delta", "title", "turn_complete", "done",
    }
    assert kinds.count("turn_complete") == 1  # hook fires it, fallback skipped
    assert kinds.index("title") < kinds.index("turn_complete"), (
        "naming must start off the critical path at on_run_start (shielded; "
        "title() never raises) — else a new chat sits on 'New chat' until "
        "the extraction tail ends"
    )
    assert events[kinds.index("title")]["title"] == "Early Title"
    assert events[-1]["title"] == "Early Title"  # done still carries it
    assert (await sessions.list())[0]["title"] == "Early Title"


async def test_unknown_session_id_not_autonamed(db):
    sessions = SessionStore(db)
    namer = SessionNamer(StubLocal("Should Not Apply"), sessions)
    app, _ = _make_app(db, sessions=sessions, namer=namer)
    async with _client(app) as c:
        # A non-empty but unknown id must NOT be treated as a new namable chat.
        done = await _chat_done(c, {"text": "hi", "session_id": "ffffffffffff"})
    assert done["title"] is None


async def test_decide_approval_applied_false_without_waiter(db):
    store = ApprovalStore(db)
    waiter = ApprovalWaiter(store)
    app, _ = _make_app(db, approval_store=store, waiter=waiter)
    # Row created directly (no waiter.request) ⇒ no live future to wake.
    aid = await store.create(None, "edit_file", {"p": "x"}, "MUTATING_LOCAL")
    async with _client(app) as c:
        r = await c.post(f"/api/approvals/{aid}", json={"approved": True})
        body = r.json()
        # Resolving an already-decided row 409s.
        again = await c.post(f"/api/approvals/{aid}", json={"approved": True})
    assert r.status_code == 200 and body == {"ok": True, "applied": False}
    assert again.status_code == 409


async def test_history_outcome_only_for_approved_single_match(db):
    store = ApprovalStore(db)
    audit = AuditStore(db)
    app, _ = _make_app(db, approval_store=store)

    # Approved + exactly one executed audit row → outcome shown.
    a1 = await store.create("run1", "calendar_event_create", {}, "MUTATING_LOCAL")
    await store.resolve(a1, "approved")
    await audit.log("run1", "calendar_event_create", {}, "MUTATING_LOCAL", "approved", "ok", False)

    # Denied → no outcome even if an audit row exists.
    a2 = await store.create("run2", "gmail_send", {}, "OUTWARD")
    await store.resolve(a2, "denied")
    await audit.log("run2", "gmail_send", {}, "OUTWARD", "denied", "blocked", True)

    # Approved but the same tool ran twice in the run → ambiguous → no outcome.
    a3 = await store.create("run3", "task_create", {}, "MUTATING_LOCAL")
    await store.resolve(a3, "approved")
    for _ in range(2):
        await audit.log("run3", "task_create", {}, "MUTATING_LOCAL", "approved", "ok", False)

    async with _client(app) as c:
        rows = {r["id"]: r for r in (await c.get("/api/approvals/history")).json()}
    assert rows[a1]["outcome_is_error"] is False
    assert rows[a2]["outcome_is_error"] is None   # denied → suppressed
    assert rows[a3]["outcome_is_error"] is None   # repeated tool → ambiguous


# ---- CSRF same-origin guard + bearer token ---------------------------------

def _bare_client(app):
    """Client with NO default Origin — a non-browser caller."""
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://t")


async def test_cross_origin_post_rejected(db):
    app, _ = _make_app(db, triggers=TriggerStore(db))
    async with _client(app) as c:
        # A mismatched Origin (malicious page) is rejected; same host is fine.
        bad = await c.post("/api/triggers/x/enable", headers={"origin": "http://evil.example"})
        assert bad.status_code == 403
        ok_host = await c.post("/api/triggers/x/enable", headers={"origin": "http://t"})
        assert ok_host.status_code == 404  # passes the guard, 404 = unknown trigger


async def test_originless_post_requires_bearer_token(db, monkeypatch):
    """Non-browser POSTs must present HARNESS_API_TOKEN — otherwise any
    local/tailnet process could release a parked approval unseen."""
    app, _ = _make_app(db, triggers=TriggerStore(db))
    async with _bare_client(app) as c:
        # No token configured: fail closed.
        monkeypatch.delenv("HARNESS_API_TOKEN", raising=False)
        assert (await c.post("/api/triggers/x/enable")).status_code == 403

        monkeypatch.setenv("HARNESS_API_TOKEN", "sekrit-token")
        no_auth = await c.post("/api/triggers/x/enable")
        assert no_auth.status_code == 403
        bad = await c.post(
            "/api/triggers/x/enable", headers={"authorization": "Bearer wrong"}
        )
        assert bad.status_code == 403
        good = await c.post(
            "/api/triggers/x/enable", headers={"authorization": "Bearer sekrit-token"}
        )
        assert good.status_code == 404  # past the guard, 404 = unknown trigger

        # GETs stay open for Origin-less clients (OAuth navigations, SSE).
        monkeypatch.delenv("HARNESS_API_TOKEN", raising=False)
        assert (await c.get("/api/health")).status_code == 200


# ---- approval previews ------------------------------------------------------

class StubDraftReader:
    """Stands in for an adapted READ_ONLY MCP tool (get_draft / read / info)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def run(self, args):
        self.calls.append(args)
        return self.payload


def _previews(handles=None, triggers=None):
    return ApprovalPreviews(tool_handles=handles or {}, triggers=triggers)


async def test_approval_preview_returns_draft_content(db):
    reader = StubDraftReader(json.dumps({
        "draft_id": "d1", "to": "sarah@example.com", "cc": "",
        "subject": "Invoice", "body": "Here you go.", "attachments": ["a.pdf"],
    }))
    app, gw = _make_app(db, previews=_previews({"gmail_get_draft": reader}))
    aid = await gw.approval_store.create(None, "gmail_send_draft", {"draft_id": "d1"}, "OUTWARD")
    async with _client(app) as c:
        r = await c.get(f"/api/approvals/{aid}/preview")
        assert r.status_code == 200
        body = r.json()
        assert body["to"] == "sarah@example.com"
        assert body["body"] == "Here you go."
        assert reader.calls == [{"draft_id": "d1"}]


async def test_pending_approvals_carry_previewable_flag(db):
    app, gw = _make_app(db, previews=_previews())
    await gw.approval_store.create(None, "drive_trash_file", {"file_id": "f"}, "DESTRUCTIVE")
    await gw.approval_store.create(None, "gmail_send", {"to": "x"}, "OUTWARD")
    async with _client(app) as c:
        rows = (await c.get("/api/approvals")).json()
        flags = {r["tool"]: r["previewable"] for r in rows}
        # The flag is what tells the UI to fetch /preview — the JS must not
        # hardcode tool names (resolver-backed vs self-describing lives in
        # gateway/preview.py, the single source of truth).
        assert flags == {"drive_trash_file": True, "gmail_send": False}


async def test_approval_preview_guards(db):
    handles = {
        "gmail_get_draft": StubDraftReader(
            "Error from google_workspace.gmail_get_draft: boom"
        ),
        "gmail_read": StubDraftReader(
            "Error from google_workspace.gmail_read: boom"
        ),
    }
    app, gw = _make_app(db, previews=_previews(handles))
    # Non-resolver tool → 404 (self-describing args, nothing to preview).
    other = await gw.approval_store.create(None, "gmail_send", {"to": "x"}, "OUTWARD")
    # Resolved → 404 (both previewable tools).
    done = await gw.approval_store.create(None, "gmail_send_draft", {"draft_id": "d"}, "OUTWARD")
    await gw.approval_store.resolve(done, "approved")
    done_trash = await gw.approval_store.create(
        None, "gmail_trash_message", {"message_id": "m"}, "DESTRUCTIVE"
    )
    await gw.approval_store.resolve(done_trash, "denied")
    # Pending but the MCP call fails (plain-text error) → 502, fail visible.
    broken = await gw.approval_store.create(None, "gmail_send_draft", {"draft_id": "d"}, "OUTWARD")
    broken_trash = await gw.approval_store.create(
        None, "gmail_trash_message", {"message_id": "m"}, "DESTRUCTIVE"
    )
    async with _client(app) as c:
        assert (await c.get(f"/api/approvals/{other}/preview")).status_code == 404
        assert (await c.get(f"/api/approvals/{done}/preview")).status_code == 404
        assert (await c.get(f"/api/approvals/{done_trash}/preview")).status_code == 404
        assert (await c.get("/api/approvals/nope/preview")).status_code == 404
        assert (await c.get(f"/api/approvals/{broken}/preview")).status_code == 502
        assert (await c.get(f"/api/approvals/{broken_trash}/preview")).status_code == 502


async def test_approval_preview_503_without_reader(db):
    # A wired registry whose needed handle is missing (no Gmail MCP) → 503.
    app, gw = _make_app(db, previews=_previews())
    aid = await gw.approval_store.create(None, "gmail_send_draft", {"draft_id": "d"}, "OUTWARD")
    async with _client(app) as c:
        assert (await c.get(f"/api/approvals/{aid}/preview")).status_code == 503


async def test_approval_preview_404_without_registry(db):
    # No previews wired at all (gw.previews=None) → every preview 404s and
    # /api/approvals marks nothing previewable; modals degrade to raw args.
    app, gw = _make_app(db)
    aid = await gw.approval_store.create(None, "gmail_send_draft", {"draft_id": "d"}, "OUTWARD")
    async with _client(app) as c:
        assert (await c.get(f"/api/approvals/{aid}/preview")).status_code == 404
        rows = (await c.get("/api/approvals")).json()
        assert rows[0]["previewable"] is False


async def test_trash_preview_returns_message_summary(db):
    """The trash preview returns a compact from/subject/date/snippet dict —
    never the full body (gmail_read can return up to 50k chars)."""
    reader = StubDraftReader(json.dumps({
        "id": "m1", "from": "Alice <alice@example.com>", "to": "me@example.com",
        "subject": "Hello", "date": "Sun, 13 Jul 2026 10:00:00 +0100",
        "body": "x" * 1000, "attachments": [],
    }))
    app, gw = _make_app(db, previews=_previews({"gmail_read": reader}))
    aid = await gw.approval_store.create(
        None, "gmail_trash_message", {"message_id": "m1"}, "DESTRUCTIVE"
    )
    async with _client(app) as c:
        r = await c.get(f"/api/approvals/{aid}/preview")
        assert r.status_code == 200
        body = r.json()
        assert body["from"] == "Alice <alice@example.com>"
        assert body["subject"] == "Hello"
        assert body["date"] == "Sun, 13 Jul 2026 10:00:00 +0100"
        assert body["snippet"] == "x" * 300 + "…"
        assert "body" not in body
        assert reader.calls == [{"message_id": "m1"}]


async def test_trash_preview_503_without_message_reader(db):
    """The draft handle must not leak into the trash path — a configured
    gmail_get_draft alone still 503s a trash preview."""
    draft_reader = StubDraftReader(json.dumps({"body": "should never be used"}))
    app, gw = _make_app(db, previews=_previews({"gmail_get_draft": draft_reader}))
    aid = await gw.approval_store.create(
        None, "gmail_trash_message", {"message_id": "m1"}, "DESTRUCTIVE"
    )
    async with _client(app) as c:
        assert (await c.get(f"/api/approvals/{aid}/preview")).status_code == 503
    assert draft_reader.calls == []


# ---- /api/transcribe (dictation) -------------------------------------------

class FakeTranscriber:
    """Duck-types WhisperTranscriber.stream(); endpoint tests never import
    faster_whisper. transcribe() is deliberately absent — it pins that the
    route consumes the streaming method, not the joined-text wrapper."""

    def __init__(
        self,
        segments: tuple[str, ...] = ("hello", "world"),
        error: str | None = None,
        fail_after: int | None = None,  # None = success; 0 = before 1st segment
    ):
        self._segments = segments
        self._fail_after = fail_after
        self.last_error = error

    async def stream(self, data: bytes):
        for i, seg in enumerate(self._segments):
            if self._fail_after is not None and i >= self._fail_after:
                break
            yield seg
        if self._fail_after is not None:
            raise RuntimeError(self.last_error or "boom")


def _sse_events(text: str) -> list[dict]:
    return [
        json.loads(block[len("data: "):])
        for block in text.split("\n\n")
        if block.startswith("data: ")
    ]


async def test_transcribe_status_enabled(db):
    """The web UI shows the mic only when this says enabled — one env flag
    (HARNESS_STT) controls both the endpoint and the button."""
    app, _ = _make_app(db, stt=FakeTranscriber())
    async with _client(app) as c:
        r = await c.get("/api/transcribe")
        assert r.status_code == 200
        assert r.json() == {"enabled": True}


async def test_transcribe_status_disabled(db):
    app, _ = _make_app(db, stt=None)
    async with _client(app) as c:
        r = await c.get("/api/transcribe")
        assert r.status_code == 200
        assert r.json() == {"enabled": False}


async def test_transcribe_streams_segments(db):
    app, _ = _make_app(db, stt=FakeTranscriber())
    async with _client(app) as c:
        r = await c.post(
            "/api/transcribe",
            content=b"\x00" * 100,
            headers={"Content-Type": "audio/webm"},
        )
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.text.startswith(": connected")  # comment lines ride alongside data
        assert _sse_events(r.text) == [
            {"type": "segment", "text": "hello"},
            {"type": "segment", "text": "world"},
            {"type": "done"},
        ]


async def test_transcribe_error_event(db):
    app, _ = _make_app(db, stt=FakeTranscriber(error="decode failed", fail_after=0))
    async with _client(app) as c:
        r = await c.post("/api/transcribe", content=b"not audio")
        assert r.status_code == 200  # failure arrives in-stream, not as HTTP
        events = _sse_events(r.text)
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "decode failed" in events[0]["detail"]


async def test_transcribe_error_event_midstream(db):
    app, _ = _make_app(db, stt=FakeTranscriber(error="decoder died", fail_after=1))
    async with _client(app) as c:
        r = await c.post("/api/transcribe", content=b"\x00" * 100)
        events = _sse_events(r.text)
        assert events[0] == {"type": "segment", "text": "hello"}
        assert events[1]["type"] == "error"
        assert "decoder died" in events[1]["detail"]


async def test_transcribe_503_when_disabled(db):
    app, _ = _make_app(db)  # stt defaults to None (HARNESS_STT=off)
    async with _client(app) as c:
        r = await c.post("/api/transcribe", content=b"\x00" * 10)
        assert r.status_code == 503


async def test_transcribe_413_oversize(db):
    from harness.gateway.routes.stt import MAX_AUDIO_BYTES

    app, _ = _make_app(db, stt=FakeTranscriber())
    async with _client(app) as c:
        r = await c.post("/api/transcribe", content=b"x" * (MAX_AUDIO_BYTES + 1))
        assert r.status_code == 413


async def test_transcribe_400_empty_body(db):
    app, _ = _make_app(db, stt=FakeTranscriber())
    async with _client(app) as c:
        r = await c.post("/api/transcribe", content=b"")
        assert r.status_code == 400




# ---- document-attachment picker (plan doc-attachments Step 3) -------------

async def test_index_serves_the_document_capable_picker(db):
    """GET / must ship the widened accept= and version-stamped assets as
    SERVED bytes, not as files on disk: the mount (NoCacheStatic html=True)
    is what the PWA fetches, and a lost ?v= tag on either the stylesheet or
    the entry module means WebKit revalidates into last week's code — the
    convention bump exists precisely because stale caches were invisible.
    The extension LIST's cross-file sync is pinned in test_conventions;
    this asserts the delivered page carries the document-capable picker."""
    app, _ = _make_app(db)
    async with _client(app) as c:
        r = await c.get("/")
        assert r.status_code == 200
        page = r.text
    assert 'accept="image/*,.pdf,.docx,.xlsx,.txt,.md"' in page, (
        "served index.html cannot pick documents"
    )
    assert re.search(r'href="/chat\.css\?v=\d+"', page), "chat.css lost its ?v= tag"
    assert re.search(r'src="/app\.js\?v=\d+"', page), "app.js entry tag lost its ?v="
