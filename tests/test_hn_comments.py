"""hn_comments tool — offline via httpx.MockTransport + a stub resolver.

Mirrors tests/test_rss_fetch.py. Every request goes to the Firebase API
(hacker-news.firebaseio.com/v0/item/<id>.json), so the mock routes on the
item id in the path — the thread is a dict of fake items. Beyond the happy
path the file pins the two fetch-budget rules: depth stops at replies
(grandchildren are NEVER fetched) and the kid scan stops at max_comments.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from harness.core import tool_groups
from harness.core.policy import ActionClass
from harness.tools._extract import UNTRUSTED_NOTE
from harness.tools._netguard import InternalRefusalNote
from harness.tools.hn_comments import HnCommentsTool


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def comment(by: str, text: str, kids: list[int] | None = None, **extra) -> dict:
    item = {"id": 0, "type": "comment", "by": by, "text": text}
    if kids:
        item["kids"] = kids
    item.update(extra)
    return item


def story(kids: list[int], title: str = "Show HN: My tool") -> dict:
    return {"id": 123, "type": "story", "title": title, "score": 300, "by": "pg", "kids": kids}


def make_tool(handler) -> HnCommentsTool:
    return HnCommentsTool(transport=httpx.MockTransport(handler), resolver=public_resolver)


def items_handler(items: dict[int, dict | None], story_item: dict | None = None):
    """Serve /v0/item/<id>.json from a dict; ids absent from it answer 404.
    ``fetched`` records the ids requested, in order."""
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item_id = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        fetched.append(item_id)
        if item_id == "123":
            data = story_item
        else:
            if int(item_id) not in items:
                return httpx.Response(404, text="404 Not Found")
            data = items[int(item_id)]
        return httpx.Response(200, headers={"content-type": "application/json"}, text=json.dumps(data))

    handler.fetched = fetched
    return handler


POST = "https://news.ycombinator.com/item?id=123"


# ---- happy path + threading --------------------------------------------------------


async def test_thread_renders_top_comments_with_nested_replies():
    items = {
        4: comment("alice", "Top comment <p>second para</p>", kids=[8, 9, 10, 11]),
        5: {"id": 5, "deleted": True},
        6: comment("bob", "Second comment"),
        7: comment("zed", "killed", kids=None, dead=True),
        8: comment("carol", "a reply", kids=[12]),  # grandchild never fetched
        9: {"id": 9, "type": "comment", "by": "deleted", "text": "gone"},
        10: comment("dave", "another reply"),
        11: comment("erin", "fourth kid — over the reply cap"),
        12: comment("frank", "DEEP grandchild"),
    }
    handler = items_handler(items, story(kids=[4, 5, 6, 7]))
    out = await make_tool(handler).run({"url": POST})

    assert out.startswith(UNTRUSTED_NOTE)
    assert "Show HN: My tool" in out and "300 points" in out and "by pg" in out
    assert "news.ycombinator.com/item?id=123" in out
    assert "1. **alice**" in out and "2. **bob**" in out  # HN's kid order kept
    assert "Top comment" in out and "second para" in out  # <p> became a line break
    assert "<p>" not in out
    assert "  > **carol**" in out and "  > **dave**" in out  # replies nested with "> "
    assert "  > " in out[out.index("1. **alice**") : out.index("2. **bob**")]
    assert "erin" not in out  # reply cap: only 3 kid lookups per comment
    assert "frank" not in out and "DEEP" not in out  # depth cap: never fetched
    assert "deleted" not in out and "zed" not in out  # deleted/dead skipped
    assert "12" not in handler.fetched  # grandchildren are not requested at all


async def test_items_fetched_sequentially_in_kid_order():
    items = {i: comment(f"u{i}", f"c{i}") for i in (4, 5, 6)}
    handler = items_handler(items, story(kids=[4, 5, 6]))
    await make_tool(handler).run({"url": POST})
    assert handler.fetched == ["123", "4", "5", "6"]


# ---- fetch budget: scan cap + comment cap -------------------------------------------


async def test_max_comments_default_five_and_clamped_to_ten():
    items = {i: comment(f"u{i}", f"c{i}") for i in range(4, 24)}
    handler = items_handler(items, story(kids=list(range(4, 24))))
    out = await make_tool(handler).run({"url": POST})
    assert "u8" in out and "u9" not in out  # default 5, stops scanning at quota
    assert handler.fetched[-1] == "8"

    handler = items_handler(items, story(kids=list(range(4, 24))))
    out = await make_tool(handler).run({"url": POST, "max_comments": 99})
    assert "u13" in out and "u14" not in out  # clamped to 10


async def test_deleted_kids_do_not_consume_the_quota():
    items = {4: {"deleted": True}, 5: {"dead": True}, 6: comment("a", "kept")}
    handler = items_handler(items, story(kids=[4, 5, 6]))
    out = await make_tool(handler).run({"url": POST})
    assert "kept" in out  # scan continued past deleted/dead


# ---- knobs ---------------------------------------------------------------------------


async def test_max_chars_truncates_comment_and_reply():
    items = {
        4: comment("alice", "d" * 500, kids=[8]),
        8: comment("carol", "r" * 500),
    }
    out = await make_tool(items_handler(items, story(kids=[4]))).run(
        {"url": POST, "max_chars": 100}
    )
    assert "d" * 100 in out and "d" * 101 not in out
    assert "r" * 100 in out and "r" * 101 not in out
    assert "[truncated]" in out


async def test_garbage_knob_values_fall_back_to_defaults():
    items = {4: comment("a", "ok", kids=[8]), 8: comment("b", "reply")}
    handler = items_handler(items, story(kids=[4]))
    out = await make_tool(handler).run(
        {"url": POST, "max_comments": "oops", "max_chars": {"not": 1}}
    )
    assert "error" not in out.lower() and "ok" in out and "reply" in out

    out = await make_tool(items_handler(items, story(kids=[4]))).run(
        {"url": POST, "max_chars": 5}
    )
    assert "ok" in out  # below-floor clamps to 40, doesn't crash


# ---- URL validation: no fetch off the HN domain ---------------------------------------


def _never_fetch(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"must not fetch {request.url}")


async def test_non_hn_urls_error_without_fetching():
    tool = make_tool(_never_fetch)
    for url in (
        "https://evil.test/item?id=123",
        "https://news.ycombinator.com.evil.test/item?id=123",  # suffix trick
        "https://news.ycombinator.com@evil.test/item?id=123",  # userinfo trick
        "https://old.reddit.com/r/x/comments/1/",
    ):
        out = await tool.run({"url": url})
        assert out.startswith("hn_comments error:") and "not a Hacker News post URL" in out, url


async def test_missing_or_non_numeric_id_refused():
    tool = make_tool(_never_fetch)
    for url in (
        "https://news.ycombinator.com/item",
        "https://news.ycombinator.com/item?id=abc",
        "https://news.ycombinator.com/",
        "",
    ):
        out = await tool.run({"url": url})
        assert out.startswith("hn_comments error:") and "not a Hacker News post URL" in out, url


# ---- empty + API failures ---------------------------------------------------------------


async def test_story_without_kids_says_no_comments():
    out = await make_tool(items_handler({}, {"id": 123, "type": "story", "title": "T"})).run(
        {"url": POST}
    )
    assert "no comments" in out and "error" not in out.lower()


async def test_missing_item_200_null_and_404_both_report_no_item():
    out = await make_tool(items_handler({}, None)).run({"url": POST})
    assert "no item 123" in out

    def not_found(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="404 Not Found")

    out = await make_tool(not_found).run({"url": POST})
    assert "no item 123" in out


async def test_story_http_error_reports_status():
    out = await make_tool(lambda req: httpx.Response(500)).run({"url": POST})
    assert "hn_comments error" in out and "HTTP 500" in out


async def test_malformed_story_json_reports_parse_error():
    out = await make_tool(lambda req: httpx.Response(200, text="<html>nope")).run({"url": POST})
    assert "hn_comments error" in out and "parse" in out


async def test_failing_child_fetch_is_skipped_others_render():
    items = {5: comment("bob", "kept")}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/item/4.json" in request.url.path:
            return httpx.Response(500, text="boom")
        return items_handler(items, story(kids=[4, 5]))(request)

    out = await make_tool(handler).run({"url": POST})
    assert "kept" in out and "error" not in out.lower()


async def test_timeout_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    out = await make_tool(handler).run({"url": POST})
    assert "did not respond within 15s" in out


async def test_unreachable_host_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope")

    out = await make_tool(handler).run({"url": POST})
    assert "could not reach" in out


# ---- SSRF guard (same pattern as rss_fetch) ----------------------------------------------


async def test_rebounding_firebase_dns_returns_internal_refusal_note():
    async def tailnet_resolver(host: str) -> list[str]:
        return ["100.64.0.7"]  # CGNAT range tailscale uses — not global

    tool = HnCommentsTool(transport=httpx.MockTransport(_never_fetch), resolver=tailnet_resolver)
    out = await tool.run({"url": POST})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out


async def test_redirect_to_private_address_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "hacker-news.firebaseio.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/x.json"})
        raise AssertionError("private hop must not be fetched")

    out = await make_tool(handler).run({"url": POST})
    assert isinstance(out, InternalRefusalNote)


# ---- redirect + pinning (the three load-bearing properties) --------------------------------


async def test_redirect_followed_and_each_hop_checked():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if b"redirected" not in request.url.query:
            return httpx.Response(
                302, headers={"location": "/v0/item/123.json?redirected=1"}
            )
        return httpx.Response(
            200, text=json.dumps({"id": 123, "type": "story", "title": "T", "kids": []})
        )

    out = await make_tool(handler).run({"url": POST})
    assert len(hops) == 2 and all(h.startswith("https://93.184.216.34") for h in hops)
    assert "no comments" in out


async def test_connection_pinned_to_validated_ip():
    """DNS-rebinding guard: connect to the IP that passed the check, hostname
    preserved as Host header + TLS SNI."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(
            200, text=json.dumps({"id": 123, "type": "story", "title": "T", "kids": []})
        )

    await make_tool(handler).run({"url": POST})
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "hacker-news.firebaseio.com"
    assert seen["sni"] == "hacker-news.firebaseio.com"


