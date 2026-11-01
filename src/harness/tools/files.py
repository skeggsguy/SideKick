"""File tools: read_file, list_files (read-only) and edit_file / copy_file (gated).

Each tool's blocking file I/O lives in a sync _run; the async run wraps it
in asyncio.to_thread so the event loop stays responsive."""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

from ..core.paths import (
    SCRATCH_DIR,
    WriteJailError,
    deny_read_name,
    deny_read_reason,
    is_sensitive_read,
    resolve_for_write,
)
from ..core.policy import ActionClass
from .base import Tool

# Guard against a tool call accidentally dumping a huge file into the context.
MAX_BYTES = 200_000
# Cap directory listings so a recursive walk can't flood the context.
MAX_ENTRIES = 500
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache"}


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read and return the UTF-8 text contents of a file at the given path. "
        "Use this to inspect source code, config, or data files on the machine."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file to read (absolute or relative to the working directory).",
            }
        },
        "required": ["path"],
    }
    classification = ActionClass.READ_ONLY

    async def run(self, args: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._run, args)

    def _run(self, args: dict[str, Any]) -> str:
        path = os.path.expanduser(args["path"])
        reason = deny_read_reason(path)
        if reason:
            return f"Error: cannot read {path}: {reason}."
        if not os.path.exists(path):
            return f"Error: no such file: {path}"
        if os.path.isdir(path):
            return f"Error: {path} is a directory, not a file."
        try:
            size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(MAX_BYTES)
            if size > MAX_BYTES:
                data += f"\n\n[...truncated, file is {size} bytes; showed first {MAX_BYTES}]"
            return data
        except Exception as e:  # noqa: BLE001 - surface any read error to the model
            return f"Error reading {path}: {e}"


class ListFilesTool(Tool):
    name = "list_files"
    description = (
        "List files and directories under a path (recursive, depth-first), "
        "one relative path per line; directories end with '/'. Use this to "
        "explore the layout of a project before reading files."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory to list (default: the working directory).",
            }
        },
        "required": [],
    }
    classification = ActionClass.READ_ONLY

    async def run(self, args: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._run, args)

    def _run(self, args: dict[str, Any]) -> str:
        root = os.path.expanduser(args.get("path") or ".")
        reason = deny_read_reason(root)
        if reason:
            return f"Error: cannot list {root}: {reason}."
        if not os.path.isdir(root):
            return f"Error: no such directory: {root}"
        entries: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            )
            rel = os.path.relpath(dirpath, root)
            prefix = "" if rel == "." else rel + os.sep
            entries.extend(prefix + d + "/" for d in dirnames)
            entries.extend(
                prefix + f for f in sorted(filenames) if not deny_read_name(f)
            )
            if len(entries) >= MAX_ENTRIES:
                entries = entries[:MAX_ENTRIES]
                entries.append(f"[...truncated at {MAX_ENTRIES} entries]")
                break
        return "\n".join(entries) or "(empty directory)"


class EditFileTool(Tool):
    name = "edit_file"
    description = (
        "Create or edit a text file. To create a new file (or overwrite an "
        "empty one), pass old_str=\"\" and the full contents as new_str. To "
        "edit, pass an old_str that appears exactly once in the file and the "
        "new_str to replace it with. Writes are only allowed inside the file "
        f"area ({SCRATCH_DIR}: downloads/ and the sandbox workspace live inside "
        "it). Reads work anywhere except secret files (.env, tokens, keys)."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path of the file to create or edit."},
            "old_str": {
                "type": "string",
                "description": "Exact text to replace; empty string to create a new file.",
            },
            "new_str": {"type": "string", "description": "Replacement (or full new file) text."},
        },
        "required": ["path", "old_str", "new_str"],
    }
    classification = ActionClass.MUTATING_LOCAL

    async def run(self, args: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._run, args)

    def _run(self, args: dict[str, Any]) -> str:
        try:
            path = str(resolve_for_write(args["path"]))
        except WriteJailError as e:
            return f"Error: {e}"
        old, new = args["old_str"], args["new_str"]

        if old == "":
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return (
                    f"Error: {path} already exists and is not empty; pass the "
                    "exact old_str to edit it."
                )
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            return f"Created {path} ({len(new)} chars)."

        if not os.path.exists(path):
            return f"Error: no such file: {path}"
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        count = content.count(old)
        if count == 0:
            return "Error: old_str not found in file."
        if count > 1:
            return f"Error: old_str appears {count} times; provide more context to make it unique."
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.replace(old, new, 1))
        return f"Edited {path}."


