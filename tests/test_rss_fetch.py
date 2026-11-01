"""rss_fetch tool — offline via httpx.MockTransport + a stub resolver.

Mirrors tests/test_web_fetch.py: the stub resolver returns a public address
for any hostname so the SSRF guard passes without DNS; guard tests override it
or use literal IPs. Feed bytes are served by the mock transport — feedparser
must never be handed a URL (it would fetch it itself, past the guard).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harness.core import tool_groups
from harness.core.policy import ActionClass
from harness.tools._netguard import InternalRefusalNote
from harness.tools.rss_fetch import RssFetchTool
from harness.tools._extract import UNTRUSTED_NOTE

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>My Blog</title><link>https://blog.example.com</link>
<item><title>Post One</title><link>https://blog.example.com/1</link>
<author>alice@blog.example.com (Alice)</author>
<pubDate>Tue, 07 Oct 2026 09:00:00 GMT</pubDate>
<description>&lt;p&gt;Hello &amp; welcome &lt;b&gt;world&lt;/b&gt;&lt;/p&gt;</description>
</item>
<item><title>Post Two</title><link>https://blog.example.com/2</link>
<description>Second post body.</description></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<link href="https://a.example.com"/>
<entry><title>A1</title><link href="https://a.example.com/1"/>
<updated>2026-10-08T12:00:00Z</updated>
<author><name>Bob</name></author><summary>Plain summary</summary></entry>
</feed>"""

EMPTY_RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel>'
    "<title>Empty Feed</title><link>https://e.example.com</link></channel></rss>"
)

MALFORMED = "<rss><channel><title>Broken"  # truncated mid-document


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def make_tool(handler) -> RssFetchTool:
    return RssFetchTool(transport=httpx.MockTransport(handler), resolver=public_resolver)


def feed_handler(body: str, content_type: str = "application/rss+xml"):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, text=body)

    return handler


# ---- happy paths --------------------------------------------------------------


async def test_rss20_renders_items_with_metadata():
    out = await make_tool(feed_handler(RSS)).run({"url": "https://blog.example.com/feed"})
    assert out.startswith(UNTRUSTED_NOTE)
    assert "My Blog" in out and "https://blog.example.com" in out
    assert "Post One" in out and "https://blog.example.com/1" in out
    assert "Alice" in out  # author_detail.name, not the raw "email (Name)" blob
    assert "2026-10-07" in out  # pubDate normalized
    assert "Hello & welcome world" in out
    # description HTML is stripped, not passed through raw
    assert "<b>" not in out and "<p>" not in out
    assert "Post Two" in out and "Second post body." in out


async def test_atom_parses_with_author_and_updated():
    out = await make_tool(feed_handler(ATOM, "application/atom+xml")).run(
        {"url": "https://a.example.com/feed"}
    )
    assert "Atom Feed" in out and "A1" in out
    assert "https://a.example.com/1" in out
    assert "Bob" in out and "2026-10-08" in out
    assert "Plain summary" in out


async def test_items_without_optional_fields_still_render():
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Sparse</title>'
        "<item></item>"
        "<item><title>Only a title</title></item>"
        "</channel></rss>"
    )
    out = await make_tool(feed_handler(body)).run({"url": "https://s.example.com/f"})
    assert "Sparse" in out and "Only a title" in out  # no crash on the bare item
    assert "(untitled)" in out


# ---- edge cases: empty, malformed, not-a-feed ---------------------------------


async def test_empty_feed_says_no_items():
    out = await make_tool(feed_handler(EMPTY_RSS)).run({"url": "https://e.example.com/f"})
    assert "no items" in out and "Empty Feed" in out
    assert "error" not in out.lower()


async def test_malformed_xml_returns_clear_error():
    out = await make_tool(feed_handler(MALFORMED)).run({"url": "https://m.example.com/f"})
    assert out.startswith("rss_fetch error:")
    assert "m.example.com" in out


async def test_html_page_is_not_a_feed():
    out = await make_tool(feed_handler("<html><body><p>hi</p></body></html>", "text/html")).run(
        {"url": "https://x.example.com/"}
    )
    assert out.startswith("rss_fetch error:") and "feed" in out


