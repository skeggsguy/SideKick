"""browser_fetch (Lane A) — offline against a FakeChrome speaking CDP over a
real websockets server on 127.0.0.1:0.

The FakeChrome scripts command responses and emits Page.lifecycleEvent /
Network.responseReceived events, so the whole navigate -> settle -> stable-DOM
-> challenge-detect pipeline runs for real (including the CDPClient reader
task) with no browser and no network. Poll/retry sleeps are monkeypatched to
~0 by an autouse fixture.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import websockets

import harness.tools.browser_fetch as bf
from harness.core.policy import ActionClass
from harness.tools._cdp import CDPClient, CDPError
from harness.tools.browser_fetch import (
    _CHROME_MISSING_MSG,
    BrowserFetchTool,
    ChromeUnavailable,
    fetch_page,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "html"

CLEAN_HTML = (
    "<html><head><title>Hi</title></head>"
    "<body><p>hello world from the fake browser</p></body></html>"
)


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch):
    monkeypatch.setattr(bf, "_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(bf, "_RETRY_SLEEP_S", 0)


async def _public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


class FakeChrome:
    """Scripts CDP responses + events for one page fetch."""

    def __init__(
        self,
        *,
        html_sequence: list[str],
        status: int = 200,
        headers: dict | None = None,
        fire_lifecycle: bool = True,
        emit_network: bool = True,
        getdocument_malformed: bool = False,
        clear_after: int | None = None,
        cleared_html: str | None = None,
        iframe_doc: dict | None = None,
    ) -> None:
        self.html_sequence = list(html_sequence)
        self.status = status
        self.headers = headers or {}
        self.fire_lifecycle = fire_lifecycle
        self.emit_network = emit_network
        self.getdocument_malformed = getdocument_malformed
        self.clear_after = clear_after
        self.cleared_html = cleared_html
        self.iframe_doc = iframe_doc
        self.commands: list[str] = []
        self._idx = 0
        self._outerhtml_calls = 0
        self._cleared = False

    async def handler(self, ws) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            self.commands.append(msg["method"])
            await self._dispatch(ws, msg)

    async def _dispatch(self, ws, msg: dict) -> None:
        mid, method = msg["id"], msg["method"]
        result: dict = {}
        if method == "Target.createTarget":
            result = {"targetId": "T1"}
        elif method == "Target.attachToTarget":
            result = {"sessionId": "S1"}
        elif method == "Page.getFrameTree":
            result = {"frameTree": {"frame": {"id": "F1"}}}
        elif method == "Page.navigate":
            await ws.send(json.dumps({"id": mid, "result": {}}))
            await self._emit_after_navigate(ws)
            return
        elif method == "DOM.getDocument":
            result = {} if self.getdocument_malformed else {"root": {"nodeId": 1}}
        elif method == "DOM.getOuterHTML":
            self._outerhtml_calls += 1
            if (
                self.clear_after is not None
                and not self._cleared
                and self._outerhtml_calls > self.clear_after
            ):
                self._cleared = True
                # The interstitial passed: Chrome reloads the main frame with
                # a fresh 200 Document response.
                await ws.send(
                    json.dumps(
                        {
                            "method": "Network.responseReceived",
                            "sessionId": "S1",
                            "params": {
                                "type": "Document",
                                "frameId": "F1",
                                "response": {"status": 200, "headers": {}},
                            },
                        }
                    )
                )
            html = self.cleared_html if self._cleared else self._next_html()
            result = {"outerHTML": html}
        elif method == "Target.getTargetInfo":
            result = {"targetInfo": {"url": "https://example.com/final"}}
        # Page/DOM/Network enables, lifecycle toggle, closeTarget -> {}
        await ws.send(json.dumps({"id": mid, "result": result}))

    def _next_html(self) -> str:
        if self._idx < len(self.html_sequence):
            html = self.html_sequence[self._idx]
            self._idx += 1
            return html
        return self.html_sequence[-1]

    async def _emit_after_navigate(self, ws) -> None:
        if self.emit_network:
            await ws.send(
                json.dumps(
                    {
                        "method": "Network.responseReceived",
                        "sessionId": "S1",
                        "params": {
                            "type": "Document",
                            "frameId": "F1",
                            "response": {
                                "status": self.status,
                                "headers": self.headers,
                            },
                        },
                    }
                )
            )
        if self.iframe_doc is not None:
            # A same-process iframe's Document response rides the same session
            # with its own frameId — it must never override the main frame.
            await ws.send(
                json.dumps(
                    {
                        "method": "Network.responseReceived",
                        "sessionId": "S1",
                        "params": {
                            "type": "Document",
                            "frameId": "F2",
                            "response": self.iframe_doc,
                        },
                    }
                )
            )
        if self.fire_lifecycle:
            # No sleep needed: the client registers its settle waiter BEFORE
            # sending Page.navigate (pinned below), so this can't be missed.
            await ws.send(
                json.dumps(
                    {
                        "method": "Page.lifecycleEvent",
                        "sessionId": "S1",
                        "params": {"name": "networkAlmostIdle"},
                    }
                )
            )


async def _serve(fake: FakeChrome):
    server = await websockets.serve(fake.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/devtools/browser/fake"


def _tool(ws_url: str) -> BrowserFetchTool:
    async def getter(cdp_port: int) -> str:
        return ws_url

    return BrowserFetchTool(resolver=_public_resolver, ws_url_getter=getter)


async def _run_tool(fake: FakeChrome, args: dict) -> str:
    server, ws_url = await _serve(fake)
    try:
        return await _tool(ws_url).run(args)
    finally:
        server.close()
        await server.wait_closed()


async def _run_fetch_page(fake: FakeChrome, **kwargs):
    server, ws_url = await _serve(fake)
    cdp = await CDPClient.connect(ws_url)
    try:
        return await fetch_page(cdp, "https://example.com/x", **kwargs)
    finally:
        await cdp.close()
        server.close()
        await server.wait_closed()


# --- 1. happy path -------------------------------------------------------------


async def test_happy_path_renders_content_and_closes_tab():
    fake = FakeChrome(html_sequence=[CLEAN_HTML])
    out = await _run_tool(fake, {"url": "https://example.com/x"})
    assert "hello world from the fake browser" in out
    assert "untrusted external data" in out
    assert "Target.closeTarget" in fake.commands
    # The banned domain is never touched.
    assert not any(cmd.startswith("Runtime.") for cmd in fake.commands), (
        "never call Runtime.* over CDP (Runtime.enable is a Cloudflare bot signal) — read the DOM via "
        "getDocument/getOuterHTML"
    )


# --- 2. lifecycle never fires -> DOM-poll fallback, no hang ---------------------


async def test_no_lifecycle_event_falls_back_to_dom_polling():
    fake = FakeChrome(html_sequence=[CLEAN_HTML], fire_lifecycle=False)
    result = await asyncio.wait_for(
        _run_fetch_page(fake, settle_s=0.2, stable_s=1.0), 10
    )
    assert result.ok is True
    assert "hello world" in result.html


# --- 3. growing-then-stable DOM -------------------------------------------------


async def test_growing_dom_polled_until_stable():
    fake = FakeChrome(html_sequence=["<p>a</p>", "<p>aa</p>", "<p>aaa</p>"])
    result = await _run_fetch_page(fake, stable_s=5.0)
    assert result.ok is True
    assert result.html == "<p>aaa</p>"  # the final, largest read
    assert fake._outerhtml_calls >= 4  # 3 growth reads + stability confirms


# --- 4. persistent challenge -> blocked, tab left open --------------------------


async def test_persistent_challenge_blocks_and_leaves_tab_open():
    body = (_FIXTURES / "cloudflare_challenge.html").read_text()
    fake = FakeChrome(
        html_sequence=[body],
        status=403,
        headers={"cf-mitigated": "challenge"},
    )
    out = await _run_tool(fake, {"url": "https://example.com/x"})
    assert "bot-protection challenge (Cloudflare)" in out
    assert "LEFT OPEN" in out
    assert "notify" in out
    assert "NOT retry automatically" in out
    assert "Target.closeTarget" not in fake.commands


# --- 5. challenge clears on the retry -------------------------------------------


async def test_challenge_clears_on_retry():
    body = (_FIXTURES / "cloudflare_challenge.html").read_text()
    fake = FakeChrome(
        html_sequence=[body],
        status=403,
        headers={"cf-mitigated": "challenge"},
        clear_after=3,  # first stable-DOM pass sees the challenge; retry clears
        cleared_html=CLEAN_HTML,
    )
    out = await _run_tool(fake, {"url": "https://example.com/x"})
    assert "hello world from the fake browser" in out
    assert "bot-protection" not in out
    assert "Target.closeTarget" in fake.commands


# --- 6. hard protocol break -> ok=False, best-effort close ----------------------


async def test_malformed_getdocument_fails_fetch_and_closes_tab():
    fake = FakeChrome(html_sequence=["<p>x</p>"], getdocument_malformed=True)
    result = await _run_fetch_page(fake, settle_s=0.1, stable_s=0.5)
    assert result.ok is False
    assert result.blocked is False
    assert "KeyError" in (result.reason or "")
    assert "Target.closeTarget" in fake.commands  # best-effort cleanup

    fake2 = FakeChrome(html_sequence=["<p>x</p>"], getdocument_malformed=True)
    out = await _run_tool(fake2, {"url": "https://example.com/x"})
    assert out.startswith("browser_fetch error:")


# --- 7. Chrome down -> actionable message ---------------------------------------


async def test_chrome_unavailable_names_the_services():
    async def getter(cdp_port: int) -> str:
        raise ChromeUnavailable("connection refused")

    tool = BrowserFetchTool(resolver=_public_resolver, ws_url_getter=getter)
    out = await tool.run({"url": "https://example.com/x"})
    assert out == _CHROME_MISSING_MSG
    assert "chrome-agent" in out and "egress-proxy" in out
    assert "docs/browser-fallback-setup.md" in out
    assert "systemctl" not in out  # no systemd on the macOS host


# --- 8. SSRF pre-check fires before any WS connect -------------------------------


async def test_private_and_tailnet_urls_refused_before_connect():
    getter_called = []

    async def getter(cdp_port: int) -> str:
        getter_called.append(True)
        return "ws://127.0.0.1:1/nope"

    async def tailnet_resolver(host: str) -> list[str]:
        return ["100.64.0.9"]

    for resolver, url in (
        (_public_resolver, "http://192.168.1.10/"),
        (_public_resolver, "http://127.0.0.1:8787/"),
        (tailnet_resolver, "https://intranet.example/"),
    ):
        tool = BrowserFetchTool(resolver=resolver, ws_url_getter=getter)
        out = await tool.run({"url": url})
        assert "browser_fetch error" in out
        assert "private or internal" in out
    assert not getter_called  # the guard is first — Chrome is never touched


# --- 9. classification pins -------------------------------------------------------


def test_classification_and_remote_pins():
    assert BrowserFetchTool.classification is ActionClass.READ_ONLY
    assert BrowserFetchTool.remote is True
    assert BrowserFetchTool.name == "browser_fetch"


# --- 10. non-200 SPA shell -> HTTP error, not empty content ----------------------


async def test_non_200_reports_http_status_not_shell_content():
    body = "<html><body><nav>Buy Rent Sold</nav></body></html>"
    fake = FakeChrome(html_sequence=[body], status=404)
    out = await _run_tool(fake, {"url": "https://example.com/nope"})
    assert out == "browser_fetch error: https://example.com/final returned HTTP 404."


# --- 11. >1MB outerHTML arrives intact (max_size regression guard) ---------------


async def test_large_outerhtml_arrives_intact():
    big = "<html><body>" + "a" * 1_500_000 + "</body></html>"
    fake = FakeChrome(html_sequence=[big])
    result = await _run_fetch_page(fake, settle_s=0.1, stable_s=5.0)
    assert result.ok is True
    assert len(result.html) == len(big), (
        "websockets.connect max_size defaults to 1 MB and outerHTML exceeds it — CDPClient must keep its 32 MB"
    )


# --- 12. iframe Document responses never override the main frame -----------------


async def test_iframe_document_response_does_not_override_main_frame():
    """Same-process iframes (ads, consent frames, embedded challenge widgets)
    emit their own type=Document Network.responseReceived on the page session,
    AFTER the main frame's. A 404 + cf-mitigated iframe must not turn a healthy
    page into an HTTP error or a false 'blocked' challenge report."""
    fake = FakeChrome(
        html_sequence=[CLEAN_HTML],
        status=200,
        iframe_doc={"status": 404, "headers": {"cf-mitigated": "challenge"}},
    )
    out = await _run_tool(fake, {"url": "https://example.com/x"})
    assert "hello world from the fake browser" in out
    assert "HTTP 404" not in out
    assert "bot-protection" not in out
    assert "Target.closeTarget" in fake.commands  # not left open as 'blocked'


# --- 13. dead connection fails pending commands and never hangs -------------------


async def test_connection_close_fails_pending_and_fast_fails_next_send():
    """The read loop must fail every pending future on disconnect (no hang on
    a dead socket) and mark the client dead so later sends fail immediately
    instead of waiting out their full per-command timeout."""

    async def swallow_then_close(ws) -> None:
        await ws.recv()  # take the command, never reply
        await ws.close()

    server = await websockets.serve(swallow_then_close, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    cdp = await CDPClient.connect(f"ws://127.0.0.1:{port}/devtools/browser/dead")
    try:
        with pytest.raises(CDPError):
            await asyncio.wait_for(cdp.send("Page.enable", timeout=30.0), 5)
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(CDPError):
            await cdp.send("DOM.enable", timeout=30.0)
        assert loop.time() - start < 1.0  # fast-fail, not the 30s timeout
    finally:
        await cdp.close()
        server.close()
        await server.wait_closed()

# --- 14. internal refusal carries the typed taint marker --------------------------


async def test_private_url_returns_internal_refusal_note():
    """agent._dispatch taints the turn's egress on isinstance of this marker
    — the message text is presentation, the type is the contract."""
    from harness.tools._netguard import InternalRefusalNote

    async def getter(cdp_port: int) -> str:
        raise AssertionError("guard must fire before Chrome is touched")

    tool = BrowserFetchTool(resolver=_public_resolver, ws_url_getter=getter)
    out = await tool.run({"url": "http://127.0.0.1:8787/"})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out

    # Chrome-down (a plain operational error) must NOT carry the marker.
    async def down(cdp_port: int) -> str:
        raise ChromeUnavailable("connection refused")

    tool2 = BrowserFetchTool(resolver=_public_resolver, ws_url_getter=down)
    out2 = await tool2.run({"url": "https://example.com/"})
    assert not isinstance(out2, InternalRefusalNote)


# --- 15. BK-037: relay entry URL refused at the pre-check, Chrome untouched -------

_RELAY_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json").read_text()
)

# Hard copy of the board's criterion-2 directive (convention:
# tests/test_relay_block.py:48) — an import of RELAY_DIRECTIVE would make the
# equality check tautological if the constant drifts.
_RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)

# The fixture shapes browser_fetch itself dialed in Oct 2026 (their audit
# rows' tool field names it — 6 shapes, surface map §6). Pinned at collection:
# an empty selection silently SKIPS the pins (the step-3 lesson).
_RELAY_DIALS = [e for e in _RELAY_CORPUS["blocked"] if "browser_fetch" in e["tools"]]
assert len(_RELAY_DIALS) == 6, (
    f"expected the 6 attested browser_fetch dial shapes, got {len(_RELAY_DIALS)} "
    "— an empty selection would make the BK-037 entry pins vacuous"
)


@pytest.mark.parametrize(
    "shape",
    _RELAY_DIALS,
    ids=[f"dial{i}-{e['signature_form']}" for i, e in enumerate(_RELAY_DIALS)],
)
async def test_relay_entry_url_refused_with_directive_and_no_cdp(shape):
    # browser_fetch is entry-checked ONLY (D3): Chrome follows redirects and
    # re-resolves itself, so check_target at browser_fetch.py:313 is this
    # tool's whole share of criterion 3 — the page's own hops belong to the
    # chrome-proxy (tests/test_egress_proxy.py). The result is the tool's own
    # error string wrapping the directive verbatim; zero resolver lookups (the
    # block precedes DNS); the ws getter is a tripwire (the guard precedes any
    # CDP connection); and no taint — RefusedConfabulated is not
    # RefusedInternal (D2), so the turn is never marked attack-in-progress.
    from harness.tools._netguard import InternalRefusalNote

    lookups: list[str] = []

    async def recording_resolver(host: str) -> list[str]:
        lookups.append(host)
        return ["93.184.216.34"]

    async def never_ws(cdp_port: int) -> str:
        raise AssertionError(
            "GOTCHA REGRESSION [bk-037-browser] Chrome was reached for a relay "
            "entry URL — the pre-check must refuse before any CDP connection"
        )

    tool = BrowserFetchTool(resolver=recording_resolver, ws_url_getter=never_ws)
    out = await tool.run({"url": shape["url"]})
    assert out == f"browser_fetch error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)
    assert lookups == []