async def test_redirect_loop_bounded():
    out = await make_tool(
        lambda req: httpx.Response(302, headers={"location": "/again"})
    ).run({"url": POST})
    assert "too many redirects" in out
    assert not isinstance(out, InternalRefusalNote)  # a loop is not a probe


async def test_client_ignores_proxy_env(monkeypatch):
    """trust_env=False: HTTPS_PROXY etc. must never route around the IP check."""
    captured = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    out = await make_tool(items_handler({}, story(kids=[]))).run({"url": POST})
    assert captured["trust_env"] is False
    assert "no comments" in out


async def test_browser_user_agent_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(
            200, text=json.dumps({"id": 123, "type": "story", "title": "T", "kids": []})
        )

    await make_tool(handler).run({"url": POST})
    assert "Mozilla" in seen["ua"] and "python" not in seen["ua"].lower()


# ---- classification + tool group wiring ----------------------------------------------------


def test_classification_read_only_and_remote():
    tool = HnCommentsTool()
    assert tool.classification is ActionClass.READ_ONLY
    assert tool.remote is True


def test_grouped_into_feeds():
    assert tool_groups.group_of(HnCommentsTool()) == "feeds"


# ---- BK-037: relay URLs — the item-id pre-gate at entry, the choke on the hop ------

_RELAY_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "egress" / "relay_urls_oct2026.json").read_text()
)

