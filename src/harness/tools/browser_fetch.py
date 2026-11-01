"""browser_fetch (Lane A): render one URL in the persistent agent Chrome over raw CDP.

The escalation from web_fetch for Cloudflare-gated or JS-rendered pages.
Deterministic: navigate -> settle (networkAlmostIdle) -> poll the DOM until
stable -> read outerHTML, then run the SAME extraction pipeline as web_fetch
(_extract.render_page). Talks raw CDP over a WebSocket to a real,
auto-updating Chrome run as a service (chrome-agent behind chrome-proxy) —
no Playwright, no stealth libraries, nothing version-pinned that rots.
NEVER calls Runtime.enable (a Cloudflare bot-detection signal); challenge
detection enables the Network domain instead to capture the main-frame
Document status + headers for the shared detect_challenge().

Egress is layered: (1) the host-side check_target() SSRF pre-check refuses
private/tailnet URLs before any CDP call; (2) Chrome itself is locked to the
chrome-proxy egress filter (harness.egress_proxy as a service), which
resolves + validates + IP-pins every request the page makes. Bot-protection
challenges surface (blocked, tab left open for a manual click) — never
bypassed.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from ..core.policy import ActionClass
from ._cdp import CDPClient, CDPError
from ._extract import detect_challenge, render_page
from ._netguard import (
    InternalRefusalNote,
    Refused,
    RefusedInternal,
    Resolver,
    check_target,
    default_resolver,
)
from .base import Tool

# Read at IMPORT time: a CDP port/env change needs an agentd restart the same sitting
# (a port move once silently broke browser_fetch for 4 days).
_CDP_PORT = int(os.environ.get("HARNESS_CDP_PORT", "19222"))
_DEFAULT_CHARS = 15_000
_MAX_CHARS = 40_000
_HOST_TIMEOUT_S = 45
_SETTLE_S = 8.0
_STABLE_S = 15.0
# Module-level so offline tests can monkeypatch them to ~0 (no real sleeps).
_POLL_INTERVAL_S = 0.5
_RETRY_SLEEP_S = 6.0

_CHROME_MISSING_MSG = (
    f"browser_fetch error: the agent Chrome is not reachable on 127.0.0.1:{_CDP_PORT}. "
    "It runs as two LaunchAgents — com.harness.egress-proxy (the egress filter, "
    "127.0.0.1:18080) then com.harness.chrome-agent; check "
    "`launchctl print gui/501/com.harness.chrome-agent` and "
    "docs/browser-fallback-setup.md."
)


class ChromeUnavailable(Exception):
    """The agent Chrome's CDP endpoint could not be reached (health check)."""


async def _browser_ws_url(cdp_port: int = _CDP_PORT) -> str:
    """GET /json/version and return webSocketDebuggerUrl. Any failure is the
    health-check signal ChromeUnavailable. trust_env=False: an env proxy must
    not sit between us and the local debug port."""
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            resp = await client.get(f"http://127.0.0.1:{cdp_port}/json/version")
            resp.raise_for_status()
            url = resp.json().get("webSocketDebuggerUrl")
    except (httpx.HTTPError, ValueError) as exc:
        raise ChromeUnavailable(str(exc)) from exc
    if not url:
        raise ChromeUnavailable("no webSocketDebuggerUrl in /json/version")
    return url


def _lifecycle_match(sid: str, name: str) -> Callable[[dict], bool]:
    """Predicate for the named Page.lifecycleEvent on this session. Register it
    with cdp.expect_event() BEFORE Page.navigate: a fast (cached) page can fire
    the event between the navigate ack and a later-registered waiter, and a
    missed event stalls the fetch for the full settle timeout."""

    def match(msg: dict) -> bool:
        return (
            msg.get("method") == "Page.lifecycleEvent"
            and msg.get("sessionId") == sid
            and (msg.get("params") or {}).get("name") == name
        )

    return match


async def _get_html(cdp: CDPClient, sid: str) -> str:
    doc = await cdp.send("DOM.getDocument", {"depth": 0}, sid)
    # A missing root is a hard protocol break (KeyError propagates and fails
    # the fetch); a transient CDPError mid-render is swallowed by the poller.
    node_id = doc["root"]["nodeId"]
    res = await cdp.send("DOM.getOuterHTML", {"nodeId": node_id}, sid)
    return res.get("outerHTML", "")


