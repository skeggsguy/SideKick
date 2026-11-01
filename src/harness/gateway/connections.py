"""Vendor connection registry for the in-app OAuth ("tap to Connect") flow.

Auth is per-vendor (there is no universal cross-vendor OAuth worth adopting —
the aggregators that pretend otherwise hold your tokens in their cloud, against
this harness's local-first token model). What we DO share is the in-app flow:
the daemon serves one /api/oauth/{vendor}/start + /callback pair (gateway/app.py)
and one Connections panel, and each vendor plugs in via a Vendor record here.

Google is wired now; Microsoft 365 is a disabled stub so the same UI/routes
serve it the day the connector is unpaused (decisions.md 2026-06-12) — no
redesign needed.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable

from mcp_servers.google_workspace import auth as gauth


def _ago(epoch: float | None) -> str | None:
    """A coarse 'how long ago' string. Deliberately not an exact timestamp —
    transparency without handing out fine-grained token telemetry."""
    if not epoch:
        return None
    secs = max(0, int(time.time() - epoch))
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 90:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 36:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


@dataclass(frozen=True)
class Vendor:
    """A connectable third-party account.

    build_auth_url(redirect_uri, state) -> (consent URL, secrets). `secrets` is
        an opaque per-attempt dict (e.g. the PKCE code_verifier) the caller
        stashes against `state` and hands back to exchange().
    exchange(redirect_uri, code, secrets) -> swap the returned code for a token
        + cache it (blocking network I/O — callers run it in a worker thread).
    status() -> {"connected": bool, "scopes": [...], "missing": [...]}.
    """

    key: str
    label: str
    enabled: bool
    build_auth_url: Callable[[str, str], tuple[str, dict]]
    exchange: Callable[[str, str, dict], None]
    status: Callable[[], dict]


def _google_status() -> dict:
    path = gauth.token_path()
    required = list(gauth.SCOPES)
    granted: list[str] = []
    auto_refreshing = False
    last_refresh: str | None = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            granted = data.get("scopes") or []
            auto_refreshing = bool(data.get("refresh_token"))
        except (OSError, json.JSONDecodeError):
            granted = []
        try:
            # mtime is rewritten on every re-auth AND silent refresh, so this is
            # "last refreshed", not "connected at". No absolute path/timestamp
            # is exposed — only a coarse relative string.
            last_refresh = _ago(path.stat().st_mtime)
        except OSError:
            last_refresh = None
    missing = [s for s in required if s not in set(granted)]
    return {
        "connected": bool(granted) and not missing,
        "scopes": granted,
        "missing": missing,
        "required": required,
        "auto_refreshing": auto_refreshing,
        "last_refresh": last_refresh,
    }


def _google_exchange(redirect_uri: str, code: str, secrets: dict) -> None:
    gauth.web_exchange(redirect_uri, code, secrets.get("code_verifier"))


def _google() -> Vendor:
    return Vendor(
        key="google",
        label="Google",
        enabled=True,
        build_auth_url=gauth.web_auth_url,
        exchange=_google_exchange,
        status=_google_status,
    )


def _ms365_stub() -> Vendor:
    # Paused (decisions.md 2026-06-12). Present in the registry so it shows in
    # the Connections panel as "not connected"; starting auth raises until the
    # connector is brought online.
    def _paused(*_a, **_k):
        raise RuntimeError("Microsoft 365 connector is paused — not yet enabled.")

    return Vendor(
        key="ms365",
        label="Microsoft 365",
        enabled=False,
        build_auth_url=lambda redirect_uri, state: _paused(),
        exchange=lambda redirect_uri, code, secrets: _paused(),
        status=lambda: {
            "connected": False, "scopes": [], "missing": [], "required": [],
            "auto_refreshing": False, "last_refresh": None,
        },
    )


def all_vendors() -> dict[str, Vendor]:
    """The connectable vendors, keyed by `key`. Built fresh per call so a
    re-auth's new token state is reflected without caching."""
    return {v.key: v for v in (_google(), _ms365_stub())}
