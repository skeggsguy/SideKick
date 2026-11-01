"""web_fetch: read one URL and return its text content.

Companion to web_search — search gives ranked snippets, fetch reads a chosen
page in full. Two hard rules shape this module:

- Fetched pages are external content (harness ingestion rule): output carries
  an untrusted-data prefix and is never instructions.
- The daemon sits on a tailnet with a localhost web UI, so a prompt-injected
  fetch of an internal address is a real exfiltration path. The SSRF guard
  (per-hop re-resolve + IP-literal pinning, manual redirects, trust_env=False)
  lives in _netguard.py — see its docstring for the rationale.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from ..core.policy import ActionClass
from ._extract import (
    UNTRUSTED_NOTE,
    detect_challenge,
    html_to_text,
    render_page,
)
from ._netguard import (
    InternalRefusalNote,
    Refused,
    RefusedInternal,
    Resolver,
    check_target,
    default_resolver,
    pin,
)
from .base import Tool

# Re-exported for callers/tests that import it from here.
__all__ = ["WebFetchTool", "html_to_text", "UNTRUSTED_NOTE"]

_DEFAULT_CHARS = 15_000
_MAX_CHARS = 40_000
_MAX_BODY_BYTES = 2_000_000
_MAX_REDIRECTS = 5
_TEXT_TYPES = (
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
)
# Many sites bot-block httpx's default python UA (Google serves a block page,
# DuckDuckGo answers HTTP 202) — present as an ordinary browser.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


class WebFetchTool(Tool):
    name = "web_fetch"
    description = (
        "Fetch one http(s) URL and return its readable text — after web_search, "
        "or when the user gives a URL. Not a search engine: never fetch "
        "search-result pages or guess URLs. Public web only. Listing page "
        "missing entries: retry with extract='full'. JavaScript-rendered, "
        "bot-blocked or empty: use browser_fetch (browser group); to act on a "
        "site (book, fill a form), browser_delegate."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {
                "type": "integer",
                "description": f"Default {_DEFAULT_CHARS}, max {_MAX_CHARS}.",
            },
            "extract": {
                "type": "string",
                "description": "auto (default): text + embedded data; text; "
                "structured: embedded data only; full: all page text incl. listings.",
                "enum": ["auto", "text", "structured", "full"],
            },
        },
        "required": ["url"],
    }
    classification = ActionClass.READ_ONLY
    remote = True

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver = default_resolver,
    ) -> None:
        self._transport = transport
        self._resolve = resolver

    async def run(self, args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        max_chars = min(max(int(args.get("max_chars", _DEFAULT_CHARS)), 100), _MAX_CHARS)
        mode = str(args.get("extract", "auto")).strip().lower()
        if mode not in ("auto", "text", "structured", "full"):
            mode = "auto"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                transport=self._transport,
                headers=_HEADERS,
                follow_redirects=False,  # each hop is re-validated below
                trust_env=False,  # env proxies would bypass the IP validation
            ) as client:
                resp, raw, final_url = await self._fetch(client, url)
        except RefusedInternal as e:
            # An internal target (entry URL or any redirect hop) is an
            # attack-in-progress signal — the typed note taints the turn.
            return InternalRefusalNote(f"web_fetch error: {e}")
        except Refused as e:
            return f"web_fetch error: {e}"
        except httpx.TimeoutException:
            return f"web_fetch error: {url} did not respond within 15s."
        except httpx.HTTPError as e:
            return f"web_fetch error: could not reach {url} ({type(e).__name__})."

        body = raw.decode(resp.charset_encoding or "utf-8", errors="replace")
        # Challenge pages arrive as 403/503 — detect BEFORE the status check
        # or they read as plain HTTP errors.
        vendor = detect_challenge(resp.status_code, resp.headers, body)
        if vendor:
            host = urlsplit(final_url).hostname or final_url
            return (
                f"web_fetch error: {host} is behind a bot-protection challenge "
                f"({vendor}) — this page cannot be fetched; use web_search "
                "snippets or newsletter email instead."
            )
        if resp.status_code != 200:
            return f"web_fetch error: {final_url} returned HTTP {resp.status_code}."

        ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype in ("text/html", "application/xhtml+xml"):
            return await render_page(body, final_url, max_chars, mode)
        if ctype.startswith("text/") or ctype in _TEXT_TYPES:
            header, text = final_url, body
        else:
            return (
                f"web_fetch error: {final_url} is {ctype or 'unknown type'} — "
                "only text content is supported."
            )

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]
        note = f"\n[truncated at {max_chars} chars]" if truncated else ""
        return f"{UNTRUSTED_NOTE}\n{header}\n\n{text}{note}"

    async def _fetch(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[httpx.Response, bytes, str]:
        for _ in range(_MAX_REDIRECTS + 1):
            ip, host = await check_target(url, self._resolve)
            pinned, headers, extensions = pin(url, ip, host)
            async with client.stream(
                "GET", pinned, headers=headers, extensions=extensions
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise Refused(f"{url} redirected with no Location header.")
                    url = urljoin(url, location)
                    continue
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    chunks.append(chunk)
                    if size >= _MAX_BODY_BYTES:
                        break
                return resp, b"".join(chunks), url
        raise Refused(f"too many redirects (>{_MAX_REDIRECTS}).")
