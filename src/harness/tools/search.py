"""search_files: find files by name and/or search text inside them.

Replaces the two dominant uses of the removed host bash tool (the audit log
showed ~50 of 170 historical bash calls were find/grep): one READ_ONLY,
jail-respecting tool instead of an ungated shell.

Safety posture mirrors the read tools plus one stricter rule: files the hard
denylist blocks are invisible entirely, and files that would merely TAINT a
read (core/paths.is_sensitive_read — keys, credential stores, histories) are
listed by name but their CONTENT is never searched, so secret bytes can't
enter the context through a query like "password". That keeps this tool
taint-free by construction.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from ..core.paths import deny_read_name, deny_read_reason, is_sensitive_read
from ..core.policy import ActionClass
from .base import Tool

# Vendor/derived trees that make a search slow and the results useless.
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".cache",
}
MAX_FILE_BYTES = 2_000_000
MAX_SCANNED_FILES = 20_000
MAX_RESULTS_CAP = 200
LINE_CHARS = 200


class SearchFilesTool(Tool):
    name = "search_files"
    description = (
        "Search the filesystem like find+grep: match file names against a "
        "glob and/or search file contents for a pattern (regex; falls back "
        "to literal text if the regex is invalid). Returns paths, or "
        "path:line:text for content matches. Give at least one of `name` or "
        "`query`. Skips binary files, vendor dirs (.git, node_modules, "
        ".venv, …) and secret files. Does not follow symlinked directories."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "root": {
                "type": "string",
                "description": "Directory to search, e.g. ~/projects/ai or ~.",
            },
            "name": {
                "type": "string",
                "description": "Filename glob, e.g. *.py or *report*.",
            },
            "query": {
                "type": "string",
                "description": "Text/regex to find inside files (case-insensitive "
                "unless case_sensitive is set).",
            },
            "case_sensitive": {"type": "boolean"},
            "max_results": {
                "type": "integer",
                "description": f"Cap on matches (default 50, max {MAX_RESULTS_CAP}).",
            },
        },
        "required": ["root"],
    }
    classification = ActionClass.READ_ONLY

    async def run(self, args: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._search, args)

    def _search(self, args: dict[str, Any]) -> str:
        root = Path(str(args.get("root", ""))).expanduser()
        name_glob = str(args.get("name", "")).strip()
        query = str(args.get("query", "")).strip()
        if not name_glob and not query:
            return "search_files error: give `name` (glob), `query` (text), or both."
        reason = deny_read_reason(str(root))
        if reason is not None:
            return f"search_files error: {reason}."
        if not root.is_dir():
            return f"search_files error: {root} is not a directory."
        max_results = min(max(int(args.get("max_results", 50)), 1), MAX_RESULTS_CAP)

        pattern = None
        if query:
            flags = 0 if args.get("case_sensitive") else re.IGNORECASE
            try:
                pattern = re.compile(query, flags)
            except re.error:
                pattern = re.compile(re.escape(query), flags)

        results: list[str] = []
        scanned = 0
        skipped_secret = 0
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and deny_read_reason(os.path.join(dirpath, d)) is None
            ]
            for fname in filenames:
                if truncated:
                    break
                scanned += 1
                if scanned > MAX_SCANNED_FILES:
                    truncated = True
                    break
                if deny_read_name(fname):
                    continue
                if name_glob and not fnmatch.fnmatch(fname, name_glob):
                    continue
                path = os.path.join(dirpath, fname)
                if deny_read_reason(path) is not None:
                    continue
                if pattern is None:
                    results.append(path)
                else:
                    if is_sensitive_read(path):
                        skipped_secret += 1
                        continue
                    results.extend(self._grep(path, pattern, max_results - len(results)))
                if len(results) >= max_results:
                    truncated = True
                    break
            if truncated:
                break

        if not results:
            out = "No matches."
        else:
            out = f"{len(results)} match(es):\n" + "\n".join(results[:max_results])
        notes = []
        if truncated:
            notes.append(
                f"stopped at {max_results} results" if len(results) >= max_results
                else f"stopped after scanning {MAX_SCANNED_FILES} files — narrow the root"
            )
        if skipped_secret:
            notes.append(
                f"{skipped_secret} secret-class file(s) excluded from content search"
            )
        if notes:
            out += f"\n[{'; '.join(notes)}]"
        return out

    @staticmethod
    def _grep(path: str, pattern: re.Pattern, budget: int) -> list[str]:
        if budget <= 0:
            return []
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                return []
            with open(path, "rb") as f:
                head = f.read(8192)
                if b"\0" in head:
                    return []  # binary
                data = head + f.read(MAX_FILE_BYTES - len(head))
        except OSError:
            return []
        text = data.decode("utf-8", errors="replace")
        out = []
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                out.append(f"{path}:{i}: {line.strip()[:LINE_CHARS]}")
                if len(out) >= budget:
                    break
        return out
