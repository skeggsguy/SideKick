"""web_fetch tool — offline via httpx.MockTransport + a stub resolver.

The stub resolver returns a public address for any hostname so the SSRF guard
passes without DNS; guard tests override it or use literal IPs.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harness.core.policy import ActionClass
from harness.tools.web_fetch import UNTRUSTED_NOTE, WebFetchTool, html_to_text

HTML = """
<html><head><title>My Page</title><style>.x{color:red}</style></head>
<body><script>alert("evil")</script>
<h1>Heading</h1><p>First para.</p><p>Second   para.</p></body></html>
"""


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def make_tool(handler) -> WebFetchTool:
    return WebFetchTool(
        transport=httpx.MockTransport(handler), resolver=public_resolver
    )


def test_html_to_text_strips_script_and_style():
    title, text = html_to_text(HTML)
    assert title == "My Page"
    assert "Heading" in text and "First para." in text and "Second para." in text
    assert "alert" not in text and "color:red" not in text


async def test_fetch_html_returns_labeled_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=HTML)

    out = await make_tool(handler).run({"url": "https://example.com/page"})
    assert out.startswith(UNTRUSTED_NOTE)
    assert "My Page" in out and "First para." in out
    assert "alert" not in out


async def test_browser_user_agent_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    await make_tool(handler).run({"url": "https://example.com"})
    assert "Mozilla" in seen["ua"] and "python" not in seen["ua"].lower()


async def test_plain_text_and_json_pass_through():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/json"}, text='{"a": 1}'
        )

    out = await make_tool(handler).run({"url": "https://example.com/api"})
    assert '{"a": 1}' in out


async def test_binary_content_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF"
        )

    out = await make_tool(handler).run({"url": "https://example.com/f.pdf"})
    assert "error" in out and "application/pdf" in out


async def test_max_chars_truncates_with_note():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain"}, text="z" * 5000
        )

    out = await make_tool(handler).run({"url": "https://example.com", "max_chars": 200})
    assert "[truncated at 200 chars]" in out
    assert "z" * 201 not in out


async def test_redirect_followed_and_each_hop_checked():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    out = await make_tool(handler).run({"url": "https://example.com/old"})
    # every hop is re-validated AND re-pinned to the resolved address
    assert hops == ["https://93.184.216.34/old", "https://93.184.216.34/new"], (
        "web_fetch follows redirects MANUALLY, connects to the validated IP literal and runs "
        "trust_env=False — each is an SSRF layer; don't simplify any of the three"
    )
    assert "ok" in out


async def test_redirect_to_private_address_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1:8787/"})
        raise AssertionError("private hop must not be fetched")

    out = await make_tool(handler).run({"url": "https://example.com/"})
    assert "private or internal" in out


async def test_connection_pinned_to_validated_ip():
    """DNS-rebinding guard: the request goes to the IP that passed the check,
    with the hostname preserved as Host header + TLS SNI."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    await make_tool(handler).run({"url": "https://example.com:8443/x"})
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "example.com:8443"
    assert seen["sni"] == "example.com"


async def test_client_ignores_proxy_env(monkeypatch):
    """trust_env=False: HTTPS_PROXY etc. must never route around the IP check."""
    captured = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")

    out = await make_tool(handler).run({"url": "https://example.com"})
    assert captured["trust_env"] is False
    assert "ok" in out


async def test_redirect_loop_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    out = await make_tool(handler).run({"url": "https://example.com/again"})
    assert "too many redirects" in out


async def test_private_and_literal_ip_targets_refused():
    tool = make_tool(lambda req: httpx.Response(200))
    for url in (
        "http://127.0.0.1:8787/api/sessions",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://[::1]/",
    ):
        assert "private or internal" in await tool.run({"url": url})