async def test_plain_text_is_not_a_feed():
    out = await make_tool(feed_handler("just text", "text/plain")).run(
        {"url": "https://x.example.com/notes"}
    )
    assert out.startswith("rss_fetch error:") and "feed" in out


async def test_parse_errors_with_partial_items_get_a_note():
    # feedparser is lenient: a feed truncated late yields the items it managed
    # to parse plus bozo — the output must flag that later items may be missing.
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Trunc</title>'
        "<item><title>Kept</title></item>"
        "<item><title>Half"
    )
    out = await make_tool(feed_handler(body)).run({"url": "https://t.example.com/f"})
    assert "Kept" in out
    assert "XML" in out and "missing" in out  # the partial-parse note


# ---- truncation knobs ----------------------------------------------------------


def _many_items(n: int) -> str:
    items = "".join(
        f"<item><title>Item {i}</title><link>https://m.example.com/{i}</link>"
        f"<description>Body {i}</description></item>"
        for i in range(1, n + 1)
    )
    return (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Many</title>'
        f"{items}</channel></rss>"
    )


async def test_max_items_defaults_to_ten_and_truncates_with_note():
    out = await make_tool(feed_handler(_many_items(15))).run(
        {"url": "https://m.example.com/f"}
    )
    assert "Item 10" in out and "Item 11" not in out
    assert "5 more" in out  # hidden-count note so the model knows to raise max_items


async def test_max_items_clamped_to_twenty_five():
    out = await make_tool(feed_handler(_many_items(40))).run(
        {"url": "https://m.example.com/f", "max_items": 99}
    )
    assert "Item 25" in out and "Item 26" not in out


async def test_description_truncated_at_max_chars():
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Long</title>'
        "<item><title>T</title>"
        "<description>" + "d" * 500 + "</description></item></channel></rss>"
    )
    out = await make_tool(feed_handler(body)).run(
        {"url": "https://l.example.com/f", "max_chars": 100}
    )
    assert "d" * 100 in out and "d" * 101 not in out
    assert "[truncated]" in out


async def test_garbage_knob_values_fall_back_to_defaults():
    """Malformed optional args (a type the schema wouldn't accept) must not
    crash the tool or the turn — they fall back to the schema defaults;
    a below-floor value clamps to the floor instead."""
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Knobs</title>'
        "<item><title>K</title><link>https://k.example.com/1</link>"
        f"<description>{'d' * 400}</description></item></channel></rss>"
    )
    out = await make_tool(feed_handler(body)).run(
        {"url": "https://k.example.com/f", "max_items": "oops", "max_chars": {"not": 1}}
    )
    assert "error" not in out.lower()
    assert "d" * 400 in out  # garbage max_chars → default 2000, nothing truncated

    out = await make_tool(feed_handler(body)).run(
        {"url": "https://k.example.com/f", "max_chars": 50}
    )
    assert "[truncated]" in out  # below-floor clamps to 100, doesn't crash


# ---- SSRF guard (same pattern as web_fetch) -------------------------------------


async def test_private_literal_ip_returns_internal_refusal_note():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("private entry must not be fetched")

    out = await make_tool(handler).run({"url": "http://127.0.0.1:8787/feed"})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out


async def test_internal_hostname_refused_via_resolver():
    async def tailnet_resolver(host: str) -> list[str]:
        return ["100.64.0.7"]  # CGNAT range tailscale uses — not global

    tool = RssFetchTool(
        transport=httpx.MockTransport(lambda req: httpx.Response(200)),
        resolver=tailnet_resolver,
    )
    out = await tool.run({"url": "http://my-box.tailnet.ts.net/feed"})
    assert isinstance(out, InternalRefusalNote)


async def test_redirect_to_private_address_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/f"})
        raise AssertionError("private hop must not be fetched")

    out = await make_tool(handler).run({"url": "https://example.com/feed"})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out


async def test_non_http_scheme_refused_without_marker():
    out = await make_tool(lambda req: httpx.Response(200)).run(
        {"url": "ftp://example.com/feed"}
    )
    assert out.startswith("rss_fetch error:") and "only http(s)" in out
    assert not isinstance(out, InternalRefusalNote)


