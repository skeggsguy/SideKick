"""OAuth credentials for the Google Workspace MCP server.

Broad per-service scopes, granted in a single re-auth. We deliberately moved
off least-privilege incremental scopes: the harness's security boundary is the
policy engine (servers.toml ActionClass + core/policy.py), not the OAuth scope
— gmail_send stays OUTWARD and always prompts no matter how broad the token is.
Broad scopes only widen blast radius if the on-disk token leaks, and in return
new Google tools never need a scope change or another re-auth. See
.claude/decisions.md (2026-06-19, broad-scope reversal).

The OAuth client secret comes from Google Cloud Console (a *Web application*
client now, so the in-app Connections flow can redirect to the daemon's tailnet
URL — see docs/google-oauth-setup.md); the user token is cached locally and
refreshed automatically. Both files live outside the repo by default, and the
repo's .gitignore catches *token*.json regardless.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    # Broad read/write per service. Each is a superset of the narrow scopes it
    # replaces (gmail.modify ⊇ readonly+compose, calendar ⊇ readonly+events,
    # drive ⊇ readonly+file). Gmail tops out at gmail.modify, NOT
    # https://mail.google.com/ — that adds *permanent delete*, a DESTRUCTIVE
    # capability we don't expose; granting it would be the only thing ever
    # needing a further re-auth.
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]

_DATA_DIR = Path.home() / ".local" / "share" / "harness"

AUTH_HELP = (
    "Google account is not authenticated. Run "
    "`uv run google-workspace-mcp --auth` in a terminal to complete the "
    "OAuth flow (see docs/google-oauth-setup.md for one-time Cloud Console "
    "setup)."
)


def client_secret_path() -> Path:
    return Path(
        os.environ.get(
            "GOOGLE_OAUTH_CLIENT_FILE", _DATA_DIR / "google_client_secret.json"
        )
    ).expanduser()


def token_path() -> Path:
    return Path(
        os.environ.get("GOOGLE_TOKEN_FILE", _DATA_DIR / "google_token.json")
    ).expanduser()


def _write_token(path: Path, creds: Credentials) -> None:
    """Write the cached token and lock it down to owner-only (0600) —
    it grants mail/calendar/docs write access, never leave it world-readable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(path, 0o600)


def load_credentials() -> Credentials:
    """Load the cached token, refreshing if expired. Raises with clear
    instructions when the user has never authenticated — or when the cached
    token predates the write scopes (re-auth needed before any write call)."""
    path = token_path()
    if not path.exists():
        raise RuntimeError(AUTH_HELP)
    # Validate against the scopes ACTUALLY granted in the token file. We can't
    # check creds.scopes after construction: from_authorized_user_file(path,
    # SCOPES) overwrites creds.scopes with the passed SCOPES, so a superset
    # check on the constructed object is a tautology (SCOPES >= SCOPES) — a
    # token predating a new scope would sail through to refresh() and fail with
    # a cryptic 'invalid_scope' instead of these instructions.
    granted = set(json.loads(path.read_text(encoding="utf-8")).get("scopes") or [])
    if not granted >= set(SCOPES):
        raise RuntimeError(
            "Cached Google token is missing newly-required scopes "
            f"(write access). {AUTH_HELP}"
        )
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _write_token(path, creds)
        return creds
    raise RuntimeError(f"Cached Google token is unusable. {AUTH_HELP}")


_MISSING_SECRET = (
    "OAuth client secret not found at {secret}.\n"
    "Create a *Web application* OAuth client in Google Cloud Console (add the "
    "daemon's tailnet callback as an authorized redirect URI), save it there, "
    "or point GOOGLE_OAUTH_CLIENT_FILE at it. See docs/google-oauth-setup.md."
)


def _load_flow():
    secret = client_secret_path()
    if not secret.exists():
        raise SystemExit(_MISSING_SECRET.format(secret=secret))
    return InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)


def _web_flow(redirect_uri: str) -> Flow:
    """A web-application OAuth flow with an explicit redirect_uri — backs the
    in-app Connections flow, which redirects to the daemon's tailnet URL rather
    than a loopback address the device can't reach."""
    secret = client_secret_path()
    if not secret.exists():
        raise RuntimeError(_MISSING_SECRET.format(secret=secret))
    return Flow.from_client_secrets_file(
        str(secret), scopes=SCOPES, redirect_uri=redirect_uri
    )