async def test_internal_hostname_refused_via_resolver():
    async def tailnet_resolver(host: str) -> list[str]:
        return ["100.64.0.7"]  # CGNAT range tailscale uses — not global

    tool = WebFetchTool(
        transport=httpx.MockTransport(lambda req: httpx.Response(200)),
        resolver=tailnet_resolver,
    )
    out = await tool.run({"url": "http://my-box.tailnet.ts.net/"})
    assert "private or internal" in out


async def test_non_http_scheme_refused():
    out = await make_tool(lambda req: httpx.Response(200)).run(
        {"url": "ftp://example.com/x"}
    )
    assert "only http(s)" in out


async def test_unresolvable_host_refused():
    async def failing_resolver(host: str) -> list[str]:
        raise OSError("no dns")

    tool = WebFetchTool(
        transport=httpx.MockTransport(lambda req: httpx.Response(200)),
        resolver=failing_resolver,
    )
    out = await tool.run({"url": "https://nope.invalid/"})
    assert "could not resolve" in out


def test_classification_read_only_and_remote():
    tool = WebFetchTool()
    assert tool.classification is ActionClass.READ_ONLY
    assert tool.remote is True


# --- WP-A1: trafilatura readability + RSS types --------------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "html"


def _html_handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=body)

    return handler


async def test_boilerplate_page_yields_article_not_nav_or_cookie():
    body = (_FIXTURES / "boilerplate.html").read_text()
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/aardvark"}
    )
    # the readable article survives
    assert "termite colonies" in out and "satellite collars" in out
    # nav / footer / cookie-banner boilerplate is stripped by trafilatura
    assert "Wholesale Enquiries" not in out
    assert "We use cookies" not in out
    assert "Accept All Cookies" not in out
    assert "Modern Slavery" not in out


async def test_trafilatura_hostile_page_falls_back_to_stdlib_text():
    # trafilatura returns nothing for a page whose only content is in <aside>;
    # the stdlib extractor still recovers the text.
    body = (
        "<html><head><title>Sidebar Page</title></head><body>"
        "<aside>UNIQUE_ASIDE_MARKER sidebar text that trafilatura discards"
        "</aside></body></html>"
    )
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/side"})
    assert "UNIQUE_ASIDE_MARKER" in out


async def test_rss_content_type_accepted():
    rss = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>'
        "<item><title>UNIQUE_RSS_ITEM</title></item></channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/rss+xml"}, text=rss
        )

    out = await make_tool(handler).run({"url": "https://example.com/feed.xml"})
    assert "UNIQUE_RSS_ITEM" in out
    assert "only text content is supported" not in out


async def test_title_in_header_on_both_extraction_paths():
    # readable (trafilatura) path
    body1 = (_FIXTURES / "boilerplate.html").read_text()
    out1 = await make_tool(_html_handler(body1)).run(
        {"url": "https://example.com/aardvark"}
    )
    header1 = out1.splitlines()[1]  # line after the untrusted-data note
    assert "https://example.com/aardvark" in header1
    assert "Aardvark Migration Study" in header1

    # stdlib-fallback path (trafilatura yields nothing)
    body2 = (
        "<html><head><title>Fallback Title Page</title></head><body>"
        "<aside>UNIQUE_ASIDE_MARKER content</aside></body></html>"
    )
    out2 = await make_tool(_html_handler(body2)).run({"url": "https://example.com/side"})
    header2 = out2.splitlines()[1]
    assert "Fallback Title Page" in header2


# --- WP-A2: embedded structured data + extract param ----------------------------

SECTION_HEADER = "--- embedded structured data"


async def test_jsonld_extracted_in_auto_mode():
    body = (_FIXTURES / "jsonld_listings.html").read_text()
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/buy"})
    assert f"{SECTION_HEADER} (application/ld+json) ---" in out
    assert '"@type":"Residence"' in out
    assert "12 Maple Street" in out and "4/88 Station Road" in out
    # auto = prose AND structured data
    assert "No properties found" in out


