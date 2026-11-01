"""hn_comments: read the top comment thread of one Hacker News post.

The Hacker News arm of the feeds group (rss_fetch is the feed reader,
reddit_comments the Reddit arm). Takes a news.ycombinator.com/item?id=...
URL, extracts the numeric item id, and walks the Firebase API: the story's
``kids`` in HN's own ranking order, replies one level deep. Every request
goes to hacker-news.firebaseio.com — the argument's host is validated
first and then never used, so a forged URL cannot point this tool anywhere.
Same two hard rules as the sibling tools (untrusted-data prefix on output,
_netguard SSRF guard on every hop).

Fetch budget: the kid scan stops at max_comments live comments after at
most ``_MAX_KIDS_SCAN`` lookups, and each comment shows at most
``_MAX_REPLIES`` replies at depth 2 — deeper threads are not fetched, which
keeps a comment pull at a bounded number of sequential round-trips.
"""

from __future__ import annotations

import html as html_mod
import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

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
from .rss_fetch import _clamped, _snippet
from .web_fetch import _HEADERS

__all__ = ["HnCommentsTool"]

_DEFAULT_COMMENTS = 5
_MAX_COMMENTS = 10
_DEFAULT_COMMENT_CHARS = 300
_MIN_COMMENT_CHARS = 40
_MAX_COMMENT_CHARS = 2_000
_MAX_KIDS_SCAN = 20  # kid lookups spent before the live-comment quota fills
_MAX_REPLIES = 3  # replies shown per top-level comment (depth 2, never more)
_MAX_BODY_BYTES = 1_000_000
_MAX_REDIRECTS = 5

_API = "https://hacker-news.firebaseio.com/v0/item/{}.json"

_URL_HELP = "expected a Hacker News post URL like https://news.ycombinator.com/item?id=<digits>"

_TAG_RE = re.compile(r"<[^>]+>")


def _is_hn_host(host: str | None) -> bool:
    return (host or "").lower().rstrip(".") == "news.ycombinator.com"


def _item_id(post_url: str) -> str | None:
    """The numeric HN item id from a news.ycombinator.com/item?id=... URL, or
    None when the URL is not an HN story link (checked before any fetch)."""
    parts = urlsplit(post_url)
    if parts.scheme not in ("http", "https") or not _is_hn_host(parts.hostname):
        return None
    if parts.path != "/item":
        return None
    match = re.search(r"(?:^|&)id=(\d+)(?:&|$)", parts.query)
    return match.group(1) if match else None