async def test_empty_url_refused():
    out = await make_tool(lambda req: httpx.Response(200)).run({"url": ""})
    assert out.startswith("rss_fetch error:")


# ---- redirect + pinning (web_fetch's three load-bearing properties) -------------


async def test_redirect_followed_and_each_hop_checked():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=EMPTY_RSS)

    out = await make_tool(handler).run({"url": "https://example.com/old"})
    # every hop is re-validated AND re-pinned to the resolved address
    assert hops == ["https://93.184.216.34/old", "https://93.184.216.34/new"]
    assert "no items" in out


async def test_connection_pinned_to_validated_ip():
    """DNS-rebinding guard: connect to the IP that passed the check, hostname
    preserved as Host header + TLS SNI."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=EMPTY_RSS)

    await make_tool(handler).run({"url": "https://example.com:8443/feed"})
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "example.com:8443"
    assert seen["sni"] == "example.com"


async def test_redirect_loop_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    out = await make_tool(handler).run({"url": "https://example.com/again"})
    assert "too many redirects" in out


async def test_feedparser_gets_bytes_never_the_url(monkeypatch):
    """feedparser self-fetches a URL it's handed, past the _netguard checks —
    the tool must only ever pass the already-guarded response bytes."""
    import feedparser as fp_mod

    captured: dict[str, str] = {}
    orig_parse = fp_mod.parse

    def spy(data, *args, **kwargs):
        captured["type"] = type(data).__name__
        return orig_parse(EMPTY_RSS.encode())

    monkeypatch.setattr(fp_mod, "parse", spy)
    out = await make_tool(feed_handler(EMPTY_RSS)).run({"url": "https://e.example.com/f"})
    assert captured["type"] == "bytes", "feedparser must receive bytes, never a URL"
    assert "no items" in out


async def test_client_ignores_proxy_env(monkeypatch):
    """trust_env=False: HTTPS_PROXY etc. must never route around the IP check."""
    captured = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    out = await make_tool(feed_handler(EMPTY_RSS)).run({"url": "https://example.com/f"})
    assert captured["trust_env"] is False
    assert "no items" in out


# ---- HTTP failures ---------------------------------------------------------------


async def test_non_200_reports_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, headers={"content-type": "text/html"}, text="oops")

    out = await make_tool(handler).run({"url": "https://x.example.com/feed"})
    assert "rss_fetch error" in out and "HTTP 500" in out


async def test_timeout_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    out = await make_tool(handler).run({"url": "https://slow.example.com/feed"})
    assert "did not respond within 15s" in out


async def test_unreachable_host_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    out = await make_tool(handler).run({"url": "https://down.example.com/feed"})
    assert "could not reach" in out


async def test_browser_user_agent_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, text=EMPTY_RSS)

    await make_tool(handler).run({"url": "https://example.com/f"})
    assert "Mozilla" in seen["ua"] and "python" not in seen["ua"].lower()


# ---- classification + tool group wiring -------------------------------------------


def test_classification_read_only_and_remote():
    tool = RssFetchTool()
    assert tool.classification is ActionClass.READ_ONLY
    assert tool.remote is True


def test_grouped_into_feeds():
    """The feeds group must exist with a purpose line (the routing line the
    model sees in load_tools' catalog) and rss_fetch deferred into it."""
    assert tool_groups.group_of(RssFetchTool()) == "feeds"
    assert "feeds" in tool_groups.GROUP_DESCRIPTIONS


# ---- BK-037: confabulated relay URLs — entry refusal at the choke ----------------

_RELAY_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json").read_text()
)

# Hard copy of the criterion-2 directive (tests/test_relay_block.py:48's
# convention) — importing RELAY_DIRECTIVE would make this substring check
# tautological: if the constant drifts, these pins go RED.
_RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)


def _one_shape_per_attested_spelling() -> list[dict]:
    """rss_fetch is not among the corpus tools (it never dialed one), so the
    faithful call is one fixture shape per attested host spelling — public
    bucket.oss-region. and dual-stack bucket.region.oss. (surface map §6);
    the shapes themselves come from the corpus, not from invented hosts."""
    seen: set[str] = set()
    shapes = []
    for shape in _RELAY_CORPUS["blocked"]:
        if shape["host_spelling"] not in seen:
            seen.add(shape["host_spelling"])
            shapes.append(shape)
    return shapes


