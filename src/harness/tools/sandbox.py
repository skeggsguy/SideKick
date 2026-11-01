"""Ephemeral Docker sandbox for model-written code.

Each call runs in a throwaway container: --rm, --network none, dropped
capabilities, and only a dedicated *workspace* directory mounted — NOT the repo
root, so the harness's own secrets (.env), git hooks, and source are out of reach
of model-written (possibly injection-steered) code. The workspace is mounted at
the same absolute path inside the container as on the host, so files the model
writes hand back to the host tools at the identical path (no /work translation).
The sandbox isolates *code execution*; it cannot make outward actions safe —
those are gated by the policy engine instead (see docs/PLAN-v2.md, safety model).

A pre-flight check (see _docker_preflight) turns the cryptic docker socket errors
into one actionable message, so a broken docker setup self-diagnoses instead of
dumping raw stderr at the model.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from ..core.paths import allowed_write_roots
from ..core.policy import ActionClass
from .base import Tool

MAX_OUTPUT = 20_000
DEFAULT_IMAGE = "harness-sandbox:latest"
FALLBACK_IMAGE = "python:3.12-slim"
DEFAULT_TIMEOUT_S = 60
# Libraries pre-installed in docker/sandbox/Dockerfile (network is off at run
# time, so pip install can't work — these are the batteries included).
BAKED_LIBS = "numpy, pandas, matplotlib, pyyaml, pillow, tabulate, openpyxl"

# Default workspace: <repo>/scratch/sandbox (anchored to this file, not cwd, so
# it's stable regardless of where the daemon/REPL was launched).
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_WORKSPACE = _REPO_ROOT / "scratch" / "sandbox"

# Remedies assume the host's runtime: OrbStack on macOS (docs/sandbox-setup.md).
_DOCKER_MISSING_MSG = (
    "Error: docker is not installed or not on PATH. On macOS it comes from "
    "OrbStack: `brew install --cask orbstack`, `orbctl start`, and make sure "
    "`docker` is on this process's PATH (a launchd agent does not read your "
    "shell profile) — see docs/sandbox-setup.md."
)
_PERMISSION_MSG = (
    "Error: permission denied connecting to the Docker socket. Check the "
    "active context points at OrbStack (`docker context use orbstack`), then "
    "restart it with `orbctl stop && orbctl start`."
)
_DAEMON_MSG = (
    "Error: the Docker engine isn't running — OrbStack is stopped. Start it "
    "with `orbctl start` (check `orbctl status`)."
)


async def run_subprocess(argv: list[str], timeout_s: int) -> str:
    """Run a subprocess off the event loop; kill it and report on timeout."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"Error: command timed out after {timeout_s}s."
    return _format_output(
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def _format_output(returncode: int, stdout: str, stderr: str) -> str:
    parts = []
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    parts.append(f"[exit code: {returncode}]")
    out = "\n".join(parts)
    if len(out) > MAX_OUTPUT:
        out = out[:MAX_OUTPUT] + f"\n[...truncated at {MAX_OUTPUT} chars]"
    return out


def _classify_docker_error(stderr: str) -> str:
    """Map a failed `docker` invocation's stderr to an actionable message.

    Ordered deliberately: a socket permission error can also contain
    "Cannot connect to the Docker daemon", so the permission case — the most
    common failure here — must be checked first or it gets the wrong remedy.
    """
    low = stderr.lower()
    if "permission denied" in low:
        return _PERMISSION_MSG
    if "cannot connect to the docker daemon" in low or "is the docker daemon running" in low:
        return _DAEMON_MSG
    if "executable file not found" in low or ("not found" in low and "docker" in low):
        return _DOCKER_MISSING_MSG
    trimmed = stderr.strip()
    if len(trimmed) > 400:  # don't leak a wall of host paths into model context
        trimmed = trimmed[:400] + " […]"
    return f"Error: docker is unavailable. {trimmed}"


async def _docker_preflight() -> str | None:
    """Return None if docker is usable, else an actionable error message.

    Probes `docker version --format {{.Server.Version}}`, which forces a daemon
    round-trip so permission/daemon failures surface (a client-only check would
    falsely pass). Runs on every call — it's cheap next to `docker run`, so no
    caching (and thus no global state to race under the orchestrator semaphore).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "version", "--format", "{{.Server.Version}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return _DOCKER_MISSING_MSG
    _, stderr = await proc.communicate()
    if proc.returncode == 0:
        return None
    return _classify_docker_error(stderr.decode("utf-8", errors="replace"))


async def _image_exists(image: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "image", "inspect", image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return await proc.wait() == 0


def _hint_missing_lib(out: str) -> str:
    """Make the 'no network, can't pip install' failure legible to the model."""
    if "ModuleNotFoundError" in out or "No module named" in out or "pip install" in out:
        return (
            f"\n[note] The sandbox has no network — `pip install` cannot work. "
            f"Pre-baked libs: {BAKED_LIBS}. To add another, edit "
            "docker/sandbox/Dockerfile and rerun scripts/build_sandbox_image.sh."
        )
    return ""


def _workspace_dir() -> str:
    return os.environ.get("HARNESS_SANDBOX_MOUNT") or str(_DEFAULT_WORKSPACE)


def _ro_mounts(workspace: str) -> list[str]:
    """--volume args exposing the file-area roots read-only inside the sandbox.

    Skips a root that IS the workspace (duplicate mount target errors) or sits
    inside it (an :ro bind there would punch a read-only hole in the writable
    workspace), and roots missing on the host (docker would create the bind
    source root-owned).
    """
    ws = Path(workspace).resolve()
    argv: list[str] = []
    for root in allowed_write_roots():
        if root.is_relative_to(ws) or not root.is_dir():
            continue
        argv += ["--volume", f"{root}:{root}:ro"]
    return argv


class SandboxRunTool(Tool):
    name = "sandbox_run"
    description = (
        "Run a shell command inside an ephemeral Docker container. The working "
        "directory IS a WORKSPACE that persists between calls — write files with "
        "PLAIN RELATIVE NAMES (e.g. `out.png`), do NOT guess an absolute path "
        "like /work or /workspace (those don't exist; an absolute path will "
        "fail). The workspace has the SAME absolute path inside the sandbox and "
        "on the host, shown as '[workspace: ...]' in each result: to act on a "
        "file you wrote, take that path + your relative name (e.g. "
        "'<workspace>/out.png') and pass it to the host-side tools (drive_upload, "
        "read/edit) — they see the file at that EXACT path. The rest of the "
        "agent's file area (scratch/, including downloads/) is visible READ-ONLY "
        "at its host paths — read downloaded or user-dropped files from there, "
        "but write outputs only in the workspace. CONTRACT: only the "
        "workspace persists (everything else in the container is wiped each "
        "call). There is NO network and NO `pip install`: the pre-baked libs "
        f"({BAKED_LIBS}) are importable, anything else must be added to "
        "docker/sandbox/Dockerfile and the image rebuilt. NO credentials or "
        "secrets are reachable (email, calendar, web are NOT reachable here)."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run. cwd is the workspace — write outputs with relative names (e.g. `out.png`), not an absolute /work path.",
            },
            "timeout_s": {
                "type": "integer",
                "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT_S}).",
            },
        },
        "required": ["command"],
    }
    # Mutates only the mounted workspace — gated like a local write.
    classification = ActionClass.MUTATING_LOCAL

    def __init__(self, image: str | None = None) -> None:
        # An explicit image (ctor arg or env) is trusted as-is. Otherwise we
        # resolve the prebaked image vs the slim fallback once, lazily, in run().
        self._explicit = image or os.environ.get("HARNESS_SANDBOX_IMAGE")
        self._resolved: str | None = None
        self._fallback_note = ""

    async def _resolve_image(self) -> str:
        """Pick the image once per process. Falls back to a *local* slim image
        (never auto-pulls an arbitrary tag) and records an operator note."""
        if self._explicit:
            return self._explicit
        if self._resolved is None:
            if await _image_exists(DEFAULT_IMAGE):
                self._resolved = DEFAULT_IMAGE
            else:
                self._resolved = FALLBACK_IMAGE
                self._fallback_note = (
                    "[note] harness-sandbox image not built — using "
                    "python:3.12-slim (stdlib only). Run "
                    "scripts/build_sandbox_image.sh for the pre-baked libs.\n"
                )
        return self._resolved

    async def run(self, args: dict[str, Any]) -> str:
        preflight = await _docker_preflight()
        if preflight is not None:
            return preflight

        timeout = args.get("timeout_s") or DEFAULT_TIMEOUT_S
        image = await self._resolve_image()
        workspace = _workspace_dir()
        os.makedirs(workspace, exist_ok=True)

        argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            # Defense in depth: no capabilities, no privilege escalation, and
            # process/memory/cpu caps so model code can't fork-bomb or OOM the
            # host (which would reap agentd).
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "2",
            # Give matplotlib/etc. a writable HOME (the container has no passwd
            # entry for our uid, so HOME defaults to "/", which isn't writable).
            "--env",
            "HOME=/tmp",
            # Run as the host user so files written to the workspace are owned by
            # us, not root — otherwise the handback (host tools read/edit the
            # results) fails on permissions. Safe because only the workspace is
            # mounted.
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            # Mount the workspace at the SAME absolute path inside the container
            # as on the host, so a file the model writes here is reachable by the
            # host-side tools (drive_upload, read, edit) at the exact same path —
            # no /work-vs-host translation, which silently broke the handback.
            "--volume",
            f"{workspace}:{workspace}",
            # The rest of the agent's file area (scratch/, incl. downloads/) is
            # visible READ-ONLY so code can process downloaded/dropped files;
            # writes stay workspace-only at the mount level (nesting is fine —
            # docker sorts binds by destination). _ro_mounts skips roots that
            # would shadow the workspace or don't exist on the host.
            *_ro_mounts(workspace),
            "--workdir",
            workspace,
            image,
            "bash",
            "-c",
            args["command"],
        ]
        try:
            out = await run_subprocess(argv, timeout)
        except FileNotFoundError:  # docker vanished between preflight and run
            return _DOCKER_MISSING_MSG
        header = (
            f"[workspace: {workspace} — same path here and on the host; pass it "
            "to other tools to use these files]\n"
        )
        return header + self._fallback_note + out + _hint_missing_lib(out)