def _comment_text(raw: str) -> str:
    """Firebase comment text is HTML: paragraphs are bare <p> tags, links and
    emphasis are inline. Strip tags, keep paragraph breaks, collapse runs of
    whitespace within each line."""
    text = html_mod.unescape(_TAG_RE.sub("", raw.replace("<p>", "\n")))
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _truncated(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "… [truncated]"


class HnCommentsTool(Tool):
    name = "hn_comments"
    description = (
        "Fetch the top comments of a Hacker News post, given the post URL "
        "(e.g. from rss_fetch or web_search). Returns the thread as readable "
        "text, HN's own ranking order, replies nested under their comment, "
        "deleted and dead items skipped. HN post URLs only "
        "(news.ycombinator.com/item?id=...); for other sites' comment pages "
        "use web_fetch."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "The HN post URL, e.g. "
                    "https://news.ycombinator.com/item?id=1234567."
                ),
            },
            "max_comments": {
                "type": "integer",
                "description": f"Return at most this many top-level comments "
                f"(default {_DEFAULT_COMMENTS}, max {_MAX_COMMENTS}).",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Truncate each comment to this many "
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

        item_id = _item_id(url)
        if item_id is None:
            return f"hn_comments error: {url!r} is not a Hacker News post URL — {_URL_HELP}"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                transport=self._transport,
                headers=_HEADERS,
                follow_redirects=False,  # each hop is re-validated below
                trust_env=False,  # env proxies would bypass the IP validation
            ) as client:
                status, story = await self._get_json(client, _API.format(item_id))
                if status is None:
                    return (
                        "hn_comments error: could not parse HN's JSON response "
                        f"for item {item_id}."
                    )
                if status == 404:
                    return (
                        f"hn_comments error: HN has no item {item_id} — "
                        "check the post URL."
                    )
                if status != 200:
                    return f"hn_comments error: HN returned HTTP {status}."
                if story is None:  # Firebase answers a missing item with null
                    return (
                        f"hn_comments error: HN has no item {item_id} — "
                        "check the post URL."
                    )
                if not isinstance(story, dict):
                    return "hn_comments error: unexpected HN API response shape."

                # HN's kid order IS the ranking. Sequential fetches, stop at
                # the live-comment quota; replies come one level deeper.
                thread: list[tuple[dict, list[dict]]] = []
                for kid in (story.get("kids") or [])[:_MAX_KIDS_SCAN]:
                    if len(thread) >= max_comments:
                        break
                    comment = await self._get_live_comment(client, kid)
                    if comment is None:
                        continue
                    replies = []
                    for reply_id in (comment.get("kids") or [])[:_MAX_REPLIES]:
                        reply = await self._get_live_comment(client, reply_id)
                        if reply is not None:
                            replies.append(reply)
                    thread.append((comment, replies))
        except RefusedInternal as e:
            # An internal target (rebound DNS or a redirect hop) is an
            # attack-in-progress signal — the typed note taints the turn.
            return InternalRefusalNote(f"hn_comments error: {e}")
        except Refused as e:
            return f"hn_comments error: {e}"
        except httpx.TimeoutException:
            return "hn_comments error: HN did not respond within 15s."
        except httpx.HTTPError as e:
            return f"hn_comments error: could not reach HN ({type(e).__name__})."

        return self._format(story, item_id, thread, comment_chars)

    async def _get_live_comment(
        self, client: httpx.AsyncClient, item_id: Any
    ) -> dict | None:
        """A fetchable, live comment — None for anything to skip: a failed or
        malformed fetch, a deleted/dead item, or a non-comment."""
        if not isinstance(item_id, (int, str)):
            return None
        status, data = await self._get_json(client, _API.format(item_id))
        if status != 200 or not isinstance(data, dict):
            return None
        if data.get("deleted") or data.get("dead") or data.get("by") == "deleted":
            return None
        if data.get("type") not in (None, "comment"):
            return None
        if not isinstance(data.get("text"), str) or not data["text"].strip():
            return None
        return data

    async def _get_json(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[int | None, Any]:
        """(status, parsed_json) for one guarded fetch. Status None means the
        body never arrived parseable; Firebase answers a missing item with a
        literal ``null`` (200, None)."""
        for _ in range(_MAX_REDIRECTS + 1):
            ip, host = await check_target(url, self._resolve)
            pinned, headers, extensions = pin(url, ip, host)
            resp = await client.get(pinned, headers=headers, extensions=extensions)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    raise Refused(f"{url} redirected with no Location header.")
                url = urljoin(url, location)
                continue
            if resp.status_code != 200:
                return resp.status_code, None
            if len(resp.content) > _MAX_BODY_BYTES:
                return None, None
            try:
                return 200, json.loads(resp.content.decode("utf-8", errors="replace"))
            except (json.JSONDecodeError, ValueError):
                return None, None
        raise Refused(f"too many redirects (>{_MAX_REDIRECTS}).")

    def _format(
        self,
        story: dict,
        item_id: str,
        thread: list[tuple[dict, list[dict]]],
        comment_chars: int,
    ) -> str:
        title = _snippet(str(story.get("title") or "")) or f"HN item {item_id}"
        meta = [f"https://news.ycombinator.com/item?id={item_id}"]
        if story.get("score") is not None:
            meta.append(f"{story['score']} points")
        if story.get("by"):
            meta.append(f"by {story['by']}")
        header = f'Top comments on "{title}" ({", ".join(meta)}):'

        if not thread:
            return f"{UNTRUSTED_NOTE}\n{header}\n\nThe post has no comments right now."

        blocks: list[str] = []
        for i, (comment, replies) in enumerate(thread, 1):
            by = str(comment.get("by") or "unknown")
            body = _truncated(_comment_text(comment["text"]), comment_chars)
            lines = [f"{i}. **{by}**:", *(f"   {ln}" for ln in body.splitlines())]
            for reply in replies:
                reply_text = _truncated(
                    re.sub(r"\s+", " ", _comment_text(reply["text"])).strip(),
                    comment_chars,
                )
                lines.append(f"  > **{reply.get('by') or 'unknown'}**: {reply_text}")
            blocks.append("\n".join(lines))
        return f"{UNTRUSTED_NOTE}\n{header}\n\n" + "\n\n".join(blocks)
