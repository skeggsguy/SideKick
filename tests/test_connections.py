"""In-app vendor OAuth: the /api/oauth flow + the get_service token-cache fix.

All offline — the Google flow primitives are monkeypatched; no real account,
no network.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from mcp_servers.google_workspace import auth as gauth
from harness.gateway import connections
from harness.gateway.app import Gateway, create_app


def _gateway(public_url):
    # Only the OAuth routes are exercised; the other deps are never touched by
    # those handlers, so Nones are fine here.
    return Gateway(
        sessions=None,
        runs=None,
        approval_store=None,
        waiter=None,
        triggers=None,
        bus=None,
        push=None,
        agent_factory=None,
        public_url=public_url,
    )


@pytest.fixture
async def oauth_client(monkeypatch, tmp_path):
    token = tmp_path / "google_token.json"
    monkeypatch.setattr(gauth, "token_path", lambda: token)
    calls: dict[str, list] = {"exchange": []}

    monkeypatch.setattr(
        gauth, "web_auth_url",
        lambda redirect_uri, state: (
            f"https://consent.test/o?state={state}", {"code_verifier": "v-" + state}
        ),
    )

    def fake_exchange(redirect_uri, code, code_verifier=None):
        token.write_text(json.dumps({"scopes": list(gauth.SCOPES)}), encoding="utf-8")
        calls["exchange"].append((redirect_uri, code, code_verifier))

    monkeypatch.setattr(gauth, "web_exchange", fake_exchange)

    app = create_app(_gateway("https://host.ts.net"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.calls = calls  # type: ignore[attr-defined]
        c.token = token  # type: ignore[attr-defined]
        yield c


async def test_start_redirects_to_consent(oauth_client):
    resp = await oauth_client.get("/api/oauth/google/start", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://consent.test/o?state=")


async def test_callback_exchanges_code_and_redirects(oauth_client):
    start = await oauth_client.get("/api/oauth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    cb = await oauth_client.get(
        f"/api/oauth/google/callback?state={state}&code=abc123",
        follow_redirects=False,
    )
    assert cb.status_code == 303
    assert cb.headers["location"] == "/#connections?connected=google"
    # exchange ran with the daemon's own callback as redirect_uri and the PKCE
    # verifier carried over from the start step (the "Missing code verifier" bug).
    assert oauth_client.calls["exchange"] == [
        ("https://host.ts.net/api/oauth/google/callback", "abc123", "v-" + state)
    ]


async def test_callback_rejects_unknown_state(oauth_client):
    cb = await oauth_client.get(
        "/api/oauth/google/callback?state=forged&code=x", follow_redirects=False
    )
    assert cb.status_code == 400


async def test_callback_state_is_single_use(oauth_client):
    start = await oauth_client.get("/api/oauth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    first = await oauth_client.get(
        f"/api/oauth/google/callback?state={state}&code=a", follow_redirects=False
    )
    assert first.status_code == 303
    replay = await oauth_client.get(
        f"/api/oauth/google/callback?state={state}&code=a", follow_redirects=False
    )
    assert replay.status_code == 400  # state was consumed


async def test_status_reflects_token_scopes(oauth_client):
    before = {v["key"]: v for v in (await oauth_client.get("/api/oauth/status")).json()}
    assert before["google"]["connected"] is False
    assert before["google"]["enabled"] is True

    start = await oauth_client.get("/api/oauth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    await oauth_client.get(
        f"/api/oauth/google/callback?state={state}&code=c", follow_redirects=False
    )

    after = {v["key"]: v for v in (await oauth_client.get("/api/oauth/status")).json()}
    assert after["google"]["connected"] is True
    assert after["google"]["missing"] == []


async def test_disabled_vendor_cannot_start(oauth_client):
    resp = await oauth_client.get("/api/oauth/ms365/start", follow_redirects=False)
    assert resp.status_code == 400


async def test_unknown_vendor_404(oauth_client):
    resp = await oauth_client.get("/api/oauth/nope/start", follow_redirects=False)
    assert resp.status_code == 404


async def test_start_without_public_url_is_503(monkeypatch, tmp_path):
    monkeypatch.setattr(gauth, "token_path", lambda: tmp_path / "tok.json")
    app = create_app(_gateway(None))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/api/oauth/google/start", follow_redirects=False)
        assert resp.status_code == 503


def test_google_status_missing_scopes(monkeypatch, tmp_path):
    token = tmp_path / "tok.json"
    token.write_text(json.dumps({"scopes": list(gauth.SCOPES)[:1]}), encoding="utf-8")
    monkeypatch.setattr(gauth, "token_path", lambda: token)
    st = connections._google_status()
    assert st["connected"] is False
    assert set(st["missing"]) == set(gauth.SCOPES) - set(gauth.SCOPES[:1])


def test_scope_info_covers_all_scopes():
    """The Connections page's client-side SCOPE_INFO map must describe every
    granted scope, or the UI silently understates access. The JS map can't
    import the Python SCOPES list, so pin it: regex the keys out of settings.js
    (the Connections panel module) and assert they cover gauth.SCOPES (drift in
    either direction fails)."""
    import re
    from pathlib import Path

    app_js = (
        Path(__file__).resolve().parents[1] / "src/harness/gateway/webui/settings.js"
    ).read_text(encoding="utf-8")
    prefix = "https://www.googleapis.com/auth/"
    mapped = set(re.findall(r'\[G \+ "([^"]+)"\]', app_js))
    required = {s[len(prefix):] for s in gauth.SCOPES if s.startswith(prefix)}
    missing = required - mapped
    assert not missing, f"SCOPE_INFO missing entries for: {sorted(missing)}"


def test_get_service_rebuilds_when_token_changes(monkeypatch):
    builds: list[int] = []
    monkeypatch.setattr(gauth, "load_credentials", lambda: object())
    monkeypatch.setattr(gauth, "build", lambda *a, **k: builds.append(1) or object())
    # mtime sequence: same token twice (cache hit), then a re-auth bumps it.
    times = iter([100.0, 100.0, 200.0])
    monkeypatch.setattr(gauth, "_token_mtime", lambda: next(times))
    gauth._services.clear()

    gauth.get_service("gmail", "v1")  # build #1 (mtime 100)
    gauth.get_service("gmail", "v1")  # cached (mtime 100)
    gauth.get_service("gmail", "v1")  # rebuild #2 (mtime 200 — re-auth)
    gauth._services.clear()

    assert len(builds) == 2
