"""reddit_comments tool — offline via httpx.MockTransport + a stub resolver.

Mirrors tests/test_rss_fetch.py: the stub resolver returns a public address
for any hostname so the SSRF guard passes without DNS; guard tests override it
or use internal targets. The tool rebuilds every request as
https://www.reddit.com/r/<sub>/comments/<id>/.rss (the .json endpoint is
403-blocked for unauthenticated non-browser clients since 2026-10), so the
mock routes on path — a request that reaches the transport has already passed
the domain check. The Atom fixtures below mirror the live reddit shape: a t3_
submission entry followed by t1_ comment entries with entity-escaped HTML
content.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

import httpx

from harness.core import tool_groups
from harness.core.policy import ActionClass
from harness.tools._extract import UNTRUSTED_NOTE
from harness.tools._netguard import InternalRefusalNote
from harness.tools.reddit_comments import RedditCommentsTool


async def public_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def make_tool(handler) -> RedditCommentsTool:
    return RedditCommentsTool(transport=httpx.MockTransport(handler), resolver=public_resolver)


def comment(cid: str, author: str, body: str) -> str:
    """One t1_ entry as reddit's Atom serves it: escaped HTML content,
    author as /u/<name>."""
    return (
        f"<entry><author><name>/u/{author}</name></author>"
        f"<content type=\"html\">{html.escape(f'<div class=\"md\">{body}</div>')}</content>"
        f"<id>t1_{cid}</id>"
        f"<title>{html.escape(f'/u/{author} on My post')}</title></entry>"
    )


def submission(title: str = "My post", sub: str = "python") -> str:
    return (
        f"<entry><author><name>/u/poster</name></author>"
        f"<category term=\"{sub}\" label=\"r/{sub}\"/>"
        f"<content type=\"html\">{html.escape('<div class=\"md\">selftext here</div>')}</content>"
        f"<id>t3_abc123</id><title>{html.escape(title)}</title></entry>"
    )


def deleted_comment(cid: str = "d1") -> str:
    return (
        f"<entry><author><name>[deleted]</name></author>"
        f"<content type=\"html\">{html.escape('<div class=\"md\">[deleted]</div>')}</content>"
        f"<id>t1_{cid}</id><title>[deleted] on My post</title></entry>"
    )


def reddit_atom(comments: list[str], title: str = "My post", sub: str = "python") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>{html.escape(title)}</title>"
        f'<category term="{sub}" label="r/{sub}"/>'
        f"<id>/r/{sub}/comments/abc123/.rss</id>"
        + submission(title, sub)
        + "".join(comments)
        + "</feed>"
    )


def feed_handler(comments: list[str], **kwargs):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "application/atom+xml"}, text=reddit_atom(comments, **kwargs)
        )

    return handler


POST = "https://www.reddit.com/r/python/comments/abc123/my_post/"


# ---- happy paths ---------------------------------------------------------------


async def test_comments_extracted_in_feed_order_skipping_post_and_junk():
    comments = [
        comment("c1", "alice", "first comment body"),  # feed order IS the order
        comment("c2", "bob", "second comment body"),
        deleted_comment(),
        comment("c3", "carol", "third comment body"),
    ]
    out = await make_tool(feed_handler(comments)).run({"url": POST})
    assert out.startswith(UNTRUSTED_NOTE)
    assert "My post" in out and "r/python" in out
    assert "alice" in out and "bob" in out and "carol" in out
    assert out.index("alice") < out.index("bob") < out.index("carol")
    assert "[deleted]" not in out  # deleted comment skipped
    assert "selftext here" not in out  # the t3_ submission is not a comment
    assert "[truncated]" not in out


async def test_html_content_stripped_to_text():
    comments = [comment("c1", "a", 'links to <a href="https://x.test">a site</a>')]
    out = await make_tool(feed_handler(comments)).run({"url": POST})
    assert "links to a site" in out
    assert "<a href" not in out and "https://x.test" not in out


async def test_feed_without_t3_entry_still_uses_feed_title():
    body = reddit_atom([comment("c1", "a", "ok")]).replace(submission(), "")
    out = await make_tool(
        lambda req: httpx.Response(200, text=body)
    ).run({"url": POST})
    assert "My post" in out and "u/a" in out


# ---- knobs ----------------------------------------------------------------------


def _many(n: int) -> list[str]:
    return [comment(f"c{i}", f"user{i}", f"Comment {i} body") for i in range(1, n + 1)]


async def test_max_comments_default_five_with_hidden_note():
    out = await make_tool(feed_handler(_many(8))).run({"url": POST})
    assert "Comment 5" in out and "Comment 6" not in out
    assert "[3 more comments" in out  # the model learns to raise max_comments


async def test_max_comments_clamped_to_ten():
    out = await make_tool(feed_handler(_many(14))).run({"url": POST, "max_comments": 99})
    assert "Comment 10" in out and "Comment 11" not in out


async def test_max_chars_truncates_bodies():
    out = await make_tool(feed_handler([comment("c1", "a", "d" * 500)])).run(
        {"url": POST, "max_chars": 100}
    )
    assert "d" * 100 in out and "d" * 101 not in out
    assert "[truncated]" in out


async def test_garbage_knob_values_fall_back_to_defaults():
    """Malformed optional args must not crash the tool; below-floor values
    clamp instead."""
    out = await make_tool(
        feed_handler([comment("c1", "a", "e" * 250)])
    ).run({"url": POST, "max_comments": "oops", "max_chars": {"not": 1}})
    assert "error" not in out.lower()
    assert "e" * 250 in out  # default 300 chars, nothing truncated

    out = await make_tool(feed_handler([comment("c1", "a", "d" * 50)])).run(
        {"url": POST, "max_chars": 5}
    )
    assert "d" * 40 in out  # below-floor clamps to 40, doesn't crash


# ---- URL validation: no fetch off the reddit domain ------------------------------


def _never_fetch(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"must not fetch {request.url}")


async def test_non_reddit_url_errors_without_fetching():
    out = await make_tool(_never_fetch).run({"url": "https://evil.test/r/x/comments/1/"})
    assert out.startswith("reddit_comments error:")
    assert "not a Reddit post URL" in out


async def test_lookalike_hosts_refused():
    tool = make_tool(_never_fetch)
    for url in (
        "https://evil-reddit.com/r/x/comments/1/",
        "https://reddit.com.evil.test/r/x/comments/1/",
        "https://www.reddit.com@evil.test/r/x/comments/1/",  # userinfo trick
    ):
        out = await tool.run({"url": url})
        assert out.startswith("reddit_comments error:") and "not a Reddit post" in out, url


async def test_reddit_url_without_comments_path_refused():
    out = await make_tool(_never_fetch).run({"url": "https://www.reddit.com/r/python/top/"})
    assert out.startswith("reddit_comments error:") and "not a Reddit post URL" in out

    out = await make_tool(_never_fetch).run({"url": ""})
    assert out.startswith("reddit_comments error:")


async def test_request_always_rebuilt_to_www_reddit():
    """Whatever reddit host the argument names, the request goes to the
    rebuilt www.reddit.com Atom endpoint (pinned IP), sorted by top."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host_header"] = request.headers["host"]
        seen["path"] = request.url.path
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, text=reddit_atom([comment("c1", "a", "ok")]))

    out = await make_tool(handler).run({"url": "https://old.reddit.com/r/x/comments/abc/why/"})
    assert seen["host_header"] == "www.reddit.com"
    assert seen["path"] == "/r/x/comments/abc/.rss"  # .json is 403-blocked
    assert "sort=top" in seen["query"]
    assert "a" in out


