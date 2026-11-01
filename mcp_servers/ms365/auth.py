"""MSAL device-code auth for the Microsoft 365 MCP server.

Personal Microsoft accounts, read-only Graph
scopes. One-time setup is an Azure app registration + `uv run ms365-mcp
--auth` — see docs/ms365-setup.md. The token cache lives outside the repo
(and .gitignore catches *token*.json regardless); MSAL refreshes silently
from it thereafter.
"""

from __future__ import annotations

import os
from pathlib import Path

import msal

# Delegated Graph scopes — read-only in this phase, mirroring the Google
# server's read-first stance. Adding write scopes later forces one re-auth.
SCOPES = ["Mail.Read", "Calendars.Read"]

# "consumers" = personal Microsoft accounts only, not work/school tenants.
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/consumers"

_DATA_DIR = Path.home() / ".local" / "share" / "harness"

AUTH_HELP = (
    "Microsoft account is not authenticated. Run `uv run ms365-mcp --auth` "
    "in a terminal and follow the device-code instructions (see "
    "docs/ms365-setup.md for the one-time Azure app registration)."
)


def client_id() -> str:
    cid = os.environ.get("MS365_CLIENT_ID")
    if not cid:
        raise RuntimeError(
            "MS365_CLIENT_ID is not set. Register an Azure app and add its "
            "Application (client) ID to .env — see docs/ms365-setup.md."
        )
    return cid


def token_cache_path() -> Path:
    return Path(
        os.environ.get("MS365_TOKEN_FILE", _DATA_DIR / "ms365_token_cache.json")
    ).expanduser()


def _app(cache: msal.SerializableTokenCache) -> msal.PublicClientApplication:
    # Constructing the app does LIVE authority discovery — never in offline tests;
    # ms365 auth paths are manual-verify only.
    return msal.PublicClientApplication(
        client_id(),
        authority=os.environ.get("MS365_AUTHORITY", DEFAULT_AUTHORITY),
        token_cache=cache,
    )


def _load_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    path = token_cache_path()
    if path.exists():
        cache.deserialize(path.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    if cache.has_state_changed:
        path = token_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cache.serialize(), encoding="utf-8")


def get_access_token() -> str:
    """Silent token from the cache (refreshing when needed). Raises with
    clear instructions when the user has never authenticated."""
    cache = _load_cache()
    app = _app(cache)
    accounts = app.get_accounts()
    result = (
        app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    )
    _save_cache(cache)
    if not result or "access_token" not in result:
        raise RuntimeError(AUTH_HELP)
    return result["access_token"]


def run_auth_flow() -> None:
    """Interactive one-time device-code flow; caches the token for the server."""
    cache = _load_cache()
    app = _app(cache)
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SystemExit(
            f"Could not start the device-code flow: {flow.get('error_description', flow)}\n"
            "Check that the Azure app allows public client flows "
            "(docs/ms365-setup.md)."
        )
    print(flow["message"])  # "go to https://microsoft.com/devicelogin, enter CODE"
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(
            f"Authentication failed: {result.get('error_description', result)}"
        )
    _save_cache(cache)
    print(f"Authenticated. Token cached at {token_cache_path()}")