def web_auth_url(redirect_uri: str, state: str) -> tuple[str, dict]:
    """Build the Google consent URL for the in-app flow. `state` is an opaque
    anti-CSRF token the caller stores and re-checks on the callback. Returns
    (url, secrets): `secrets` must be stashed and handed back to web_exchange —
    it carries the PKCE code_verifier, which the token exchange needs to match
    the code_challenge sent here (a fresh Flow at callback time would otherwise
    fail with 'invalid_grant: Missing code verifier')."""
    flow = _web_flow(redirect_uri)
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true",
        state=state,
    )
    return url, {"code_verifier": flow.code_verifier}


def web_exchange(redirect_uri: str, code: str, code_verifier: str | None = None) -> None:
    """Exchange the authorization code for a token and cache it. `code_verifier`
    is the PKCE secret returned by web_auth_url. Blocking (network I/O) — call
    from a worker thread in async contexts."""
    # Google merges previously-granted scopes into the response (incremental
    # auth), so the returned scope set is a SUPERSET of what we asked for. That
    # is fine for us (load_credentials only needs granted ⊇ SCOPES), but
    # oauthlib raises on any scope mismatch unless we relax the check.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = _web_flow(redirect_uri)
    flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    _write_token(token_path(), flow.credentials)


def run_auth_flow() -> None:
    """Interactive one-time OAuth flow; caches the token for the server."""
    flow = _load_flow()
    # Headless/remote-friendly: pin the loopback port with GOOGLE_OAUTH_PORT so
    # it can be SSH-forwarded, and never try to launch a browser on the (often
    # display-less) host — just print the URL to open elsewhere.
    port = int(os.environ.get("GOOGLE_OAUTH_PORT", "0"))
    kwargs: dict = {"port": port, "open_browser": False}
    if port:
        # The redirect_uri stays http://localhost:<port> (Google only allows
        # loopback), but bind on all interfaces so the catcher is reachable
        # over an SSH tunnel even when sshd lives on the Windows host and
        # WSL2's localhost relay isn't in the path. Ephemeral, ~30s, personal
        # box — acceptable exposure for the one-shot auth.
        kwargs["bind_addr"] = "0.0.0.0"
    creds = flow.run_local_server(**kwargs)
    path = token_path()
    _write_token(path, creds)
    print(f"Authenticated. Token cached at {path}")


def run_auth_flow_manual() -> None:
    """Copy/paste OAuth flow for headless or mobile-SSH use — no local server,
    no port forwarding. Open the printed URL anywhere, approve, then paste back
    the (failed-to-load) localhost URL the browser lands on; the code rides in
    its query string."""
    # The pasted redirect is http://localhost (loopback) — tell oauthlib that's
    # fine, exactly as run_local_server does internally. RELAX_TOKEN_SCOPE: see
    # web_exchange — Google returns a superset of the requested scopes.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = _load_flow()
    # The OAuth client is a Web-application type registering BOTH this URI and the
    # tailnet /api/oauth callback; the in-app flow needs HARNESS_PUBLIC_URL set.
    flow.redirect_uri = "http://localhost:8765/"
    auth_url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    print("\n1. Open this URL in any browser and approve access:\n")
    print(auth_url)
    print(
        "\n2. Your browser will then try to load a 'localhost:8765' page and "
        "fail to connect — THAT IS EXPECTED.\n"
        "3. Copy the FULL address from the browser's address bar (it starts "
        "with http://localhost:8765/?code=...) and paste it below.\n"
    )
    response = input("Paste the localhost URL here: ").strip()
    flow.fetch_token(authorization_response=response)
    path = token_path()
    _write_token(path, flow.credentials)
    print(f"\nAuthenticated. Token cached at {path}")


#: (api, version) -> (token mtime at build time, service client). Keyed on the
#: token's mtime so a re-auth (which rewrites the token) self-heals: the next
#: call rebuilds with fresh credentials. Without this, a long-lived MCP server
#: keeps using the pre-re-auth credentials until it restarts.
_services: dict[tuple[str, str], tuple[float, object]] = {}


def _token_mtime() -> float:
    try:
        return token_path().stat().st_mtime
    except OSError:
        return 0.0


def get_service(api: str, version: str):
    """Build (and cache) a Google API service client, rebuilding when the
    cached token file has changed on disk since the client was built."""
    key = (api, version)
    mtime = _token_mtime()
    cached = _services.get(key)
    if cached is None or cached[0] != mtime:
        service = build(
            api, version, credentials=load_credentials(), cache_discovery=False
        )
        _services[key] = (mtime, service)
    return _services[key][1]