async def test_user_profile_post_urls_work():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, text=reddit_atom([comment("c1", "a", "ok")]))

    await make_tool(handler).run({"url": "https://www.reddit.com/user/someone/comments/zz9/hi/"})
    assert seen["path"] == "/user/someone/comments/zz9/.rss"


# ---- empty + API failures ----------------------------------------------------------


async def test_no_comments_says_so_without_error():
    out = await make_tool(feed_handler([])).run({"url": POST})
    assert "no comments" in out and "error" not in out.lower()


async def test_only_deleted_comments_counts_as_no_comments():
    out = await make_tool(feed_handler([deleted_comment()])).run({"url": POST})
    assert "no comments" in out


async def test_rate_limit_429_gets_retry_hint():
    out = await make_tool(lambda req: httpx.Response(429)).run({"url": POST})
    assert "reddit_comments error:" in out and "429" in out and "rate-limit" in out


async def test_non_200_reports_status():
    out = await make_tool(lambda req: httpx.Response(500, text="oops")).run({"url": POST})
    assert "reddit_comments error" in out and "HTTP 500" in out


async def test_block_page_reports_parse_error():
    """Reddit's 200-but-HTML shapes (or truncated XML) must not crash —
    403 block pages came as HTML bodies, so the non-feed path matters."""
    out = await make_tool(lambda req: httpx.Response(200, text="<html>nope")).run({"url": POST})
    assert "reddit_comments error" in out and "parse" in out


