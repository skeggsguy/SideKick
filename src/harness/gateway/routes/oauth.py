"""Vendor connections (in-app OAuth).

One flow shared across vendors (Google now, MS365 later). `start` redirects
the browser to the provider consent screen; the provider redirects back to
`callback` on this same daemon (reachable over the tailnet), which swaps the
code for a token — no SSH, no copy/paste. The token write self-heals the
running MCP server via get_service's mtime check, so no restart is needed.
`state` is validated (single-use) to prevent CSRF.
"""

from __future__ import annotations

import asyncio
import secrets as _secrets
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from ..connections import all_vendors
from ..state import Gateway


def build_router(gw: Gateway) -> APIRouter:
    router = APIRouter()
    vendors = all_vendors()
    oauth_pending: dict[str, dict] = {}

    @router.get("/api/oauth/status")
    async def oauth_status():
        return [
            {"key": v.key, "label": v.label, "enabled": v.enabled, **v.status()}
            for v in vendors.values()
        ]

    @router.get("/api/oauth/{vendor}/start")
    async def oauth_start(vendor: str):
        v = vendors.get(vendor)
        if v is None:
            raise HTTPException(404, "unknown vendor")
        if not v.enabled:
            raise HTTPException(400, f"{v.label} is not enabled")
        if not gw.public_url:
            raise HTTPException(
                503,
                "HARNESS_PUBLIC_URL is not set — in-app auth needs the daemon's "
                "external (tailnet) URL to receive the OAuth redirect.",
            )
        redirect_uri = f"{gw.public_url.rstrip('/')}/api/oauth/{vendor}/callback"
        state = _secrets.token_urlsafe(24)
        try:
            url, secrets = v.build_auth_url(redirect_uri, state)
        except Exception as e:  # bad/missing client secret etc. — don't leak state
            raise HTTPException(500, f"could not start {v.label} auth: {e}")
        # `secrets` carries the PKCE code_verifier the callback must replay.
        oauth_pending[state] = {
            "vendor": vendor, "redirect_uri": redirect_uri, "secrets": secrets,
        }
        return RedirectResponse(url, status_code=307)

    @router.get("/api/oauth/{vendor}/callback")
    async def oauth_callback(
        vendor: str, state: str = "", code: str = "", error: str = ""
    ):
        if error:
            return RedirectResponse(f"/#connections?error={quote(error)}", status_code=303)
        pending = oauth_pending.pop(state, None)
        if pending is None or pending["vendor"] != vendor:
            raise HTTPException(400, "invalid or expired auth state")
        v = vendors.get(vendor)
        if v is None:
            raise HTTPException(404, "unknown vendor")
        try:
            # fetch_token does blocking network I/O — keep the event loop free.
            await asyncio.to_thread(
                v.exchange, pending["redirect_uri"], code, pending["secrets"]
            )
        except Exception as e:
            return RedirectResponse(f"/#connections?error={quote(str(e))}", status_code=303)
        return RedirectResponse(f"/#connections?connected={vendor}", status_code=303)

    return router
