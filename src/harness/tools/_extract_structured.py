"""Embedded structured data extraction (WP-A2): JSON-LD, __NEXT_DATA__ and
window state blobs from <script> tags. A listing page's real data often lives
there while the visible prose is a JS placeholder. Compaction + budgeting
(4k/blob, 8k total) keep a blob from starving the prose budget; the first
source that yields blobs wins."""

from __future__ import annotations

import json
from html.parser import HTMLParser
from typing import Any

_BLOB_MIN_CHARS = 80  # smaller blobs are config noise, not data
_BLOB_MAX_CHARS = 4_000
_STRUCTURED_BUDGET = 8_000  # total across blobs — separate from prose max_chars
_MAX_LIST_ITEMS = 10
_MAX_STR_CHARS = 300
_WINDOW_STATE_MARKERS = (
    "window.__NUXT__",
    "window.__INITIAL_STATE__",
    "window.__APOLLO_STATE__",
)


class _ScriptCollector(HTMLParser):
    """Collect (attrs, text) for every <script> element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)  # charrefs inside <script> stay raw
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self._attrs: dict[str, str | None] | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "script":
            self._attrs = dict(attrs)
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._attrs is not None:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._buf)))
            self._attrs = None


def _collect_scripts(html: str) -> list[tuple[dict[str, str | None], str]]:
    parser = _ScriptCollector()
    parser.feed(html)
    parser.close()
    return parser.scripts


def extract_structured(html: str) -> list[tuple[str, str]]:
    """Best-effort extraction of machine-readable data embedded in <script>
    tags. Returns [(kind, compacted_json_str), ...] — [] when nothing usable.

    Priority: JSON-LD > __NEXT_DATA__ (drilled to props.pageProps) > window
    state assignments (__NUXT__ / __INITIAL_STATE__ / __APOLLO_STATE__); the
    first source that yields blobs wins. Malformed JSON is silently skipped.
    Blobs are compacted (lists pruned, long strings truncated) and budgeted
    (4k per blob, 8k total) — a budget deliberately separate from the prose
    max_chars so structured data never starves prose and vice versa.
    """
    scripts = _collect_scripts(html)
    for finder in (_jsonld_blobs, _next_data_blobs, _window_state_blobs):
        blobs = finder(scripts)
        if blobs:
            return blobs
    return []


def _compact(value: Any) -> Any:
    """Recursively prune a parsed JSON value for model consumption."""
    if isinstance(value, dict):
        return {k: _compact(v) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > _MAX_LIST_ITEMS:
            head = [_compact(v) for v in value[:_MAX_LIST_ITEMS]]
            return head + [f"…(+{len(value) - _MAX_LIST_ITEMS} more items)"]
        return [_compact(v) for v in value]
    if isinstance(value, str) and len(value) > _MAX_STR_CHARS:
        return value[:_MAX_STR_CHARS] + "…"
    return value


def _budgeted(candidates: list[tuple[str, Any]]) -> list[tuple[str, str]]:
    """Compact each candidate object and apply the per-blob/total budgets."""
    out: list[tuple[str, str]] = []
    total = 0
    for kind, obj in candidates:
        blob = json.dumps(_compact(obj), ensure_ascii=False, separators=(",", ":"))
        if len(blob) < _BLOB_MIN_CHARS:
            continue
        if len(blob) > _BLOB_MAX_CHARS:
            blob = blob[:_BLOB_MAX_CHARS] + "…[blob truncated]"
        if total + len(blob) > _STRUCTURED_BUDGET:
            break
        out.append((kind, blob))
        total += len(blob)
    return out


def _jsonld_blobs(
    scripts: list[tuple[dict[str, str | None], str]],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, Any]] = []
    for attrs, text in scripts:
        ctype = (attrs.get("type") or "").split(";")[0].strip().lower()
        if ctype != "application/ld+json":
            continue
        try:
            candidates.append(("application/ld+json", json.loads(text)))
        except ValueError:
            continue  # malformed embedded JSON — skip silently
    return _budgeted(candidates)


def _next_data_blobs(
    scripts: list[tuple[dict[str, str | None], str]],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, Any]] = []
    for attrs, text in scripts:
        if attrs.get("id") != "__NEXT_DATA__":
            continue
        try:
            data = json.loads(text)
        except ValueError:
            continue
        props = data.get("props") if isinstance(data, dict) else None
        page_props = props.get("pageProps") if isinstance(props, dict) else None
        if isinstance(page_props, (dict, list)) and page_props:
            candidates.append(("__NEXT_DATA__ props.pageProps", page_props))
        else:
            candidates.append(("__NEXT_DATA__", data))
    return _budgeted(candidates)


def _window_state_blobs(
    scripts: list[tuple[dict[str, str | None], str]],
) -> list[tuple[str, str]]:
    candidates: list[tuple[str, Any]] = []
    for _attrs, text in scripts:
        for marker in _WINDOW_STATE_MARKERS:
            idx = text.find(marker)
            if idx < 0:
                continue
            eq = text.find("=", idx + len(marker))
            brace = text.find("{", eq) if eq >= 0 else -1
            if brace < 0:
                continue
            literal = _balanced_object(text, brace)
            if literal is None:
                continue
            try:
                candidates.append((marker, json.loads(literal)))
            except ValueError:
                continue  # JS-only literal (functions, single quotes) — skip
    return _budgeted(candidates)


def _balanced_object(text: str, start: int) -> str | None:
    """Return the brace-balanced {...} substring starting at text[start], or
    None if it never closes. String- and escape-aware (double quotes only —
    JSON's grammar; single-quoted JS fails the json.loads afterwards anyway)."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
