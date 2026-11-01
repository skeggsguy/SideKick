"""HTML email body -> readable text, plus the Gmail payload body resolver (BK-003).

An HTML-only email (real-estate agent auto-replies are the shape that prompted
this) has no text/plain part, so the old plain-text-only walk returned "" and
gmail_read fell back to Gmail's ~140-char snippet — the body was effectively
lost. resolve_body() prefers text/plain exactly as before and falls back to
stripping text/html to readable text.

Why a stdlib html.parser here rather than the harness's tools/_extract.py:
the MCP server is a standalone stdio process that never imports from harness
(same deliberate separation as the _paths.py duplicate), and trafilatura is
banned for this job — it is prose-tuned and silently drops the card/grid
content that ad-style emails are built from (learnings 2026-07-28).

The extractor is email-specific (web pages would want nav/footer kept):
- block elements become line breaks; links render as "text (url)";
- quoted replies (blockquote, class/id gmail_quote / ymquote), <style>/<script>
  and display:none wrappers are dropped, not dumped;
- tracking pixels never contribute: <img> has no textual output, and a
  pixel link ("<a href=...><img width=1 height=1></a>") yields empty link
  text, which suppresses the url append;
- bare signature divider lines ("--", "—") are trimmed; the signature's own
  content (an agent's phone number is lead info) is kept.
"""

from __future__ import annotations

import base64
import re
from html.parser import HTMLParser

# Subtrees dropped outright. head carries only title/meta for an email body.
_SKIP_TAGS = {"script", "style", "noscript", "template", "head", "blockquote"}

# Tags that break lines on BOTH open and close vs. OPEN only. td/th break on
# open only so cells of one table row read as separate lines without a blank
# line between each; br must break exactly once whether written <br> or <br/>.
_BREAK_BOTH = {
    "p", "div", "li", "ul", "ol", "table", "tr", "form", "dl", "dt", "dd",
    "pre", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
    "header", "footer", "main", "nav", "aside", "figure", "figcaption",
}  # fmt: skip
_BREAK_START = {"br", "td", "th", "hr"}

# Void elements never receive an end tag, so a skip opened on one would never
# close (and they carry no text children to drop anyway).
_VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}  # fmt: skip

# Gmail quotes the quoted reply in <div class="gmail_quote">, Yahoo in
# "ymquote"; matched as substrings so suffixed variants still hit.
_QUOTE_MARKERS = ("gmail_quote", "ymquote")

# Standalone signature separator lines (RFC 822 "--", the em-dash style).
_DIVIDERS = {"--", "\u2014", "___"}


def _style_is_hidden(style: str) -> bool:
    return "display:none" in re.sub(r"\s+", "", style).lower()


def _is_quote_container(attrs: dict[str, str]) -> bool:
    blob = f"{attrs.get('class', '')} {attrs.get('id', '')}".lower()
    return any(marker in blob for marker in _QUOTE_MARKERS)


class _EmailTextExtractor(HTMLParser):
    """Strip email HTML to readable text. Feed bytes/str, then close(), then text()."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._open: list[str] = []  # every open non-void tag, in order
        self._drop_from: int | None = None  # index in _open where the drop began
        self._links: list[tuple[str, int]] = []  # (href, chunk index at open)

    @property
    def _dropping(self) -> bool:
        return self._drop_from is not None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        adict = {k.lower(): (v or "") for k, v in attrs}
        # Push link state BEFORE the drop check so every <a> has a matching pop;
        # link text stays empty inside a dropped subtree, so nothing leaks out.
        if tag == "a":
            self._links.append((adict.get("href", "").strip(), len(self._chunks)))
        if tag in _VOID:
            if not self._dropping and tag in _BREAK_START:
                self._chunks.append("\n")
            return
        # Track EVERY open tag, not just the drop root: a quote wrapper is full
        # of nested <div>s, and matching end tags by name alone would let the
        # first inner </div> end the drop and leak the rest of the quote.
        self._open.append(tag)
        if self._dropping:
            return
        if (
            tag in _SKIP_TAGS
            or _style_is_hidden(adict.get("style", ""))
            or _is_quote_container(adict)
        ):
            self._drop_from = len(self._open) - 1
            return
        if tag in _BREAK_BOTH or tag in _BREAK_START:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag == "a":
            # Emit BEFORE the stack pop below: a hidden <a> is still inside its
            # own dropped subtree at this point, so _emit_link stays silent.
            if self._links:
                href, start = self._links.pop()
                self._emit_link(href, start)
        try:  # a stray </span> must not desync the open stack
            idx = len(self._open) - 1 - self._open[::-1].index(tag)
        except ValueError:
            return
        del self._open[idx:]
        if self._drop_from is not None and self._drop_from >= len(self._open):
            self._drop_from = None
            return
        if self._dropping:
            return
        if tag in _BREAK_BOTH:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self._chunks.append(data)

    def _emit_link(self, href: str, start: int) -> None:
        """Close one <a>: collapse its label to a single line and append the
        url as " (href)" unless the label already IS the url (or an obvious
        mailto of it) — a bare url printed twice is noise, and empty label
        (the tracking-pixel shape) means print nothing at all."""
        new = self._chunks[start:]
        label = " ".join("".join(new).split())
        if new:
            self._chunks[start:] = [label]
        if not label or self._dropping or not href:
            return
        lowered, label_lower = href.lower(), label.lower()
        # Not destinations: a javascript: handler, an in-page anchor, an inline
        # cid: reference, or a data: blob (which can be tens of KB and would eat
        # MAX_BODY_CHARS, truncating the real body).
        if lowered.startswith(("javascript:", "#", "cid:", "data:")):
            return
        # Label already spells the destination: exact, scheme-stripped (the
        # "www.x/unsub" label on an "https://www.x/unsub" href), or mailto-of.
        bare = lowered.removeprefix("https://").removeprefix("http://")
        if label_lower in (lowered, bare) or lowered == f"mailto:{label_lower}":
            return
        self._chunks.append(f" ({href})")

    def text(self) -> str:
        # Compact lines, no blank ones: an email's blank lines are wrapper-div
        # and nested-table artifacts indistinguishable from paragraph breaks,
        # so one line per block boundary is the honest reading.
        raw = "".join(self._chunks).replace("\u00a0", " ").replace("\u200b", "")
        out: list[str] = []
        for line in raw.splitlines():
            line = " ".join(line.split())
            if line and line not in _DIVIDERS:
                out.append(line)
        return "\n".join(out)


def html_to_text(html: str) -> str:
    """Readable plain text for an email HTML body (tags stripped, links kept
    as "text (url)", line breaks at block boundaries, entities decoded)."""
    parser = _EmailTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def resolve_body(payload: dict) -> str:
    """Body for a Gmail message/draft payload: text/plain parts are preferred
    verbatim (unchanged behavior for multipart text+HTML mail); only when no
    plain part carries text does text/html get stripped to readable text.
    Empty return means "no body at all" — callers fall back to the snippet."""
    plain = _collect_part_text(payload, "text/plain")
    if plain:
        return plain
    html = _collect_part_text(payload, "text/html")
    return html_to_text(html) if html else ""


def _collect_part_text(part: dict, mime: str) -> str:
    """Depth-first walk of the MIME tree for parts of `mime`, base64url-decoded
    and joined with newlines — the same walk gmail used for plain text only."""
    if part.get("mimeType") == mime:
        data = part.get("body", {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    found: list[str] = []
    for child in part.get("parts", []):
        text = _collect_part_text(child, mime)
        if text:
            found.append(text)
    return "\n".join(found)