async def test_extract_text_mode_omits_structured_section():
    body = (_FIXTURES / "jsonld_listings.html").read_text()
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy", "extract": "text"}
    )
    assert SECTION_HEADER not in out
    assert "12 Maple Street" not in out
    assert "No properties found" in out


async def test_extract_structured_mode_returns_only_structured():
    body = (_FIXTURES / "jsonld_listings.html").read_text()
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy", "extract": "structured"}
    )
    assert f"{SECTION_HEADER} (application/ld+json) ---" in out
    assert "12 Maple Street" in out
    assert "No properties found" not in out  # prose omitted


async def test_structured_mode_says_when_nothing_found():
    body = "<html><head><title>Plain</title></head><body><p>Just prose.</p></body></html>"
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/p", "extract": "structured"}
    )
    assert "no embedded structured data found" in out


async def test_next_data_drilled_to_page_props_and_compacted():
    body = (_FIXTURES / "nextdata.html").read_text()
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/buy"})
    assert f"{SECTION_HEADER} (__NEXT_DATA__ props.pageProps) ---" in out
    assert "3 Example Road" in out
    # keys outside props.pageProps are dropped by the drill
    assert "IRRELEVANT_BLOAT" not in out
    # list of 14 pruned to 10 + count marker
    assert "(+4 more items)" in out
    assert "13 Example Road" not in out
    # strings >300 chars truncated
    assert "D" * 300 in out and "D" * 301 not in out


async def test_window_nuxt_state_extracted():
    body = (_FIXTURES / "nuxt_state.html").read_text()
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/"})
    assert f"{SECTION_HEADER} (window.__NUXT__) ---" in out
    assert "NUXT_STATE_SENTINEL" in out and "7 Nuxt Avenue" in out


async def test_malformed_embedded_json_skipped_silently():
    body = (
        "<html><head><title>Broken</title></head><body>"
        '<script type="application/ld+json">{"@type": "Residence", broken</script>'
        "<script>window.__INITIAL_STATE__={oops: not json};</script>"
        "<p>Visible prose survives.</p></body></html>"
    )
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/b"})
    assert SECTION_HEADER not in out
    assert "Visible prose survives." in out
    out2 = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/b", "extract": "structured"}
    )
    assert "no embedded structured data found" in out2


async def test_valid_blob_survives_alongside_malformed_one():
    good = (
        '{"@context":"https://schema.org","@type":"Residence",'
        '"name":"VALID_BLOB_SENTINEL","address":"1 Good Street, Springfield 12345"}'
    )
    body = (
        "<html><body>"
        '<script type="application/ld+json">{bad json</script>'
        f'<script type="application/ld+json">{good}</script>'
        "</body></html>"
    )
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/m"})
    assert "VALID_BLOB_SENTINEL" in out


def test_extract_structured_budgets_and_blob_truncation():
    import json

    from harness.tools._extract import extract_structured

    # each compacted item ~700 chars -> blob well over the 4k per-blob cap;
    # two capped blobs would exceed the 8k total budget -> only one survives.
    item = {"description": "x" * 400, "note": "y" * 400}
    obj = {"@type": "ItemList", "items": [item] * 30}
    script = f'<script type="application/ld+json">{json.dumps(obj)}</script>'
    blobs = extract_structured(f"<html><body>{script * 3}</body></html>")
    assert len(blobs) == 1
    kind, blob = blobs[0]
    assert kind == "application/ld+json"
    assert blob.endswith("…[blob truncated]")
    assert len(blob) <= 4_000 + len("…[blob truncated]")
    assert sum(len(b) for _, b in blobs) <= 8_000

    # list pruning marker (small items, blob under the per-blob cap)
    small = {"@type": "ItemList", "items": [{"n": i} for i in range(30)]}
    script2 = f'<script type="application/ld+json">{json.dumps(small)}</script>'
    blobs2 = extract_structured(f"<html><body>{script2}</body></html>")
    assert len(blobs2) == 1
    assert "(+20 more items)" in blobs2[0][1]  # 30-item list pruned to 10