_RELAY_SHAPES = _one_shape_per_attested_spelling()
# A 0-case parametrize is SKIPPED, not failed — a green light wired to
# nothing. The frozen Oct corpus attests exactly 2 host spellings (surface
# map §6); if this filter stops matching, the fixture or its metadata moved.
# Fail at collection instead of silently skipping the pins.
assert len(_RELAY_SHAPES) == 2, (
    f"expected one shape per 2 attested host spellings, got {len(_RELAY_SHAPES)} "
    "— an empty parameter set would make the BK-037 rss pins vacuous"
)


@pytest.mark.parametrize(
    "shape",
    _RELAY_SHAPES,
    ids=[s["endpoint_style"].split("(")[0] for s in _RELAY_SHAPES],
)
async def test_relay_url_refused_at_entry_with_directive_and_zero_egress(
    shape, monkeypatch
):
    # Criterion 3's httpx-entry half at rss_fetch: the relay URL is refused by
    # check_target at the entry hop (rss_fetch.py:160) — the tool's own error
    # string wrapping the directive verbatim, zero transport requests, zero
    # DNS lookups (D2: the block fires before the resolve), and no taint —
    # RefusedConfabulated is NOT RefusedInternal, so the turn is never marked
    # attack-in-progress.
    #
    # The feedparser trap (test_feedparser_gets_bytes_never_the_url above):
    # feedparser self-fetches a URL it is handed, past every _netguard check.
    # The parse spy is what proves THIS refusal happens at the gate — a result
    # that merely looks refused could still have routed the URL into
    # feedparser; parse must never be reached at all.
    import feedparser as fp_mod

    parse_calls: list[object] = []
    orig_parse = fp_mod.parse

    def spy(data, *args, **kwargs):
        parse_calls.append(data)
        return orig_parse(data, *args, **kwargs)

    monkeypatch.setattr(fp_mod, "parse", spy)

    requests: list[str] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-surface] relay target reached the "
            f"transport: {request.url} — the family block belongs in "
            "check_target, before DNS and before any request leaves the machine"
        )

    async def recording_resolver(host: str) -> list[str]:
        resolutions.append(host)
        return ["93.184.216.34"]

    tool = RssFetchTool(
        transport=httpx.MockTransport(handler), resolver=recording_resolver
    )
    out = await tool.run({"url": shape["url"]})
    assert out == f"rss_fetch error: {_RELAY_DIRECTIVE}"
    assert requests == []
    assert resolutions == []
    assert parse_calls == []
    assert not isinstance(out, InternalRefusalNote)


async def test_redirect_to_relay_url_refused_at_the_hop_with_directive(monkeypatch):
    """Criterion 3's per-hop half, measured directly for rss_fetch: a clean
    feed URL answers 302 with a corpus relay URL, and the manual redirect
    loop's next check_target (rss_fetch.py:160 — the same call the entry hop
    takes) refuses the hop before feedparser is ever handed anything. The
    entry pin above and step 2's unit pins prove the relay branch; this
    proves the branch is reached ON A HOP."""
    import feedparser as fp_mod

    parse_calls: list[object] = []
    orig_parse = fp_mod.parse

    def spy(data, *args, **kwargs):
        parse_calls.append(data)
        return orig_parse(data, *args, **kwargs)

    monkeypatch.setattr(fp_mod, "parse", spy)

    requests: list[str] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": _RELAY_SHAPES[0]["url"]})
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-hop] relay hop fetched: {request.url} "
            "— rss_fetch must re-run check_target on every redirect target"
        )

    async def recording_resolver(host: str) -> list[str]:
        resolutions.append(host)
        return ["93.184.216.34"]

    tool = RssFetchTool(
        transport=httpx.MockTransport(handler), resolver=recording_resolver
    )
    out = await tool.run({"url": "https://example.com/feed"})
    assert out == f"rss_fetch error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)  # confabulation never taints
    assert len(requests) == 1 and "aliyuncs" not in requests[0]  # entry hop only
    assert resolutions == ["example.com"]  # relay host never resolved
    assert parse_calls == []
