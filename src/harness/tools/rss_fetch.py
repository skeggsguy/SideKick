"""rss_fetch: read one RSS/Atom feed URL and return its recent items.

The feed-reading companion to web_fetch — where web_fetch returns a page's
readable text, this returns a feed's items (title, link, author, date,
description excerpt) as a compact markdown list. Two hard rules carry over
from web_fetch:

- Feed content is external content (harness ingestion rule): output carries
  an untrusted-data prefix and is never instructions.
- The SSRF guard (per-hop re-resolve + IP-literal pinning, manual redirects,
  trust_env=False) lives in _netguard.py — see its docstring for the
  rationale. feedparser can also fetch URLs itself; NEVER hand it one — only
  already-guarded bytes, or it re-fetches past the checks.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from ..core.policy import ActionClass
from ._extract import UNTRUSTED_NOTE
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
from .web_fetch import _HEADERS  # shared browser UA — feeds bot-block python UAs too

__all__ = ["RssFetchTool"]

_DEFAULT_ITEMS = 10
_MAX_ITEMS = 25
_DEFAULT_ITEM_CHARS = 2_000
_MAX_ITEM_CHARS = 6_000
_MAX_BODY_BYTES = 2_000_000
_MAX_REDIRECTS = 5
_TAG_RE = re.compile(r"<[^>]+>")


def _snippet(value: str) -> str:
    """Feed titles/descriptions carry entity-encoded HTML — strip tags,
    unescape entities, collapse whitespace to one compact line."""
    text = html_mod.unescape(_TAG_RE.sub(" ", value))
    return re.sub(r"\s+", " ", text).strip()


def _date(entry: Any) -> str:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return time.strftime("%Y-%m-%d", parsed)
            except (TypeError, ValueError):
                pass
    for key in ("published", "updated"):
        raw = entry.get(key)
        if raw:
            return _snippet(str(raw))[:32]
    return ""


def _author(entry: Any) -> str:
    detail = entry.get("author_detail") or {}
    name = detail.get("name") or detail.get("email")
    if name:
        return _snippet(str(name))
    raw = entry.get("author")
    return _snippet(str(raw)) if raw else ""


class RssFetchTool(Tool):
    name = "rss_fetch"
    description = (
        "Fetch an RSS or Atom feed URL and return its recent items: title, "
        "link, author, date and a short description each. Use for blogs, news "
        "sites, YouTube channels and any site with a feed (e.g. a subreddit's "
        ".rss URL). For comment threads under a Reddit or Hacker News post use "
        "reddit_comments / hn_comments; for an ordinary web page use "
        "web_fetch. Public feeds only."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The http(s) URL of the RSS/Atom feed.",
            },
            "max_items": {
                "type": "integer",
                "description": f"Return at most this many items, newest first "
                f"(default {_DEFAULT_ITEMS}, max {_MAX_ITEMS}).",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Truncate each item's description to this many "
                f"characters (default {_DEFAULT_ITEM_CHARS}, max {_MAX_ITEM_CHARS}).",
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
        max_items = _clamped(args.get("max_items"), _DEFAULT_ITEMS, 1, _MAX_ITEMS)
        item_chars = _clamped(args.get("max_chars"), _DEFAULT_ITEM_CHARS, 100, _MAX_ITEM_CHARS)
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
            return InternalRefusalNote(f"rss_fetch error: {e}")
        except Refused as e:
            return f"rss_fetch error: {e}"
        except httpx.TimeoutException:
            return f"rss_fetch error: {url} did not respond within 15s."
        except httpx.HTTPError as e:
            return f"rss_fetch error: could not reach {url} ({type(e).__name__})."

        if resp.status_code != 200:
            return f"rss_fetch error: {final_url} returned HTTP {resp.status_code}."

        # Bytes, never the URL (module docstring); sync XML parse off the loop.
        parsed = await asyncio.to_thread(feedparser.parse, raw)
        return self._format(parsed, final_url, max_items, item_chars)

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

    def _format(
        self, parsed: Any, final_url: str, max_items: int, item_chars: int
    ) -> str:
        host = urlsplit(final_url).hostname or final_url
        if not parsed.entries and (parsed.bozo or not parsed.version):
            if parsed.version:  # looked like feed XML but broke mid-parse
                return (
                    f"rss_fetch error: the feed at {host} is malformed XML — "
                    "no items could be parsed."
                )
            return (
                f"rss_fetch error: {host} is not an RSS/Atom feed "
                "(the response didn't parse as feed XML)."
            )

        feed_title = _snippet(str(parsed.feed.get("title") or "")) or host
        feed_link = parsed.feed.get("link")
        header = f"{feed_title} — {feed_link}" if feed_link else feed_title
        if not parsed.entries:
            return f"{UNTRUSTED_NOTE}\n{header}\n\nThe feed has no items right now."

        items = parsed.entries[:max_items]
        blocks: list[str] = []
        for i, entry in enumerate(items, 1):
            title = _snippet(str(entry.get("title") or "")) or "(untitled)"
            lines = [f"{i}. **{title}**"]
            link = entry.get("link")
            if link:
                lines.append(f"   {link}")
            meta = " · ".join(x for x in (_author(entry), _date(entry)) if x)
            if meta:
                lines.append(f"   {meta}")
            desc = _snippet(str(entry.get("summary") or entry.get("description") or ""))
            if desc:
                if len(desc) > item_chars:
                    desc = desc[:item_chars].rstrip() + "… [truncated]"
                lines.append(f"   {desc}")
            blocks.append("\n".join(lines))

        out = f"{UNTRUSTED_NOTE}\n{header}\n\n" + "\n\n".join(blocks)
        hidden = len(parsed.entries) - len(items)
        if hidden > 0:
            out += f"\n\n[{hidden} more items in this feed not shown]"
        if parsed.bozo:  # lenient parse: items survived but the XML broke
            out += "\n[note: the feed XML had errors — later items may be missing]"
        return out


def _clamped(value: Any, default: int, lo: int, hi: int) -> int:
    """int() the arg into [lo, hi], falling back to the default on garbage —
    a malformed optional arg must not crash the tool or the turn."""
    try:
        return min(max(int(value), lo), hi)
    except (TypeError, ValueError):
        return default
