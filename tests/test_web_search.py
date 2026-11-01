"""web_search tool — offline, Brave responses served by httpx.MockTransport."""

from __future__ import annotations

import json

import httpx

from harness.core.policy import ActionClass
from harness.tools.web_search import UNTRUSTED_NOTE, WebSearchTool


def brave_payload(n: int) -> dict:
    return {
        "web": {
            "results": [
                {
                    "title": f"Result {i}",
                    "url": f"https://example.com/{i}",
                    "description": f"Snippet {i}",
                }
                for i in range(1, n + 1)
            ]
        }
    }


def make_tool(handler) -> WebSearchTool:
    return WebSearchTool("test-key", transport=httpx.MockTransport(handler))


async def test_results_formatted_and_labeled_untrusted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Subscription-Token"] == "test-key"
        assert request.url.params["q"] == "claude releases"
        return httpx.Response(200, json=brave_payload(3))

    out = await make_tool(handler).run({"query": "claude releases", "count": 3})
    assert out.startswith(UNTRUSTED_NOTE)
    assert "1. Result 1" in out and "3. Result 3" in out
    assert "https://example.com/2" in out and "Snippet 2" in out


async def test_count_clamped_to_ten():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["count"] = request.url.params["count"]
        return httpx.Response(200, json=brave_payload(2))

    await make_tool(handler).run({"query": "x", "count": 50})
    assert seen["count"] == "10"


async def test_empty_results_and_missing_query():
    tool = make_tool(lambda req: httpx.Response(200, json={"web": {"results": []}}))
    assert "No web results" in await tool.run({"query": "zzz"})
    assert "required" in await tool.run({})


async def test_401_names_the_env_var_without_echoing_key():
    out = await make_tool(lambda req: httpx.Response(401)).run({"query": "x"})
    assert "BRAVE_SEARCH_API_KEY" in out
    assert "test-key" not in out


async def test_429_retries_once_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=brave_payload(1))

    out = await make_tool(handler).run({"query": "x"})
    assert len(calls) == 2
    assert "Result 1" in out


async def test_429_twice_reports_rate_limit():
    handler = lambda req: httpx.Response(429, headers={"Retry-After": "0"})  # noqa: E731
    out = await make_tool(handler).run({"query": "x"})
    assert "rate limit" in out


async def test_timeout_returns_message_not_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    out = await make_tool(handler).run({"query": "x"})
    assert "did not respond" in out


async def test_snippet_html_markup_stripped():
    payload = {
        "web": {
            "results": [
                {
                    "title": "T",
                    "url": "https://e.com",
                    "description": "the <strong>Claude</strong> model",
                }
            ]
        }
    }
    out = await make_tool(lambda req: httpx.Response(200, json=payload)).run(
        {"query": "x"}
    )
    assert "the Claude model" in out and "<strong>" not in out


async def test_long_snippets_truncated():
    payload = {
        "web": {
            "results": [
                {"title": "T", "url": "https://e.com", "description": "y" * 1000}
            ]
        }
    }
    out = await make_tool(lambda req: httpx.Response(200, json=payload)).run(
        {"query": "x"}
    )
    assert "y" * 301 not in out and "…" in out


def test_classification_read_only_and_remote():
    tool = WebSearchTool("k")
    assert tool.classification is ActionClass.READ_ONLY
    assert tool.remote is True
    json.dumps(tool.input_schema)  # schema must be JSON-serializable


async def test_client_ignores_proxy_env(monkeypatch):
    """trust_env=False: env proxies must not reroute Brave API traffic."""
    captured = {}
    real_client = httpx.AsyncClient

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    tool = make_tool(lambda req: httpx.Response(200, json=brave_payload(1)))
    out = await tool.run({"query": "anything"})
    assert captured["trust_env"] is False
    assert "Result 1" in out


def test_result_urls_round_trip_into_ledger():
    """Pins the format↔extractor coupling: EgressLedger.seed_search_results
    must admit exactly the result URLs' hosts from _format_results output —
    not titles, not snippet prose (which could carry attacker text)."""
    from harness.core.egress import EgressLedger
    from harness.tools.web_search import _format_results

    results = [
        {
            "title": "Evil title naming evilti.tle",
            "url": "https://found.test/page",
            "description": "snippet naming https://snippet-evil.test/x in prose",
        },
        {"title": "Two", "url": "https://second.test/", "description": ""},
    ]
    ledger = EgressLedger()
    ledger.seed_search_results(_format_results(results, "q"))
    assert ledger.allows("https://found.test/other")
    assert ledger.allows("https://second.test/x")
    assert not ledger.allows("https://evilti.tle/")
    assert not ledger.allows("https://snippet-evil.test/")
