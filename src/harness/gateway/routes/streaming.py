"""Streaming routes: the chat SSE endpoint, Stop, and the live-events feed.

The chat turn itself (request model, hooks, detach-on-disconnect plumbing)
lives in chat_run.py; dictation in stt.py. This file is the thin route
layer over both, plus /api/events — the shared background-run feed.
"""

from __future__ import annotations

import asyncio
import re
import sys

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..broadcast import PageStreams, device_class
from ..live import live_projection
from ..media import resolve
from ..state import Gateway
from ._sse import sse
from .chat_run import ChatRequest, chat_sse_response, start_chat_run
from .stt import build_router as build_stt_router


_PAGE_RE = re.compile(r"[A-Za-z0-9-]{1,40}")
_N_RE = re.compile(r"[0-9]{1,9}")


def _page_param(page: str | None) -> str | None:
    """A short safe token or absent — never 422 a malformed/old client."""
    return page if page is not None and _PAGE_RE.fullmatch(page) else None


def _n_param(n: str | None) -> int | None:
    return int(n) if n is not None and _N_RE.fullmatch(n) else None


def build_router(gw: Gateway) -> APIRouter:
    router = APIRouter()
    router.include_router(build_stt_router(gw))
    page_streams = PageStreams()  # duplicate-subscription diagnostic

    @router.post("/api/chat")
    async def chat(req: ChatRequest):
        # Resolve media ids to the stored attachment shape BEFORE the run
        # starts: an unknown id is a clean 404 with no session created and no
        # agent built. path stays server-internal (the messages route strips
        # it; the browser fetches bytes from GET /media/{id}). name (the
        # sidecar original filename, "" for images) and size ride along —
        # the wire hint and the doc chips read them, so they are captured
        # once here and persisted with the message (content-addressed files
        # never change, so the hint re-serializes byte-identic every call).
        attachments = None
        if req.attachment_ids:
            attachments = []
            for att_id in req.attachment_ids:
                resolved = resolve(att_id)
                if resolved is None:
                    raise HTTPException(404, f"unknown attachment {att_id!r}")
                try:
                    size = resolved["path"].stat().st_size
                except OSError:  # vanished between resolve() and stat()
                    raise HTTPException(404, f"unknown attachment {att_id!r}")
                attachments.append(
                    {
                        "id": att_id,
                        "mime": resolved["mime"],
                        "path": str(resolved["path"]),
                        "name": resolved["name"],
                        "size": size,
                    }
                )
        run = await start_chat_run(
            gw, req.text, req.session_id, req.effort, req.model,
            attachments=attachments,
        )
        return chat_sse_response(run)

    @router.post("/api/chat/{session_id}/stop", status_code=202)
    async def stop_chat(session_id: str):
        """User Stop for the live turn on this session.

        Mid-model-stream → cancel the task now: the cancel just closes the
        provider request, which FREES the shared local model slot (canceling
        is what gets the user their machine back). Tool executing →
        request_stop() defers (a cancel there would strand the side effect
        without its history or audit row) and the loop raises CancelledError
        itself once the step is persisted, so we don't cancel here. After
        turn_complete the answer is on screen and only the naming/extraction
        tail remains — 409 rather than pretending a Stop undoes it.
        """
        task = gw.active_chats.get(session_id)
        chat_run = gw.chat_runs.get(session_id)
        if task is None or chat_run is None or task.done():
            raise HTTPException(404, "no live run on this chat")
        if chat_run.answer_complete:
            raise HTTPException(409, "already finishing")
        if chat_run.agent.request_stop():
            task.cancel()
        return {"stopped": True}

    @router.post("/api/chat/{session_id}/detach", status_code=202)
    async def detach_chat(session_id: str):
        """Client noticed its chat stream died — reroute the live turn's
        events to the broadcaster now, without waiting for the server to
        notice the socket.

        Without this the zombie-queue failure mode hangs recovery: when the
        client's network vanishes silently (tailscale conn dies, no FIN/RST
        reaches us), writes to the loopback socket keep "succeeding", the
        heartbeat keeps the proxy from timing it out, and the run streams
        into a dead queue forever — /api/events never carries the turn, so
        the PWA sits on 'Reconnecting · still working' until the user
        background-and-resumes the app. Idempotent; a no-op 202 once the
        run is gone (its own teardown already detached)."""
        chat_run = gw.chat_runs.get(session_id)
        if chat_run is not None:
            chat_run.detach()
        return {"detached": True}

    @router.get("/api/chat/{session_id}/live")
    async def chat_live(session_id: str, since: int = 0):
        """Replay the live chat run's emitted events for a resyncing client.

        The contract (pinned in tests/test_chat_recovery.py — that file IS
        the client's contract):

        - while the run is live: {active: true, seq: M, oldest_seq: O,
          gap: G, events: [...]} — every event with seq > `since`, in order,
          each carrying the same seq/run_id the live sinks carried. `since`
          means "I have seen everything up to seq N"; since=0 replays the
          whole live window (the ring's whole window if the run is long
          enough to have overflowed) — but a client COLD-OPENING the chat
          wants GET /api/sessions/{sid}/messages instead, which joins the
          window to the persisted transcript. seq numbers
          restart at 1 for each new run on the session, so a watermark
          belongs to its run_id.
        - `oldest_seq` is the oldest event still replayable and `gap` is
          TRUE iff events the client asked for were dropped off the front
          (`since + 1 < oldest_seq`; at `since == oldest_seq - 1` nothing is
          missing, and a caught-up or beyond-watermark client is never
          flagged). That is the SUPPORTED drop detector; no seq arithmetic
          is the client's job.
          A gap'd head is gone from live data and NOT fillable by a mid-call
          store refetch (the store has no in-flight message —
          decisions 2026-10-17): paint the surviving suffix from oldest_seq and
          heal at run_done. Window: RING_CAPACITY events — sized (measured)
          at 2.1x the longest observed run, ~105 min of streaming.
        - once the run is gone: {active: false} and nothing else. The ring
          is in-memory and per-run — it dies with the run, is never written
          to SQLite, and survives no daemon restart. active: false OBLIGES
          the client to fall back to the persisted refetch; the run_done
          heal stays the post-run path.

        Session existence is NOT this endpoint's question (GET
        /api/sessions/{id} answers that, 200-null = deleted); a session with
        no live run is simply inactive, never a 404.

        Liveness is answered by the SHARED predicate (gateway/live.py), so
        this and /messages' `live` field can never disagree. A client with NO
        watermark gets the whole screen (persisted transcript + unpersisted
        tail) from the one-fetch GET /api/sessions/{sid}/messages — but a
        client that HOLDS a watermark must come HERE with it instead of
        re-fetching /messages: the full batch is MiB-scale (~2.4 MiB at the
        longest observed run), since=N is the cheap path. NOTE the
        two-sinces rule: `since` HERE is the CLIENT's own watermark ("I
        painted through seq N"); /messages' live.since is the SERVER's
        persisted watermark — different numbers with different obligations,
        and a client stores its own (live.seq after a tail replay), never
        the server's."""
        proj = live_projection(gw, session_id, since)
        if proj is None:
            return {"active": False}
        return {"active": True, **proj}

    @router.get("/api/events")
    async def events(
        request: Request, page: str | None = None, n: str | None = None
    ):
        """A long-lived SSE stream of background-run output. Interactive chat
        streams over its own POST response; triggered/scheduled/watch runs
        publish here so an open browser renders them live instead of waiting
        for the completion push.

        `page`/`n` (client page id + EventSource counter) feed a diagnostic
        only: one stderr line when a page holds >1 live stream. Malformed or
        missing values are simply untracked — never a 422."""
        if gw.broadcaster is None:
            raise HTTPException(503, "live events unavailable")
        queue = gw.broadcaster.subscribe()
        page_id, seq = _page_param(page), _n_param(n)
        ua = device_class(request.headers.get("user-agent"))

        async def stream():
            # Tracked inside the generator so open/close pair with its
            # lifecycle: a body that never starts can't leave a phantom
            # stream that flags every later open as a duplicate.
            handle, line = page_streams.open(
                page_id, seq, ua, subscribers=gw.broadcaster.subscriber_count
            )
            if line:
                print(line, file=sys.stderr)
            try:
                # An initial comment opens the stream and defeats proxy
                # buffering. Inside the try: a disconnect at this very first
                # yield must still unsubscribe + close.
                yield ": connected\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=25)
                        yield sse(event)
                    except asyncio.TimeoutError:
                        # Heartbeat: keeps the connection alive through
                        # tailscale serve / intermediaries that drop idle SSE.
                        yield ": ping\n\n"
            finally:
                gw.broadcaster.unsubscribe(queue)
                line = page_streams.close(handle)
                if line:
                    print(line, file=sys.stderr)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            # Stop any reverse proxy (tailscale serve) or browser from buffering
            # or caching the stream — without these an iPad over the tailnet can
            # see nothing until the connection closes.
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return router
