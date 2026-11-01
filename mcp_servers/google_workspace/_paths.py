"""Shared filesystem helpers for the Google Workspace tools.

Downloads and upload sources are confined to the harness's allowed file
roots. This is a deliberate small duplicate of src/harness/core/paths.py —
this server runs as a separate process and cannot import harness code. The
harness injects HARNESS_FILE_ROOTS (os.pathsep-separated) when it spawns
the server; the fallback anchor covers a standalone run (e.g. --auth,
which never touches files) and relies on the editable install layout.
"""

from __future__ import annotations

import os
from pathlib import Path

_FALLBACK_ROOT = Path(__file__).resolve().parents[2] / "scratch"
DOWNLOADS_SUBDIR = "downloads"


def _allowed_roots() -> list[Path]:
    env = os.environ.get("HARNESS_FILE_ROOTS")
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(os.pathsep) if p]
    return [_FALLBACK_ROOT]


def downloads_dir() -> Path:
    """Where downloads land when the caller doesn't pass an explicit path."""
    return _allowed_roots()[0] / DOWNLOADS_SUBDIR


def _require_inside_roots(resolved: Path, local_path: str) -> None:
    roots = _allowed_roots()
    if not any(resolved.is_relative_to(r) for r in roots):
        listing = ", ".join(str(r) for r in roots)
        raise RuntimeError(
            f"{local_path!r} is outside the agent's file area. Local files may "
            f"only be read or written under: {listing}. Downloads default to "
            f"{downloads_dir()}."
        )


def resolve_download_dest(local_path: str, name: str) -> Path:
    """Resolve where a download should be written.

    `local_path` may be a full file path or an existing directory; if empty (or
    a directory) the file lands under its own `name`, defaulting to the
    downloads dir. The destination must be inside the allowed roots. Ensures
    the parent directory exists. Shared by drive_download and
    gmail_download_attachment so the two stay consistent.
    """
    dest = Path(local_path).expanduser().resolve() if local_path else downloads_dir()
    if not local_path or dest.is_dir():
        dest = dest / name
    _require_inside_roots(dest, local_path or str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def resolve_upload_source(local_path: str) -> Path:
    """Resolve a local file the model wants to hand outward (upload/attach).

    Confined to the allowed roots for the same reason reads of secrets are
    denied: an unconfined upload source is an exfiltration vector. Raises if
    the file is missing.
    """
    path = Path(local_path).expanduser().resolve()
    _require_inside_roots(path, local_path)
    if not path.is_file():
        raise RuntimeError(f"No file to upload at {local_path!r}.")
    return path