def test_extract_structured_skips_tiny_blobs():
    from harness.tools._extract import extract_structured

    html = '<html><body><script type="application/ld+json">{"a":1}</script></body></html>'
    assert extract_structured(html) == []


# --- WP-A3: bot-challenge detection + JS-rendered note ---------------------------


async def test_cloudflare_challenge_403_reported_as_challenge():
    body = (_FIXTURES / "cloudflare_challenge.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"content-type": "text/html", "cf-mitigated": "challenge"},
            text=body,
        )

    out = await make_tool(handler).run({"url": "https://www.example-realty.test/"})
    assert out == (
        "web_fetch error: www.example-realty.test is behind a bot-protection "
        "challenge (Cloudflare) — this page cannot be fetched; use web_search "
        "snippets or newsletter email instead."
    )


async def test_challenge_detected_from_body_markers_without_header():
    body = (_FIXTURES / "cloudflare_challenge.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, headers={"content-type": "text/html"}, text=body)

    out = await make_tool(handler).run({"url": "https://example.com/"})
    assert "bot-protection challenge (Cloudflare)" in out
    assert "HTTP 503" not in out


async def test_plain_403_still_reports_http_403():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, headers={"content-type": "text/html"}, text="<html>Forbidden</html>"
        )

    out = await make_tool(handler).run({"url": "https://example.com/x"})
    assert "returned HTTP 403" in out
    assert "bot-protection" not in out


async def test_datadome_and_perimeterx_markers_detected():
    async def check(marker: str, vendor: str) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                403,
                headers={"content-type": "text/html"},
                text=f"<html><body><script src='https://{marker}/x.js'></script></body></html>",
            )

        out = await make_tool(handler).run({"url": "https://example.com/"})
        assert f"bot-protection challenge ({vendor})" in out

    await check("captcha-delivery.com", "DataDome")
    await check("px-captcha.example", "PerimeterX")


async def test_js_rendered_note_on_script_heavy_shell():
    scripts = "".join(f'<script src="/chunk-{i}.js"></script>' for i in range(12))
    filler = '<script>var boot="' + "a" * 6000 + '";</script>'
    body = (
        f"<html><head><title>Shell App</title>{scripts}{filler}</head>"
        '<body><div id="root"></div></body></html>'
    )
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/app"})
    assert (
        "[note: this page appears to render its content with JavaScript — "
        "the static HTML contains little readable text. Try browser_fetch on "
        "the same URL to render it in a real browser.]"
    ) in out


async def test_js_note_suppressed_when_structured_data_found():
    # jsonld_listings.html is deliberately script-heavy with placeholder prose —
    # exactly the shape where the note must NOT fire because JSON-LD was found.
    body = (_FIXTURES / "jsonld_listings.html").read_text()
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/buy"})
    assert SECTION_HEADER in out
    assert "render its content with JavaScript" not in out
    # ... in ANY mode: the heuristic sees the page's data even in text mode
    out_text = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy", "extract": "text"}
    )
    assert "render its content with JavaScript" not in out_text


async def test_small_plain_page_gets_no_js_note():
    body = "<html><head><title>Tiny</title></head><body><p>hello</p></body></html>"
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/t"})
    assert "render its content with JavaScript" not in out


# --- extract="full" + thin-content note (listing-grid regression fix) -----------

THIN_NOTE = (
    "[note: readable extraction may be incomplete for this page (it looks "
    'like a listing or directory grid); retry with extract="full" for the '
    "complete page text]"
)


