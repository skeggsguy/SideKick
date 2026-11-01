"""Read-mostly resource routes: health, sessions, runs, memory, push, and the
client-reported timezone."""

from __future__ import annotations

from dataclasses import asdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...store.client_config import REF_KEYS
from ..live import persisted_tail_projection
from ..state import Gateway


class RenameRequest(BaseModel):
    title: str


class FactCreate(BaseModel):
    content: str
    category: str


class FactPatch(BaseModel):
    """Partial update: a field left None is untouched."""

    content: str | None = None
    category: str | None = None


def build_router(gw: Gateway) -> APIRouter:
    router = APIRouter()

    @router.get("/api/health")
    async def health():
        # `sonnet` gates the web UI's Sonnet chat entry (the only non-local
        # model option; needs an API key on the host). `name` is the greeting's
        # HARNESS_USER_NAME, null when unset.
        return {"ok": True, "sonnet": gw.sonnet_available, "name": gw.user_name}

    # ---- sessions ---------------------------------------------------------

    @router.get("/api/sessions")
    async def list_sessions():
        # `active` = an interactive chat turn is still running for the session
        # (detached or not) — the client uses it to recover after a dropped
        # chat stream (registry entries are popped when the run finishes).
        # Membership can stay true through the done→_reap tick while the store is
        # already settled — resume logic branches on /live's `active`, never this.
        return [
            {**s, "active": s["id"] in gw.active_chats}
            for s in await gw.sessions.list()
        ]

    @router.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        # Single-session GET for PWA cold-start restore (pwa-accurate-resume
        # Step 2): the 50-row list above cannot prove an older id deleted.
        # Answers 200 + row-or-NULL, deliberately NOT 404: an agentd older
        # than this route answers such GETs with FastAPI's own 404, and the
        # client must distinguish "server says gone" (200 null → forget the
        # lastChat pointer) from "server cannot say" (non-200/throw → keep
        # it). Contract pinned in tests/test_webui_v2.py.
        s = await gw.sessions.get(session_id)
        if s is None:
            return None
        return {**s, "active": session_id in gw.active_chats}

    @router.get("/api/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        """The resuming client's FIRST fetch: one response = the whole
        screen (pwa-accurate-resume Step 4), which is what keeps the
        fetch-then-swap atomic render honest — a cold open of a mid-answer
        chat paints the live reply with no second request and no race.

            {messages: [...persisted...],
             live: {since, seq, oldest_seq, gap, events} | null,
             compactions: [{checkpoint_id, anchor_idx, created_at,
                            est_before, est_after}, ...]}

        Every message row also carries `idx`: its STORE idx. Rows render
        1:1 with positions only while the messages table has no holes (a
        swallowed append leaves a permanent gap under UNIQUE
        (session_id, idx)), so anything anchoring on a row must read `idx`,
        never array position. `compactions` is the session's compaction
        history oldest-first (run_events kind='compaction' joined to runs),
        each entry anchoring its transcript seam at a STORE idx — the same
        space `idx` names; the seam paints above the first row whose idx is
        >= anchor_idx. A snapshot race can anchor past the last row: the
        client parks those seams above the live tail.

        `live` comes from the shared liveness predicate (gateway/live.py)
        and is the run's ring replayed FROM THE SERVER'S PERSISTED
        WATERMARK: events at or below `since` are exactly what `messages`
        already contains, so paint messages, then replay `live.events` —
        the overlap-free join the client cannot compute itself (persisted
        rows carry no seq; a tool-loop run replayed from 0 would re-render
        completed steps). Liveness null obliges "messages is the whole
        truth" — the run is gone and its answer is in the store; live
        present obliges "paint, then replay the tail unpaced, tracking seq
        per run_id (your watermark becomes live.seq — never `since`, which
        is the server's number)". On `gap` the dropped events were
        UNPERSISTED only (the in-flight step outgrew the ring): paint the
        suffix from oldest_seq; it heals when that step persists — the
        persisted half is whole by construction. Contract pinned in
        tests/test_chat_recovery.py, the client's contract. Liveness AND the
        persisted watermark are sampled around the store read (see below),
        so `since` can never name a boundary ahead of the transcript
        returned beside it, and a run finishing mid-read can never answer
        null beside a stale transcript.

        Cost note for clients: the tail is the in-flight step — worst case
        one full model call (~2.4 MiB at the longest observed run, up to
        ~5 MiB at a full ring). A client that already holds a watermark
        must not re-fetch this payload for it: it sends the watermark to
        GET /api/chat/{sid}/live?since=N and returns here only when /live
        says active:false. No pagination or delta-compression — the
        watermark path is the avoidance mechanism (YAGNI, and the ring dies
        with the run so this endpoint stays one fetch).
        """
        if not await gw.sessions.exists(session_id):
            raise HTTPException(404, "no such session")

        # An attachment's `path` is server-internal (a file-jail location):
        # the browser renders thumbnails from GET /media/{id} and doc chips
        # from name+size, so the wire shape is {id, mime, name, size}. The
        # .get defaults serve rows persisted before documents existed (and
        # images, which carry neither key). Every message goes through the
        # strip — assistant/tool rows carry [] anyway.
        async def transcript() -> list[dict]:
            return [
                {
                    **asdict(m),
                    # The STORE idx, in the same space compaction anchors
                    # address: array position lies once the table has a
                    # hole, and the reload seam maps anchors through this.
                    "idx": idx,
                    "attachments": [
                        {
                            "id": a["id"],
                            "mime": a["mime"],
                            "name": a.get("name", ""),
                            "size": a.get("size", 0),
                        }
                        for a in m.attachments
                    ],
                }
                for idx, m in await gw.sessions.load_messages_indexed(session_id)
            ]

        # The JOIN must hold STILL across the store read: `live.since` names
        # the seq boundary of the rows in `messages`, so a transcript write
        # that lands while the read is in flight (the watermark advances after
        # the read went to the DB) would leave the tail starting after a row
        # the client never saw — that step is skipped until run_done, the
        # head-loss shape in the completed-step direction. The same stability
        # check covers the run finishing mid-read (live -> null: the read may
        # predate the final persist). Bounded at three passes because a
        # transcript write happens once per model call, seconds apart: a
        # second pass happens only when a write really raced the read.
        def stable(before, after) -> bool:
            if (before is None) != (after is None):
                return False
            return before is None or before["since"] == after["since"]

        live = persisted_tail_projection(gw, session_id)
        for _ in range(3):
            messages = await transcript()
            after = persisted_tail_projection(gw, session_id)
            if stable(live, after):
                break
            live = after
        # Read AFTER the loop on purpose: a compaction row newer than the
        # transcript snapshot anchors at or past the last row, and the
        # client parks such seams above the live tail — while the join's
        # own frame dedupes against them (compaction.js, data-anchor).
        compactions = await gw.runs.session_compactions(session_id)
        return {
            "messages": messages, "live": after, "compactions": compactions,
            # BK-041 G4: a cron/watch run on this session — its start + phase.
            "bg_live": gw.bg_live.view(session_id),
        }

    @router.patch("/api/sessions/{session_id}")
    async def rename_session(session_id: str, req: RenameRequest):
        title = " ".join(req.title.split())[:60].strip()
        if not title:
            raise HTTPException(422, "title is empty")
        if not await gw.sessions.update_title(session_id, title):
            raise HTTPException(404, "no such session")
        return {"ok": True, "title": title}

    # ---- runs -------------------------------------------------------------

    @router.get("/api/runs")
    async def list_runs():
        return await gw.runs.list()

    @router.get("/api/runs/summary")
    async def runs_summary(days: int = 7):
        return await gw.runs.summary(days=days)

    @router.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str):
        return await gw.runs.events(run_id)

    # ---- memory -----------------------------------------------------------

    @router.get("/api/memory/facts")
    async def memory_facts(
        q: str | None = None,
        category: str | None = None,
        include_inactive: bool = False,
    ):
        if gw.memory is None:
            raise HTTPException(503, "memory is not available")
        if q:
            facts = await gw.memory.search(q)
        else:
            facts = await gw.memory.list_facts(
                category=category, include_inactive=include_inactive
            )
        return [asdict(f) for f in facts]

    @router.post("/api/memory/facts")
    async def memory_add(req: FactCreate):
        if gw.memory is None:
            raise HTTPException(503, "memory is not available")
        content = req.content.strip()
        if not content:
            raise HTTPException(422, "content is empty")
        try:
            # Hand-typed facts get confidence 1.0 so they outrank extractor
            # inferences (default 0.8) in the always_on ordering.
            fact_id, created = await gw.memory.add_fact(
                content, req.category, confidence=1.0
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"ok": True, "id": fact_id, "created": created}

    @router.patch("/api/memory/facts/{fact_id}")
    async def memory_edit(fact_id: int, req: FactPatch):
        if gw.memory is None:
            raise HTTPException(503, "memory is not available")
        content = req.content.strip() if req.content is not None else None
        if req.content is not None and not content:
            raise HTTPException(422, "content is empty")
        if content is None and req.category is None:
            raise HTTPException(422, "nothing to change")
        try:
            ok = await gw.memory.update_fact(
                fact_id, content=content, category=req.category
            )
        except ValueError as e:
            raise HTTPException(422, str(e))
        if not ok:
            raise HTTPException(404, "no active fact with that id")
        return {"ok": True}

    @router.post("/api/memory/facts/{fact_id}/forget")
    async def memory_forget(fact_id: int):
        if gw.memory is None:
            raise HTTPException(503, "memory is not available")
        if not await gw.memory.forget(fact_id):
            raise HTTPException(404, "no active fact with that id")
        return {"ok": True}

    # ---- push -------------------------------------------------------------

    @router.get("/api/push/key")
    async def push_key():
        return {"key": gw.push.public_key_b64url}

    @router.post("/api/push/subscribe")
    async def push_subscribe(subscription: dict):
        if "endpoint" not in subscription:
            raise HTTPException(422, "not a push subscription")
        await gw.push.subscribe(subscription)
        return {"ok": True}

    @router.post("/api/push/test")
    async def push_test():
        sent = await gw.push.notify_all(
            "Test notification", "Push is working end-to-end."
        )
        return {"sent": sent}

    # ---- client timezone --------------------------------------------------
    # The PWA reports its IANA zone (Intl) on load/resume; we persist the last
    # value so scheduled runs (morning briefing) render times in the zone the
    # user is actually in, even when no client is connected. The in-process
    # timezone tools read it back via ClientConfigStore.

    @router.post("/api/client/timezone")
    async def client_timezone(body: dict):
        tz = (body.get("tz") or "").strip()
        if not tz:
            raise HTTPException(422, "missing tz")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise HTTPException(422, f"unknown timezone {tz!r}")
        if gw.client_config is not None:
            await gw.client_config.set_timezone(tz)
        return {"ok": True, "tz": tz}

    # ---- contact details (identity refs) ----------------------------------
    # The {email}/{phone} values a delegated browser subagent may fill into web
    # forms. Stored server-side and resolved only at dispatch — they never enter
    # the model's context (tools/_refs.py). This authed single-user UI both sets
    # and reads them back (edit UX); the DELETE-by-empty-string clears a key.

    @router.post("/api/client/refs")
    async def set_client_refs(body: dict):
        if gw.client_config is None:
            raise HTTPException(503, "client config unavailable")
        for field, key in REF_KEYS.items():
            if field in body:
                await gw.client_config.set(key, str(body[field] or "").strip())
        return {"ok": True}

    @router.get("/api/client/refs")
    async def get_client_refs():
        if gw.client_config is None:
            raise HTTPException(503, "client config unavailable")
        out = {}
        for field, key in REF_KEYS.items():
            out[field] = await gw.client_config.get(key) or ""
        return out

    return router
