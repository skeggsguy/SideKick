"""Media routes: /api/media upload + /media/{id} file serving.

The web UI posts the raw blob (like /api/transcribe — not multipart), gets
back {id, mime, url} for images plus `name` for documents, and later
attaches ids to a chat message. GET /media/{id} exists because CSP is
`img-src 'self'`: thumbnails must be same-origin subresources, never data:
URIs. Documents (plan doc-attachments Step 1) ride the same id space and
get a Content-Disposition: attachment; the image response needs no CSP
header of its own — the browser applies index.html's policy to the load.

The save logic is gateway/media.py (testable apart from HTTP);
this file is status codes only. POST is CSRF-covered by app.py's global
require_same_origin dependency; GET carries no Origin, so the guard no-ops.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .. import media
from ..state import Gateway

# /api/media body caps — a phone photo straight from the camera is 2-5 MB;
# beyond 10 MB it's not a photo we want to decode. Documents ride 20 MB,
# the cap read_document already enforces on its input (a doc bigger than
# that could never be read). Same shape as stt.MAX_AUDIO_BYTES; monkeypatch
# these module attributes to test the caps.
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_DOC_BYTES = 20 * 1024 * 1024


def build_router(gw: Gateway) -> APIRouter:
    # gw is unused on purpose: the media store is env-driven at call time
    # (core.paths), not a Gateway member. The signature matches the other
    # builders so app.py can include this router in its loop.
    router = APIRouter()

    @router.post("/api/media")
    async def upload_media(request: Request):
        """Raw bytes in, attachment dict out — images {id, mime, url}
        (unchanged), documents {id, mime, url, name}.

        Branch by magic bytes, not by the claim: an image sniff rides the
        10 MB cap and the Pillow path; everything else gets the document
        path (≤ MAX_DOC_BYTES), where ?name= is part of the sniff — the
        claimed extension disambiguates the docx/xlsx zip pair. Pre-checks
        are plain HTTP statuses: 413 over cap, 400 empty, 415 unknown type.

        `path` stays server-side (it lands in Message.attachments, and the
        messages route must never put it on the wire).
        """
        data = await request.body()
        name = request.query_params.get("name", "")
        if media.sniff_image(data) is not None:
            if len(data) > MAX_MEDIA_BYTES:
                raise HTTPException(413, "image exceeds 10 MB")
            # Pillow decode/re-encode is CPU work: off the event loop, which
            # is also carrying live chat SSE.
            saved = await asyncio.to_thread(media.save_image, data)
            if saved is None:
                raise HTTPException(
                    415, "unsupported image (send JPEG, PNG, WebP or GIF)"
                )
            return {"id": saved["id"], "mime": saved["mime"], "url": saved["url"]}
        if len(data) > MAX_DOC_BYTES:
            raise HTTPException(413, "document exceeds 20 MB")
        if not data:
            raise HTTPException(400, "empty body")
        saved = await asyncio.to_thread(media.save_document, data, name)
        if saved is None:
            raise HTTPException(
                415,
                "unsupported upload (send a JPEG/PNG/WebP/GIF image or a "
                "PDF/DOCX/XLSX/TXT/MD document)",
            )
        return {
            "id": saved["id"],
            "mime": saved["mime"],
            "url": saved["url"],
            "name": saved["name"],
        }

    @router.get("/media/{id}")
    async def get_media(id: str):
        # Content-addressed files: immutable forever, cache hard. A
        # malformed id (any traversal shape) fails media.py's regex and
        # reads as a plain 404 — no signal about why.
        info = media.resolve(id)
        if info is None:
            raise HTTPException(404, "no such media")
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        if not info["mime"].startswith("image/"):
            # Documents are downloads, never in-page renders (the web UI
            # shows display-only chips; an accidental navigation to a raw
            # doc must offer a save, not render). The sidecar name is
            # charset-sanitized ([A-Za-z0-9._-]) — safe to quote verbatim.
            filename = info["name"] or id
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return FileResponse(
            info["path"],
            media_type=info["mime"],
            headers=headers,
        )

    return router