async def _wait_stable_dom(
    cdp: CDPClient, sid: str, *, stable_reads: int = 2, timeout: float = _STABLE_S
) -> str:
    """Poll outerHTML until its length is stable across `stable_reads` reads or
    the deadline. This is what catches SPAs: `load` fires long before React/Vue
    has painted anything, so lifecycle alone isn't enough. Transient CDPErrors
    during render are swallowed (keep polling)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = -1
    stable = 0
    html = ""
    while loop.time() < deadline:
        try:
            html = await _get_html(cdp, sid)
        except CDPError:
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue
        if len(html) == last:
            stable += 1
            if stable >= stable_reads:
                return html
        else:
            stable = 0
            last = len(html)
        await asyncio.sleep(_POLL_INTERVAL_S)
    return html


def _document_tracker(
    sid: str, frame_id: str
) -> tuple[Callable[[dict], None], Callable[[], tuple[int, dict]]]:
    """Record the LATEST main-frame Document response (status + lowercased
    headers). MAIN frame only (frame_id from Page.getFrameTree): same-process
    iframes — ads, consent frames, embedded challenge widgets — emit their own
    type=Document responses on this session AFTER the main frame's, and a
    404/challenged iframe must not overwrite the page's real status. Latest,
    not first: a cleared challenge reloads the main frame (403 -> fresh 200
    Document), and the retry re-detect must see it. Defaults to 200/{} when
    no Document response arrives (data: URLs, soft navigation).
    """
    state: dict[str, Any] = {"status": 200, "headers": {}}

    def listener(msg: dict) -> None:
        if msg.get("method") != "Network.responseReceived" or msg.get("sessionId") != sid:
            return
        params = msg.get("params") or {}
        if params.get("type") != "Document" or params.get("frameId") != frame_id:
            return
        resp = params.get("response") or {}
        state["status"] = int(resp.get("status") or 0)
        # CDP headers are a plain dict with Chrome's casing; detect_challenge
        # looks up "cf-mitigated" by lowercase key.
        state["headers"] = {str(k).lower(): v for k, v in (resp.get("headers") or {}).items()}

    def current() -> tuple[int, dict]:
        return state["status"], state["headers"]

    return listener, current


@dataclass
class FetchResult:
    ok: bool
    html: str
    url: str
    status: int
    blocked: bool
    reason: str | None
    ms: int


async def fetch_page(
    cdp: CDPClient,
    url: str,
    *,
    settle_s: float = _SETTLE_S,
    stable_s: float = _STABLE_S,
    retry_on_challenge: bool = True,
) -> FetchResult:
    """Render one URL in a fresh tab of the connected Chrome.

    Challenged pages get one patient retry (passive JS challenges clear in a
    few seconds); if still challenged the tab is LEFT OPEN for a manual solve
    and blocked=True is returned — no solver, no escalation. Any exception
    becomes ok=False with a best-effort tab close."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    target_id: str | None = None

    def ms() -> int:
        return int((loop.time() - started) * 1000)

    try:
        created = await cdp.send("Target.createTarget", {"url": "about:blank"})
        target_id = created["targetId"]
        attached = await cdp.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )
        sid = attached["sessionId"]

        await cdp.send("Page.enable", {}, sid)
        await cdp.send("DOM.enable", {}, sid)
        await cdp.send("Network.enable", {}, sid)
        await cdp.send("Page.setLifecycleEventsEnabled", {"enabled": True}, sid)
        # Runtime.enable is deliberately NEVER sent (CF detection signal).

        # The main frame's id keys the Document tracker (iframes must not
        # overwrite the page status); a missing key is a hard protocol break.
        tree = await cdp.send("Page.getFrameTree", {}, sid)
        frame_id = tree["frameTree"]["frame"]["id"]
        listener, doc_current = _document_tracker(sid, frame_id)
        cdp.add_listener(listener)

        # Register the settle waiter BEFORE navigating (see _lifecycle_match).
        idle = cdp.expect_event(_lifecycle_match(sid, "networkAlmostIdle"))
        await cdp.send("Page.navigate", {"url": url}, sid)
        # networkAlmostIdle fires well after `load`, once XHR traffic quiesces.
        await cdp.wait_event(idle, settle_s)
        html = await _wait_stable_dom(cdp, sid, timeout=stable_s)

        status, headers = doc_current()
        vendor = detect_challenge(status, headers, html)
        if vendor and retry_on_challenge:
            await asyncio.sleep(_RETRY_SLEEP_S)
            html = await _wait_stable_dom(cdp, sid, timeout=stable_s)
            status, headers = doc_current()
            vendor = detect_challenge(status, headers, html)

        info = await cdp.send("Target.getTargetInfo", {"targetId": target_id})
        final_url = (info.get("targetInfo") or {}).get("url") or url

        if vendor:
            # Interactive challenge: leave the tab open for a manual click.
            return FetchResult(False, html, final_url, status, True, vendor, ms())

        await cdp.send("Target.closeTarget", {"targetId": target_id})
        return FetchResult(True, html, final_url, status, False, None, ms())
    except Exception as err:  # structured failure, never raise into the turn
        if target_id is not None:
            try:
                await cdp.send("Target.closeTarget", {"targetId": target_id})
            except CDPError:
                pass  # best-effort cleanup; the original error is what matters
        return FetchResult(False, "", url, 0, False, f"{type(err).__name__}: {err}", ms())