async def test_json_error_dict_reports_parse_error():
    out = await make_tool(
        lambda req: httpx.Response(200, text='{"error": 403}')
    ).run({"url": POST})
    assert "reddit_comments error" in out and "parse" in out


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


# ---- SSRF guard (same pattern as rss_fetch) -----------------------------------------


async def test_rebounding_reddit_dns_returns_internal_refusal_note():
    async def tailnet_resolver(host: str) -> list[str]:
        return ["100.64.0.7"]  # CGNAT range tailscale uses — not global

    tool = RedditCommentsTool(
        transport=httpx.MockTransport(_never_fetch), resolver=tailnet_resolver
    )
    out = await tool.run({"url": POST})
    assert isinstance(out, InternalRefusalNote)
    assert "private or internal" in out


async def test_redirect_to_private_address_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "www.reddit.com":
            return httpx.Response(302, headers={"location": "http://10.0.0.1/x.rss"})
        raise AssertionError("private hop must not be fetched")

    out = await make_tool(handler).run({"url": POST})
    assert isinstance(out, InternalRefusalNote)


# ---- redirect + pinning (the three load-bearing properties) --------------------------


async def test_redirect_followed_and_each_hop_checked():
    hops = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        if b"redirected" not in request.url.query:
            return httpx.Response(
                302, headers={"location": "/r/python/comments/abc123/.rss?redirected=1"}
            )
        return httpx.Response(200, text=reddit_atom([comment("c1", "a", "ok")]))

    out = await make_tool(handler).run({"url": POST})
    # every hop is re-validated AND re-pinned to the resolved address
    assert len(hops) == 2 and all(h.startswith("https://93.184.216.34") for h in hops)
    assert "ok" in out