async def test_listing_grid_auto_thin_note_and_full_surfaces_addresses():
    body = (_FIXTURES / "listing_grid.html").read_text()

    # auto: trafilatura keeps the intro but drops the card grid -> thin note
    out_auto = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy"}
    )
    assert "Browse our current properties" in out_auto  # intro prose kept
    assert "15 Example Street" not in out_auto  # cards dropped by readability
    assert THIN_NOTE in out_auto, (
        "trafilatura drops card grids — listings need extract=\"full\" (and trafilatura stays extraction-only)"
    )
    # listing_grid.html sits ~8 chars under _JS_TEXT_THRESHOLD: keep fixture prose edits length-neutral.
    assert "render its content with JavaScript" not in out_auto  # not a JS shell

    # full: complete stdlib extraction — every card address surfaces
    out_full = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy", "extract": "full"}
    )
    for street_no in (10, 15, 25, 39):
        assert f"{street_no} Example Street, Springfield 12345" in out_full
    assert THIN_NOTE not in out_full
    # header/title contract holds on the full path (stdlib title)
    header = out_full.splitlines()[1]
    assert "Grid Example Realty" in header


async def test_full_mode_never_calls_trafilatura(monkeypatch):
    # The readable/trafilatura pipeline now lives in _extract.render_page (shared
    # by web_fetch + browser_fetch), so the guard patches its call site there.
    import harness.tools._extract as ex

    async def boom(html: str, url: str):
        raise AssertionError("extract='full' must not call trafilatura")

    monkeypatch.setattr(ex, "extract_readable", boom)
    body = (_FIXTURES / "listing_grid.html").read_text()
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy", "extract": "full"}
    )
    assert "15 Example Street" in out


async def test_full_mode_includes_structured_section_when_present():
    body = (_FIXTURES / "jsonld_listings.html").read_text()
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/buy", "extract": "full"}
    )
    assert f"{SECTION_HEADER} (application/ld+json) ---" in out
    assert "12 Maple Street" in out  # from the JSON-LD blobs
    assert "No properties found" in out  # full prose too


async def test_article_gets_no_thin_content_note():
    body = (_FIXTURES / "boilerplate.html").read_text()
    out = await make_tool(_html_handler(body)).run(
        {"url": "https://example.com/aardvark"}
    )
    assert "termite colonies" in out  # substantial readable text
    assert THIN_NOTE not in out


async def test_both_notes_eligible_prefers_js_note():
    # listing grid (thin-eligible) made script-heavy (JS-eligible): the JS-shell
    # signal wins and the thin-content note stays quiet.
    body = (_FIXTURES / "listing_grid.html").read_text()
    scripts = "".join(f'<script src="/chunk-{i}.js"></script>' for i in range(12))
    body = body.replace("</head>", f"{scripts}</head>")
    out = await make_tool(_html_handler(body)).run({"url": "https://example.com/buy"})
    assert "render its content with JavaScript" in out
    assert THIN_NOTE not in out


# ---- egress hardening: URL bound + typed internal-refusal marker ------------