class BrowserFetchTool(Tool):
    name = "browser_fetch"
    description = (
        "Render a JavaScript-heavy or bot-gated web page in a real, persistent "
        "browser and return its text (HTML reduced to readable text plus any "
        "embedded structured data). Use ONLY when web_fetch reports the page "
        "appears JS-rendered, or when web_fetch returns no content for a site "
        "you know loads dynamically or is bot-gated. Slower and heavier than "
        "web_fetch — try web_fetch first. Public web only; private and internal "
        "addresses are refused. Interactive bot-protection challenges are "
        "reported with the tab left open for a manual solve, not bypassed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The http(s) URL to render."},
            "max_chars": {
                "type": "integer",
                "description": f"Truncate returned text to this many characters "
                f"(default {_DEFAULT_CHARS}, max {_MAX_CHARS}).",
            },
            "extract": {
                "type": "string",
                "enum": ["auto", "text", "structured", "full"],
                "description": "auto (default): readable text plus embedded "
                "structured data. text: readable text only. structured: embedded "
                "structured data only. full: complete page text including "
                "listing/card content readability may drop.",
            },
        },
        "required": ["url"],
    }
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(
        self,
        *,
        cdp_port: int = _CDP_PORT,
        resolver: Resolver = default_resolver,
        ws_url_getter: Callable[[int], Awaitable[str]] = _browser_ws_url,
    ) -> None:
        self._cdp_port = cdp_port
        self._resolve = resolver
        self._ws_url_getter = ws_url_getter

    async def run(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        max_chars = min(max(int(args.get("max_chars", _DEFAULT_CHARS)), 100), _MAX_CHARS)
        mode = str(args.get("extract", "auto")).strip().lower()
        if mode not in ("auto", "text", "structured", "full"):
            mode = "auto"

        # 1. Host-side SSRF pre-check (defense in depth; the chrome-proxy
        # egress filter is the load-bearing layer for the page's own requests).
        try:
            await check_target(url, self._resolve)
        except RefusedInternal as e:
            # Internal target = attack-in-progress; the typed note taints
            # the turn's egress (agent._dispatch detects it by isinstance).
            return InternalRefusalNote(f"browser_fetch error: {e}")
        except Refused as e:
            return f"browser_fetch error: {e}"

        # 2. Health check + connect.
        try:
            ws_url = await self._ws_url_getter(self._cdp_port)
        except ChromeUnavailable:
            return _CHROME_MISSING_MSG
        try:
            cdp = await CDPClient.connect(ws_url)
        except Exception:
            # Chrome vanished between the health check and the WS connect —
            # same remedy as unreachable.
            return _CHROME_MISSING_MSG

        # 3. Render under a hard host timeout; always close the connection.
        try:
            result = await asyncio.wait_for(fetch_page(cdp, url), _HOST_TIMEOUT_S)
        except asyncio.TimeoutError:
            return f"browser_fetch error: rendering timed out after {_HOST_TIMEOUT_S}s."
        finally:
            await cdp.close()

        return await self._render(result, max_chars, mode)

    async def _render(self, result: FetchResult, max_chars: int, mode: str) -> str:
        if result.blocked:
            host = urlsplit(result.url).hostname or result.url
            return (
                f"browser_fetch: {host} is showing an interactive bot-protection "
                f"challenge ({result.reason}). The browser tab has been LEFT OPEN "
                "for a manual solve. browser_fetch will NOT retry automatically. "
                "Consider using the notify tool to ask the user to solve the "
                "challenge in the agent Chrome window, then retry this URL once "
                "they confirm."
            )
        if not result.ok:
            return f"browser_fetch error: {result.reason}"
        # Mirror web_fetch's non-200 contract (a SPA paints its shell over a
        # 404). status 0 = no Document response recorded — not an error.
        if result.status and result.status != 200:
            return f"browser_fetch error: {result.url} returned HTTP {result.status}."
        # Same pipeline as web_fetch; suppress the JS-rendered note — we ARE
        # the JS renderer.
        return await render_page(
            result.html,
            result.url,
            max_chars,
            mode,
            allow_js_note=False,
            tool_name="browser_fetch",
        )