# Hard copy of the criterion-2 directive (tests/test_relay_block.py:48's
# convention) — importing RELAY_DIRECTIVE would make these checks
# tautological: if the constant drifts, these pins go RED.
_RELAY_DIRECTIVE = (
    "fabricated relay URL (known model artifact): re-derive the real URL "
    "from the previous web_search result; do not retry variants"
)


def _one_url_per_attested_spelling() -> list[str]:
    """One fixture shape per attested host spelling (surface map §6: exactly
    two), dialed verbatim — the attested shapes are the shapes proven blocked."""
    seen: set[str] = set()
    urls = []
    for shape in _RELAY_CORPUS["blocked"]:
        if shape["host_spelling"] not in seen:
            seen.add(shape["host_spelling"])
            urls.append(shape["url"])
    return urls


_RELAY_URLS = _one_url_per_attested_spelling()
_RELAY_URL = _RELAY_URLS[0]  # public bucket.oss-region. spelling


async def test_relay_url_as_argument_stops_at_the_item_id_pre_gate():
    """(a) BK-037 surface map §5: _item_id (:66, gate at run :148-150)
    requires news.ycombinator.com/item?id=<digits> and the fetch URL is always
    rebuilt from _API (:55 → hacker-news.firebaseio.com), so a relay URL as
    `url` returns the not-an-HN-URL string with zero requests and can NEVER
    reach check_target — the entry path to the choke is unreachable,
    deliberately. Pinning that pre-gate documents the unreachability; do NOT
    reorder the host validation to put it behind check_target — the rebuilt
    URL never names another host, so it would buy nothing.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-pregate] a relay argument reached the "
            f"transport: {request.url} — the item-id gate + _API rebuild is "
            "what keeps relay URLs away from this tool's client entirely"
        )

    tool = make_tool(handler)
    for url in _RELAY_URLS:
        out = await tool.run({"url": url})
        assert out.startswith("hn_comments error:"), url
        assert "is not a Hacker News post URL" in out, (
            f"{url!r} did not stop at the item-id pre-gate: {out!r} — this pin "
            "requires the _item_id gate + _API rebuild to stay AHEAD of "
            "check_target (plan step 3: do not reorder); if the relay "
            "directive now appears, the pre-gate was moved behind the choke"
        )
        assert _RELAY_DIRECTIVE not in out  # never reaches the choke here
        assert not isinstance(out, InternalRefusalNote)
    assert requests == []


async def test_redirect_to_relay_url_refused_at_the_hop_with_directive():
    """(b) The relay test that IS realizable for this surface: a clean story
    URL whose Firebase fetch answers 302 with a corpus relay URL as Location.
    _get_json re-runs check_target per hop (:233-234, shared by the story
    fetch and every kid fetch — surface map §7), and the family block fires
    there — the directive verbatim in the tool error, the relay host never
    fetched and never even resolved (D2), and no taint."""
    requests: list[str] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.headers["host"] == "hacker-news.firebaseio.com":
            return httpx.Response(302, headers={"location": _RELAY_URL})
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-hop] relay hop fetched: {request.url} "
            "— _get_json must re-run check_target on every redirect target"
        )

    async def recording_resolver(host: str) -> list[str]:
        resolutions.append(host)
        return ["93.184.216.34"]

    tool = HnCommentsTool(
        transport=httpx.MockTransport(handler), resolver=recording_resolver
    )
    out = await tool.run({"url": POST})
    assert out == f"hn_comments error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)  # confabulation never taints
    assert len(requests) == 1 and "aliyuncs" not in requests[0]  # entry hop only
    assert resolutions == ["hacker-news.firebaseio.com"]  # relay never resolved