async def test_overlong_url_refused_no_network():
    """Control D: a URL over the sanity bound is refused before DNS/transport
    (a loose cap — chunking defeats any length limit; taints/budget do the
    real work)."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("overlong URL must never reach the network")

    url = "https://example.com/?d=" + "x" * 5000
    out = await make_tool(handler).run({"url": url})
    assert "web_fetch error" in out
    assert "4096" in out


async def test_private_target_returns_internal_refusal_note():
    """The internal refusal rides a TYPED marker (InternalRefusalNote), not a
    string match — agent._dispatch taints the turn on isinstance. Covers the
    entry URL and a redirect hop into a private address."""
    from harness.tools._netguard import InternalRefusalNote

    def entry_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("private entry must not be fetched")

    out = await make_tool(entry_handler).run({"url": "http://127.0.0.1:8787/"})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out

    def hop_handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/"})
        raise AssertionError("private hop must not be fetched")

    out = await make_tool(hop_handler).run({"url": "https://example.com/"})
    assert isinstance(out, InternalRefusalNote)

    # A non-internal refusal (bad scheme) must NOT carry the marker.
    out = await make_tool(entry_handler).run({"url": "ftp://example.com/"})
    assert not isinstance(out, InternalRefusalNote)


# ---- BK-037: confabulated relay URLs — entry refusal at the choke ----------------

_RELAY_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json").read_text()
)

# Hard copy of the board's criterion-2 directive, the convention set by
# tests/test_relay_block.py:48 — importing RELAY_DIRECTIVE would make the
# substring check tautological: if the constant drifts, these pins go RED.
_RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)

# The fixture shapes web_fetch itself dialed in Oct 2026 (their audit rows'
# tool field names it) — the attested shapes are the shapes this surface
# proves blocked; no invented hosts.
_RELAY_DIALS = [e for e in _RELAY_CORPUS["blocked"] if "web_fetch" in e["tools"]]
# A 0-case parametrize is SKIPPED, not failed — a green light wired to
# nothing. The frozen Oct corpus attests exactly 8 web_fetch dial shapes
# (surface map §6); if this filter stops matching, the fixture or its
# metadata moved. Fail at collection instead of silently skipping the pins.
assert len(_RELAY_DIALS) == 8, (
    f"expected the 8 attested web_fetch dial shapes, got {len(_RELAY_DIALS)} — "
    "an empty parameter set would make the BK-037 entry pins vacuous"
)


def _relay_guarded_tool():
    """A tool whose two egress checkpoints both leave evidence: the fake
    transport trips on ANY request, the resolver records every lookup. A
    relay entry refusal must record zero of each — D2 fires the block before
    the resolve, and the resolve is what precedes the connect."""
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

    tool = WebFetchTool(
        transport=httpx.MockTransport(handler), resolver=recording_resolver
    )
    return tool, requests, resolutions


@pytest.mark.parametrize(
    "shape",
    _RELAY_DIALS,
    ids=[f"dial{i}-{e['signature_form']}" for i, e in enumerate(_RELAY_DIALS)],
)
async def test_relay_url_refused_at_entry_with_directive_and_zero_egress(shape):
    # Criterion 3's httpx-entry half + criterion 2 at this surface: the relay
    # URL in the `url` argument is refused by check_target at web_fetch's
    # entry hop (web_fetch.py:164) — the tool's own error string wrapping the
    # directive verbatim, zero transport requests, zero DNS lookups. And no
    # taint: RefusedConfabulated is NOT RefusedInternal (D2), so this refusal
    # never marks the turn as an attack-in-progress.
    from harness.tools._netguard import InternalRefusalNote

    tool, requests, resolutions = _relay_guarded_tool()
    out = await tool.run({"url": shape["url"]})
    assert out == f"web_fetch error: {_RELAY_DIRECTIVE}"
    assert requests == []
    assert resolutions == []
    assert not isinstance(out, InternalRefusalNote)


async def test_redirect_to_relay_url_refused_at_the_hop_with_directive():
    """Criterion 3's per-hop half, measured directly for web_fetch: a clean
    entry URL answers 302 with a corpus relay URL, and the manual redirect
    loop's next check_target (web_fetch.py:164 — the same call the entry hop
    takes) refuses the hop. The entry pin above and step 2's unit pins prove
    the relay branch; this proves the branch is reached ON A HOP, so the
    board's "redirects are covered too" rests on a measurement, not on
    composing two other green tests."""
    from harness.tools._netguard import InternalRefusalNote

    requests: list[str] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": _RELAY_DIALS[0]["url"]})
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-hop] relay hop fetched: {request.url} "
            "— web_fetch must re-run check_target on every redirect target"
        )

    async def recording_resolver(host: str) -> list[str]:
        resolutions.append(host)
        return ["93.184.216.34"]

    tool = WebFetchTool(
        transport=httpx.MockTransport(handler), resolver=recording_resolver
    )
    out = await tool.run({"url": "https://example.com/start"})
    assert out == f"web_fetch error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)  # confabulation never taints
    assert len(requests) == 1 and "aliyuncs" not in requests[0]  # entry hop only
    assert resolutions == ["example.com"]  # relay host never resolved
