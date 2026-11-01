"""Dictation route: /api/transcribe. Raw audio bytes in (webm/opus from
Chrome, mp4/AAC from iPad Safari), finite SSE out — whisper segments as
they decode, then done/error. Kept out of streaming.py so the chat turn and
dictation can be read (and their heartbeats tuned) independently."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..state import Gateway
from ._sse import sse

# /api/transcribe body cap — a 60 s AAC/opus dictation clip is well under 1 MB.
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def build_router(gw: Gateway) -> APIRouter:
    router = APIRouter()

    @router.get("/api/transcribe")
    async def transcribe_status():
        """Whether dictation is on (HARNESS_STT). The web UI shows the mic
        button only when this says enabled, so one env flag hides both."""
        return {"enabled": gw.stt is not None}

    @router.post("/api/transcribe")
    async def transcribe(request: Request):
        """-> finite SSE: {"type":"segment","text":...} per whisper segment,
        then {"type":"done"} — or a terminal {"type":"error","detail":...}.
        The body is the blob itself, not multipart — PyAV sniffs the
        container, so Content-Type is informational only. Pre-checks stay
        plain HTTP statuses so the client's !resp.ok branch handles them."""
        if gw.stt is None:
            raise HTTPException(503, "speech-to-text is disabled")
        data = await request.body()
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "audio exceeds 10 MB")
        if not data:
            raise HTTPException(400, "empty audio body")

        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for text in gw.stt.stream(data):
                    queue.put_nowait({"type": "segment", "text": text})
                queue.put_nowait({"type": "done"})
            except Exception as e:  # surfaced to the client, not swallowed
                queue.put_nowait({
                    "type": "error",
                    "detail": f"could not transcribe audio ({gw.stt.last_error or e})",
                })
            finally:
                queue.put_nowait(None)

        task = asyncio.create_task(pump())

        async def stream():
            # An initial comment opens the stream and defeats proxy buffering.
            yield ": connected\n\n"
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        # Heartbeat: a cold model load (minutes on the first-ever
                        # download) idles far longer than tailscale serve
                        # tolerates. (Chat got its own heartbeat later — see
                        # chat_run.CHAT_HEARTBEAT_S; the intervals stay separate
                        # on purpose, whisper decode idles longer than a chat.)
                        yield ": ping\n\n"
                        continue
                    if event is None:
                        break
                    yield sse(event)
            finally:
                # Client disconnected mid-decode: stop consuming so the
                # transcriber lock frees for the next dictation.
                if not task.done():
                    task.cancel()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            # Same anti-buffering pair as /api/events (tailscale serve / iPad).
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
