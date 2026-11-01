"""reddit_comments: read the comments of one Reddit post.

The Reddit arm of the feeds group (rss_fetch is the feed reader, hn_comments
the Hacker News arm). Takes the post URL the model already has (from
rss_fetch or web_search), extracts the subreddit + post id, and fetches the
post's public Atom feed — the request always goes to www.reddit.com, never
to whatever host the argument names.

Why the .rss feed and not the .json endpoint: since 2026-10 Reddit answers
HTTP 403 (a policy-block page) for unauthenticated .json from non-browser
TLS fingerprints no matter which User-Agent or browser header set is sent,
and old.reddit.com bounces unauthenticated readers to a login wall. The
Atom feeds still serve. Consequences: no scores (so no client-side ranking —
sort=top lets Reddit rank), replies arrive flattened as their own entries,
and the feeds are throttled hard (429 is routine — see the retry hint).

Two hard rules carry over from rss_fetch/web_fetch:

- Comments are external content: output carries the untrusted-data prefix
  and is never instructions.
- The SSRF guard (per-hop re-resolve + IP-literal pinning, manual redirects,
  trust_env=False) lives in _netguard.py — see its docstring for the why.
  feedparser can fetch URLs itself; NEVER hand it one — only already-guarded
  bytes, or it re-fetches past the checks. The domain check below is a
  second, earlier wall: a non-Reddit URL never reaches the network at all.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import feedparser
import httpx
import re

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
from .rss_fetch import _author, _clamped, _snippet
from .web_fetch import _HEADERS

__all__ = ["RedditCommentsTool"]

_DEFAULT_COMMENTS = 5
_MAX_COMMENTS = 10
_DEFAULT_COMMENT_CHARS = 300
_MIN_COMMENT_CHARS = 40
_MAX_COMMENT_CHARS = 2_000
_MAX_BODY_BYTES = 4_000_000  # a busy thread's feed is bigger than a blog's
_MAX_REDIRECTS = 5

# /r/<sub>/comments/<id>/ — "user/<name>" counts as the subreddit segment for
# profile-post URLs. Capture keeps the path prefix so both forms rebuild.
_POST_RE = re.compile(r"(/(?:r|user)/[^/]+/comments/[A-Za-z0-9]+)")

_URL_HELP = (
    "expected a Reddit post URL like "
    "https://www.reddit.com/r/<sub>/comments/<id>/<slug>/"
)


def _is_reddit_host(host: str | None) -> bool:
    """Exact-domain match — evil-reddit.com and reddit.com.evil.test fail."""
    if not host:
        return False
    host = host.lower().rstrip(".")
    return host == "reddit.com" or host.endswith(".reddit.com")


def _entry_body(entry: Any) -> str:
    """Comment HTML lives in atom <content type=html>; feedparser decodes the
    entity-escaping, _snippet strips the markup."""
    content = entry.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
        return str(content[0].get("value") or "")
    return str(entry.get("summary") or entry.get("description") or "")


class RedditCommentsTool(Tool):
    name = "reddit_comments"
    description = (
        "Fetch the comments of a Reddit post, given the post URL (e.g. from "
        "rss_fetch or web_search). Returns author and body per comment in "
        "Reddit's top-rated order, with replies flattened in; the public feed "
        "carries no scores and Reddit throttles it, so expect HTTP 429 if "
        "used repeatedly. Reddit post URLs only (reddit.com/r/.../comments/"
        "...); for other sites' comment pages use web_fetch."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "The Reddit post URL, e.g. "
                    "https://www.reddit.com/r/python/comments/abc123/title/."
                ),
            },
            "max_comments": {
                "type": "integer",
                "description": f"Return at most this many comments, top-rated "
                f"first (default {_DEFAULT_COMMENTS}, max {_MAX_COMMENTS}).",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Truncate each comment body to this many "
                f"characters (default {_DEFAULT_COMMENT_CHARS}, "
                f"max {_MAX_COMMENT_CHARS}).",
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
        max_comments = _clamped(
            args.get("max_comments"), _DEFAULT_COMMENTS, 1, _MAX_COMMENTS
        )
        comment_chars = _clamped(
            args.get("max_chars"),
            _DEFAULT_COMMENT_CHARS,
            _MIN_COMMENT_CHARS,
            _MAX_COMMENT_CHARS,
        )

        # Validate the domain BEFORE any network I/O, then rebuild the URL
        # from validated parts so the fetch can never name another host.
        feed_url = self._feed_url(url, max_comments)
        if feed_url is None:
            return f"reddit_comments error: {url!r} is not a Reddit post URL — {_URL_HELP}"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                transport=self._transport,
                headers=_HEADERS,
                follow_redirects=False,  # each hop is re-validated below
                trust_env=False,  # env proxies would bypass the IP validation
            ) as client:
                resp, raw, final_url = await self._fetch(client, feed_url)
        except RefusedInternal as e:
            # An internal target (rebound DNS or a redirect hop) is an
            # attack-in-progress signal — the typed note taints the turn.
            return InternalRefusalNote(f"reddit_comments error: {e}")
        except Refused as e:
            return f"reddit_comments error: {e}"
        except httpx.TimeoutException:
            return "reddit_comments error: Reddit did not respond within 15s."
        except httpx.HTTPError as e:
            return f"reddit_comments error: could not reach Reddit ({type(e).__name__})."

        if resp.status_code == 429:
            return (
                "reddit_comments error: Reddit rate-limited the request "
                "(HTTP 429) — unauthenticated feeds allow only a handful of "
                "requests per minute; wait at least a minute before retrying."
            )
        if resp.status_code != 200:
            host = urlsplit(final_url).hostname or final_url
            return f"reddit_comments error: {host} returned HTTP {resp.status_code}."

        # Bytes, never the URL (module docstring); sync XML parse off the loop.
        parsed = await asyncio.to_thread(feedparser.parse, raw)
        return self._format(parsed, max_comments, comment_chars)

    @staticmethod
    def _feed_url(post_url: str, max_comments: int) -> str | None:
        """Rebuild the post's Atom feed URL, or None if the URL is not a
        Reddit post URL. Host, subreddit and id come from validated regex
        groups; the request always goes to www.reddit.com. limit gets a
        small buffer because deleted/empty entries are dropped after fetch,
        and sort=top lets Reddit do the ranking (the feed has no scores)."""
        parts = urlsplit(post_url)
        if parts.scheme not in ("http", "https") or not _is_reddit_host(parts.hostname):
            return None
        match = _POST_RE.search(parts.path)
        if match is None:
            return None
        path = match.group(1)
        # id is the last path segment; quote the subreddit/user segment so
        # nothing odd survives into the request path.
        prefix, _, post_id = path.rpartition("/")
        safe_prefix = "/".join(
            seg if i < 2 else quote(seg, safe="") for i, seg in enumerate(prefix.split("/"))
        )
        limit = min(max_comments + 5, _MAX_COMMENTS + 5)
        return (
            f"https://www.reddit.com{safe_prefix}/{post_id}"
            f"/.rss?sort=top&limit={limit}"
        )

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

    def _format(self, parsed: Any, max_comments: int, comment_chars: int) -> str:
        if not parsed.entries and (parsed.bozo or not parsed.version):
            # An HTML block page, truncated XML, or any non-feed body.
            return (
                "reddit_comments error: could not parse Reddit's response as "
                "a comment feed (Reddit may be serving a block or error page)."
            )

        header = self._header(parsed)
        comments: list[tuple[str, str]] = []
        for entry in parsed.entries:
            entry_id = str(entry.get("id") or "")
            if not entry_id.startswith("t1_"):
                continue  # the t3_ submission entry and anything unknown
            author = _author(entry)
            if author.startswith("/u/"):
                author = author[3:]
            body = _snippet(_entry_body(entry))  # comment HTML → one compact line
            if not body or body in ("[deleted]", "[removed]"):
                continue
            if author in ("[deleted]", "[unknown]"):
                continue
            comments.append((author or "unknown", body))

        if not comments:
            return f"{UNTRUSTED_NOTE}\n{header}\n\nThe post has no comments right now."

        shown = comments[:max_comments]  # Reddit's sort=top order is kept
        blocks: list[str] = []
        for i, (author, body) in enumerate(shown, 1):
            if len(body) > comment_chars:
                body = body[:comment_chars].rstrip() + "… [truncated]"
            blocks.append(f"{i}. u/{author}:\n   {body}")

        out = f"{UNTRUSTED_NOTE}\n{header}\n\n" + "\n\n".join(blocks)
        hidden = len(comments) - len(shown)
        if hidden > 0:
            out += f"\n\n[{hidden} more comments fetched but not shown]"
        if parsed.bozo:  # lenient parse: comments survived but the XML broke
            out += "\n[note: the feed XML had errors — later comments may be missing]"
        return out

    @staticmethod
    def _header(parsed: Any) -> str:
        """Post title + subreddit. The t3_ entry carries both; the feed-level
        title/category are the fallbacks."""
        title = ""
        sub = ""
        candidates = [parsed.feed] + [
            e for e in parsed.entries if str(e.get("id") or "").startswith("t3_")
        ]
        for node in candidates:
            title = title or _snippet(str(node.get("title") or ""))
            tags = node.get("tags")
            if not sub and isinstance(tags, list) and tags and isinstance(tags[0], dict):
                sub = str(tags[0].get("label") or tags[0].get("term") or "")
        title = title or "Reddit post"
        line = f'Top comments on "{title}"'
        return f"{line} ({sub}):" if sub else f"{line}:"
