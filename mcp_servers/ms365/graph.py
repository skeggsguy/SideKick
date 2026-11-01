"""Thin Microsoft Graph REST helper (sync httpx; FastMCP runs tools in a
worker thread). All calls are GETs — this server is read-only."""

from __future__ import annotations

import httpx

from .auth import get_access_token

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TIMEOUT = 30.0


def get(path: str, params: dict | None = None, headers: dict | None = None) -> dict:
    resp = httpx.get(
        f"{GRAPH_BASE}{path}",
        params=params,
        headers={"Authorization": f"Bearer {get_access_token()}", **(headers or {})},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
