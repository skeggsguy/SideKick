"""The web gateway: create_app composes the route modules over one Gateway.

Mounted inside agentd. The daemon binds localhost and is exposed only over
the tailnet via `tailscale serve` (PLAN-v2 §6); it must never bind a public
interface. Route logic lives in routes/ (streaming, control, oauth,
resources, media); the guards and CSP in security.py; the Gateway dataclass
in state.py (re-exported here for composition roots and tests).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse

from .routes import control, media, oauth, resources, streaming
from .security import NoCacheStatic, require_same_origin
from .state import Gateway

__all__ = ["Gateway", "create_app", "WEBUI_DIR"]

WEBUI_DIR = Path(__file__).parent / "webui"


def create_app(gw: Gateway) -> FastAPI:
    # The same-origin guard runs on every route in every router (it no-ops for
    # the OAuth GET navigations and the SSE GETs, which carry no Origin).
    # Static files are a separate mount and unaffected.
    app = FastAPI(
        title="harness",
        docs_url=None,
        redoc_url=None,
        dependencies=[Depends(require_same_origin)],
    )
    for build in (
        streaming.build_router,
        control.build_router,
        oauth.build_router,
        resources.build_router,
        media.build_router,
    ):
        app.include_router(build(gw))

    # ---- static UI (mounted last so /api and /media keep precedence) ------

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        # Served from the root scope so the worker can control the whole app.
        return FileResponse(WEBUI_DIR / "sw.js", media_type="text/javascript")

    app.mount("/", NoCacheStatic(directory=WEBUI_DIR, html=True), name="webui")

    return app