async def test_connection_pinned_to_validated_ip():
    """DNS-rebinding guard: connect to the IP that passed the check, hostname
    preserved as Host header + TLS SNI."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url_host"] = request.url.host
        seen["host_header"] = request.headers["host"]
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, text=reddit_atom([comment("c1", "a", "ok")]))

    await make_tool(handler).run({"url": POST})
    assert seen["url_host"] == "93.184.216.34"
    assert seen["host_header"] == "www.reddit.com"
    assert seen["sni"] == "www.reddit.com"


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
    out = await make_tool(feed_handler([comment("c1", "a", "ok")])).run({"url": POST})
    assert captured["trust_env"] is False
    assert "ok" in out


async def test_browser_user_agent_sent():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text=reddit_atom([comment("c1", "a", "ok")]))

    await make_tool(handler).run({"url": POST})
    assert "Mozilla" in seen["ua"] and "python" not in seen["ua"].lower()


async def test_feedparser_gets_bytes_never_the_url(monkeypatch):
    """feedparser re-fetches URLs itself, past the SSRF guard — only guarded
    bytes may ever reach it (gotcha: feedparser-self-fetch)."""
    from harness.tools import reddit_comments as mod

    seen: list[type] = []
    real_parse = mod.feedparser.parse

    def spy(value, *args, **kwargs):
        seen.append(type(value))
        return real_parse(value, *args, **kwargs)

    monkeypatch.setattr(mod.feedparser, "parse", spy)
    out = await make_tool(feed_handler([comment("c1", "a", "ok")])).run({"url": POST})
    assert "ok" in out
    assert seen and all(t is bytes for t in seen), seen


# ---- classification + tool group wiring ----------------------------------------------


def test_classification_read_only_and_remote():
    tool = RedditCommentsTool()
    assert tool.classification is ActionClass.READ_ONLY
    assert tool.remote is True


def test_grouped_into_feeds():
    assert tool_groups.group_of(RedditCommentsTool()) == "feeds"


# ---- BK-037: relay URLs — the domain pre-gate at entry, the choke on the hop -------

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

# One fixture shape per attested host spelling (surface map §6: exactly two),
# dialed verbatim — the attested shapes are the shapes proven blocked.
def _one_url_per_attested_spelling() -> list[str]:
    seen: set[str] = set()
    urls = []
    for shape in _RELAY_CORPUS["blocked"]:
        if shape["host_spelling"] not in seen:
            seen.add(shape["host_spelling"])
            urls.append(shape["url"])
    return urls


_RELAY_URLS = _one_url_per_attested_spelling()
_RELAY_URL = _RELAY_URLS[0]  # public bucket.oss-region. spelling


async def test_relay_url_as_argument_stops_at_the_domain_pre_gate():
    """(a) BK-037 surface map §5: run() validates the argument's host and
    REBUILDS the fetch URL before any client exists (:149-151 + _feed_url
    :188), so a relay URL as `url` returns the not-a-Reddit-URL string and can
    NEVER reach check_target — the entry path to the choke is unreachable,
    deliberately: the rebuild guarantees every check_target input names
    www.reddit.com. Pinning that pre-gate (with zero requests) documents the
    unreachability; do NOT reorder the host validation to put it behind
    check_target — it would buy nothing against a rebuilt URL that never
    names another host.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-pregate] a relay argument reached the "
            f"transport: {request.url} — the host gate + URL rebuild is what "
            "keeps relay URLs away from this tool's client entirely"
        )

    tool = make_tool(handler)
    for url in _RELAY_URLS:
        out = await tool.run({"url": url})
        assert out.startswith("reddit_comments error:"), url
        assert "is not a Reddit post URL" in out, (
            f"{url!r} did not stop at the domain pre-gate: {out!r} — this pin "
            "requires the host validation + URL rebuild to stay AHEAD of "
            "check_target (plan step 3: do not reorder); if the relay "
            "directive now appears, the pre-gate was moved behind the choke"
        )
        assert _RELAY_DIRECTIVE not in out  # never reaches the choke here
        assert not isinstance(out, InternalRefusalNote)
    assert requests == []


async def test_redirect_to_relay_url_refused_at_the_hop_with_directive():
    """(b) The relay test that IS realizable for this surface: a clean post
    URL that answers 302 with a corpus relay URL as Location. The manual
    redirect loop re-runs check_target on every hop (:216-217), and the
    family block fires there — the directive verbatim in the tool error, the
    relay host never fetched and never even resolved (D2), and no taint."""
    requests: list[str] = []
    resolutions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.headers["host"] == "www.reddit.com":
            return httpx.Response(302, headers={"location": _RELAY_URL})
        raise AssertionError(
            f"GOTCHA REGRESSION [bk-037-hop] relay hop fetched: {request.url} "
            "— _fetch must re-run check_target on every redirect target"
        )

    async def recording_resolver(host: str) -> list[str]:
        resolutions.append(host)
        return ["93.184.216.34"]

    tool = RedditCommentsTool(
        transport=httpx.MockTransport(handler), resolver=recording_resolver
    )
    out = await tool.run({"url": POST})
    assert out == f"reddit_comments error: {_RELAY_DIRECTIVE}"
    assert not isinstance(out, InternalRefusalNote)  # confabulation never taints
    assert len(requests) == 1 and "aliyuncs" not in requests[0]  # entry hop only
    assert resolutions == ["www.reddit.com"]  # relay host never resolved