class CopyFileTool(Tool):
    name = "copy_file"
    description = (
        "Copy a file byte-for-byte, binaries included (PDFs, DOCX, images — "
        "anything edit_file's text mode can't handle). The source can be any "
        "readable path; the destination must be inside the file area "
        f"({SCRATCH_DIR}: the scratchpad — uploaded documents live in "
        "scratch/uploads/). An existing destination is never clobbered "
        "unless overwrite=true, and missing parent directories are created. "
        "Use this to save an uploaded document elsewhere in the file area "
        "under a real filename (the uploads/ file keeps its hash name)."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "src": {
                "type": "string",
                "description": (
                    "File to copy from (any readable path, e.g. a "
                    "scratch/uploads/<id> document)."
                ),
            },
            "dest": {
                "type": "string",
                "description": (
                    "Destination file path inside the file area — a full "
                    "file path, not a directory."
                ),
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "Replace dest if it already exists (default false: an "
                    "existing file is refused)."
                ),
            },
        },
        "required": ["src", "dest"],
    }
    classification = ActionClass.MUTATING_LOCAL
    remote = False

    async def run(self, args: dict[str, Any]) -> str:
        return await asyncio.to_thread(self._run, args)

    def _run(self, args: dict[str, Any]) -> str:
        src = os.path.expanduser(str(args.get("src") or "").strip())
        dest = str(args.get("dest") or "").strip()
        if not src or not dest:
            return "Error: copy_file needs both 'src' and 'dest'."

        reason = deny_read_reason(src)
        if reason:
            return f"Error: cannot read {src}: {reason}."
        if is_sensitive_read(src):
            # Broader than the hard denylist (read_document refuses the same
            # class): a copy MOVES secret-shaped bytes to a plainer location,
            # and the egress taint only tracks read_file, not the copy.
            return f"Error: cannot copy {src}: secret-class files are off-limits."
        if not os.path.exists(src):
            return f"Error: no such file: {src}"
        if os.path.isdir(src):
            return f"Error: {src} is a directory, not a file."
        if not os.path.isfile(src):
            # FIFOs/character devices are not copies: /dev/null would
            # "succeed" as an empty file and /dev/zero would spin forever
            # filling the disk. (isfile FOLLOWS symlinks, so a link to a
            # regular file still copies.)
            return f"Error: {src} is not a regular file."

        # resolve_for_write resolves through symlinks and '..' before the
        # roots check, so a dest that merely LOOKS inside the jail — a
        # symlinked parent dir, a .. detour, a symlink onto an outside file —
        # is refused here rather than written through.
        try:
            dest_path = resolve_for_write(dest)
        except WriteJailError as e:
            return f"Error: {e}"
        if dest_path.is_dir():
            return (
                f"Error: {dest_path} is a directory; copy_file needs a full "
                "destination file path."
            )
        if dest_path.exists() and os.path.samefile(src, dest_path):
            # copy2 would raise SameFileError mid-copy; say it plainly first.
            # Covers a symlinked dest pointing back at src, and overwrite=true.
            return "Error: src and dest are the same file; nothing to copy."
        if dest_path.exists() and not args.get("overwrite"):
            return (
                f"Error: {dest_path} already exists; pass overwrite=true to "
                "replace it."
            )
        # A uploads/ document's .name sidecar is deliberately NOT copied: it
        # is upload-store metadata that only media.resolve() reads, keyed to
        # the content-hash id inside uploads/; beside a renamed copy it would
        # be dead bytes. The requested dest name IS the name here.
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest_path)
            size = dest_path.stat().st_size
        except OSError as e:
            return f"Error: copy to {dest_path} failed: {e}"
        return f"Copied {src} to {dest_path} ({size} bytes)."
