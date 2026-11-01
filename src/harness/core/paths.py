"""Filesystem confinement for the agent's file surfaces.

The agent may WRITE only inside the allowed roots — by default the repo's
scratch/ dir (which contains the sandbox workspace and downloads) plus
HARNESS_SANDBOX_MOUNT when that points outside scratch. Reads stay global,
but a small denylist shields secrets (.env files, token/credential files,
~/.ssh, the harness data dir); the name patterns mirror .gitignore's
secrets section.

Env is read at call time, not import time (same pattern as
tools/sandbox.py), so tests can monkeypatch:

- HARNESS_FILE_ROOTS: os.pathsep-separated paths; overrides the default
  roots entirely. Also injected into MCP server processes so the Google
  Workspace downloads obey the same jail.
- HARNESS_SANDBOX_MOUNT: the sandbox workspace; a write root when set.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from ..store.db import default_db_path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRATCH_DIR = REPO_ROOT / "scratch"
DOWNLOADS_SUBDIR = "downloads"

# Filenames reads must never surface, wherever they live.
_DENY_NAME_PATTERNS = (
    ".env",
    ".env.*",
    "*token*.json",
    "*credential*",
    ".netrc",
    "_netrc",
    ".git-credentials",
)

# Home subdirs reads must never surface (keys, cloud/cli credentials).
_DENY_HOME_DIRS = (".ssh", ".aws", ".kube", ".gnupg", ".config/gh")

# Broader secret-shaped patterns: reading one of these doesn't fail, but it
# TAINTS the turn (core/policy.TurnContext) — outbound tools (web_fetch,
# web_search) stop auto-running until the turn ends, breaking the
# read-secret → exfiltrate-in-a-GET chain at the egress step. Superset of the
# hard denylist: even a *denied* read attempt signals intent.
_SENSITIVE_NAME_PATTERNS = _DENY_NAME_PATTERNS + (
    ".npmrc",
    ".pypirc",
    "id_rsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*secret*",
    "*apikey*",
    "*.bash_history",
    "*.zsh_history",
    ".bash_history",
    ".zsh_history",
)
_SENSITIVE_HOME_DIRS = _DENY_HOME_DIRS + (
    ".docker",
    ".password-store",
    ".mozilla",
    ".config/gcloud",
    ".config/google-chrome",
    ".config/chromium",
)


class WriteJailError(ValueError):
    """A write path escaped the allowed roots."""


def allowed_write_roots() -> list[Path]:
    env = os.environ.get("HARNESS_FILE_ROOTS")
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(os.pathsep) if p]
    roots = [SCRATCH_DIR]
    mount = os.environ.get("HARNESS_SANDBOX_MOUNT")
    if mount:
        resolved = Path(mount).expanduser().resolve()
        if not resolved.is_relative_to(SCRATCH_DIR):
            roots.append(resolved)
    return roots


def roots_env_value() -> str:
    """The allowed roots serialized for a child process (HARNESS_FILE_ROOTS)."""
    return os.pathsep.join(str(r) for r in allowed_write_roots())


def resolve_for_write(path: str) -> Path:
    """Resolve a model-supplied path, requiring it inside an allowed root.

    resolve() follows symlinks in existing ancestors and anchors relative
    paths to cwd, so both symlink escapes and cwd-relative writes (the
    daemon's cwd is the repo root) are caught, not just literal prefixes.
    """
    roots = allowed_write_roots()
    resolved = Path(path).expanduser().resolve()
    if any(resolved.is_relative_to(r) for r in roots):
        return resolved
    listing = ", ".join(str(r) for r in roots)
    raise WriteJailError(
        f"{path} is outside the agent's writable area. Files may only be "
        f"created or edited under: {listing}. Downloads default to "
        f"{roots[0] / DOWNLOADS_SUBDIR}."
    )


def deny_read_name(name: str) -> bool:
    """True when a bare filename matches the secrets denylist."""
    return any(fnmatch.fnmatch(name, pat) for pat in _DENY_NAME_PATTERNS)


def _under_home_dir(resolved: Path, subdir: str) -> bool:
    """Is `resolved` inside ~/<subdir>? Also matches the symlink target when
    ~/<subdir> is a link (e.g. ~/.aws → a synced or shared folder), since
    resolve() rewrites the read path to the target. Use this for ANY read-deny/
    sensitivity rule — never a raw ~/<dir> prefix check (home config dirs can be symlinks)."""
    target = Path.home() / subdir
    if resolved.is_relative_to(target):
        return True
    try:
        real = target.resolve()
    except OSError:
        return False
    return real != target and resolved.is_relative_to(real)


def deny_read_reason(path: str) -> str | None:
    """Why a read of `path` is denied, or None when it's allowed."""
    resolved = Path(path).expanduser().resolve()
    if deny_read_name(resolved.name):
        return f"{resolved.name} may contain secrets"
    if ".git" in resolved.parts:
        return ".git contents are off-limits"
    for subdir in _DENY_HOME_DIRS:
        if _under_home_dir(resolved, subdir):
            return f"~/{subdir} is off-limits"
    data_dir = default_db_path().expanduser().resolve().parent
    if resolved.is_relative_to(data_dir):
        return f"the harness data dir ({data_dir}) is off-limits"
    return None


def is_sensitive_read(path: str) -> bool:
    """True when reading `path` should taint the turn (secret-class read).

    Deliberately broader than the hard denylist: the denylist must not
    false-positive (it blocks reads outright), while a taint only means
    outbound tools ask before running for the rest of the turn.
    """
    # A strict SUPERSET of deny_read_reason (it ends with that call): check deny FIRST,
    # or every hard denial loses its specific reason.
    resolved = Path(path).expanduser().resolve()
    name = resolved.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in _SENSITIVE_NAME_PATTERNS):
        return True
    if any(_under_home_dir(resolved, d) for d in _SENSITIVE_HOME_DIRS):
        return True
    return deny_read_reason(path) is not None
