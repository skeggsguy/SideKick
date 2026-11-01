"""Request guards and response hardening for the web gateway.

The daemon binds localhost and is exposed only over the tailnet via
`tailscale serve`; browser sessions carry no auth, so state-changing requests
are guarded here (same-origin Origin for browsers, HARNESS_API_TOKEN bearer
for non-browser clients).
"""

from __future__ import annotations

import os
import secrets as _secrets
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.staticfiles import StaticFiles

# Content-Security-Policy for the app document. `script-src 'self'` neutralises
# any stored/reflected HTML that slips into the DOM (defence-in-depth behind the
# textContent escaping) — achievable because every script is same-origin
# (vendored marked/DOMPurify, no CDN) and there are no inline <script>s except a
# nonce'd theme bootstrap. Set on the static responses (the documents whose
# scripts it governs) rather than via BaseHTTPMiddleware, which would buffer and
# break the SSE streams.
CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    # img-src 'self' only: the UI renders same-origin icons and no external or
    # data: images. This blocks a prompt-injected markdown image from GETting an
    # attacker URL on render (the render-time exfil channel); renderMd also
    # FORBID_TAGS the <img> at the sanitizer as defence-in-depth.
    "img-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'"
)


def require_same_origin(request: Request) -> None:
    """CSRF/auth guard for state-changing routes. Browser requests must carry
    a same-origin Origin header (fetch always sends one on non-GET). Requests
    WITHOUT an Origin are fine for GET/HEAD (OAuth top-level navigations, SSE)
    but on unsafe methods they are non-browser clients — those must present
    `Authorization: Bearer $HARNESS_API_TOKEN`, otherwise any local/tailnet
    process could e.g. release a parked OUTWARD approval unseen. Fails closed
    when no token is configured. (A curl smoke passes by sending
    `Origin: http://127.0.0.1:8787`.)"""
    origin = request.headers.get("origin")
    if origin is not None:
        if urlparse(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "cross-origin request rejected")
        return
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = os.environ.get("HARNESS_API_TOKEN", "")
    auth = request.headers.get("authorization", "")
    supplied = auth[7:].strip() if auth.startswith("Bearer ") else ""
    if token and supplied and _secrets.compare_digest(supplied, token):
        return
    raise HTTPException(
        403, "state-changing request without Origin requires a bearer token"
    )


class NoCacheStatic(StaticFiles):
    """Serve the web UI with `Cache-Control: no-cache` so the browser revalidates
    (cheap, via ETag) on every load instead of holding a stale app.js. Without
    this an installed PWA can keep running an old build for a long time — iOS
    caches aggressively and a close/reopen doesn't force a re-fetch."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Content-Security-Policy"] = CSP
        return response
