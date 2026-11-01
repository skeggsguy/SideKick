"""SandboxRunTool builds the right `docker run` argv: isolated (--network none,
dropped caps, resource limits), non-root so /work outputs stay owned by the host
user, and mounting only a dedicated workspace — never the repo root (which holds
.env/.git/src). A pre-flight turns docker socket errors into actionable messages.
No Docker needed — the subprocess layer is mocked.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from harness.core.paths import SCRATCH_DIR
from harness.tools.sandbox import (
    DEFAULT_TIMEOUT_S,
    SandboxRunTool,
    _classify_docker_error,
    _hint_missing_lib,
)


class _FakeProc:
    """A subprocess that exits 0 with 'ok' on stdout — passes the preflight and
    stands in for `docker run`."""

    def __init__(self, returncode: int = 0, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok\n", self._stderr

    async def wait(self) -> int:
        return self.returncode


def _capture(monkeypatch, *, run_rc: int = 0, run_stderr: bytes = b"") -> list[list[str]]:
    """Patch create_subprocess_exec to record argv instead of spawning. The
    preflight (`docker version`) and image-inspect always succeed; the recorded
    `docker run` proc uses run_rc/run_stderr."""
    seen: list[list[str]] = []

    async def fake_exec(*argv, **kwargs):
        seen.append(list(argv))
        if argv[:2] == ("docker", "run"):
            return _FakeProc(run_rc, run_stderr)
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def _run_argv(seen: list[list[str]]) -> list[str]:
    """The `docker run` argv — the preflight's `docker version` comes first now."""
    return next(a for a in seen if len(a) > 1 and a[1] == "run")


async def test_sandbox_argv_is_isolated_hardened_and_nonroot(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path))
    monkeypatch.delenv("HARNESS_FILE_ROOTS", raising=False)
    seen = _capture(monkeypatch)
    tool = SandboxRunTool(image="test-image:latest")

    out = await tool.run({"command": "echo hi"})

    assert "ok" in out
    argv = _run_argv(seen)
    # Isolation stays exactly as the design mandates.
    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    # Cheap hardening flags are pinned so a future edit can't silently drop them.
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert ["--security-opt", "no-new-privileges"] == argv[
        argv.index("--security-opt"): argv.index("--security-opt") + 2
    ]
    assert "--pids-limit" in argv
    assert "--memory" in argv
    assert "--cpus" in argv
    assert "HOME=/tmp" in argv  # writable HOME for matplotlib & friends
    # Non-root: files written to the workspace are owned by the host user.
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    # The mount is the dedicated workspace at the SAME path in and out (clean
    # handback), and is NOT cwd / the repo root.
    assert f"{tmp_path}:{tmp_path}" in argv
    assert argv[argv.index("--workdir") + 1] == str(tmp_path)
    assert not any(a == f"{os.getcwd()}:{os.getcwd()}" for a in argv)
    assert "/work" not in argv  # the /work-vs-host trap is gone
    # The file area (default root: scratch) rides along READ-ONLY; the
    # workspace itself is never :ro.
    assert f"{SCRATCH_DIR}:{SCRATCH_DIR}:ro" in argv
    assert f"{tmp_path}:{tmp_path}:ro" not in argv
    assert "test-image:latest" in argv
    assert argv[-1] == "echo hi"


async def test_workspace_nested_in_ro_root(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(ws))
    seen = _capture(monkeypatch)

    await SandboxRunTool(image="img").run({"command": "true"})
    argv = _run_argv(seen)
    # RO root with the RW workspace nested inside it.
    assert f"{tmp_path}:{tmp_path}:ro" in argv
    assert f"{ws}:{ws}" in argv
    assert f"{ws}:{ws}:ro" not in argv


async def test_root_equal_to_workspace_not_ro_mounted(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path))
    seen = _capture(monkeypatch)

    await SandboxRunTool(image="img").run({"command": "true"})
    argv = _run_argv(seen)
    # Exactly one mount of the path, and it's the RW workspace mount.
    assert argv.count(f"{tmp_path}:{tmp_path}") == 1
    assert f"{tmp_path}:{tmp_path}:ro" not in argv


async def test_missing_root_skipped(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path / "ws"))
    seen = _capture(monkeypatch)

    out = await SandboxRunTool(image="img").run({"command": "true"})
    assert "ok" in out  # run still succeeds
    argv = _run_argv(seen)
    # No :ro bind for a root missing on the host (docker would create it
    # root-owned).
    assert not any(a.endswith(":ro") for a in argv)


async def test_sandbox_default_timeout_and_custom(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path))
    _capture(monkeypatch)
    tool = SandboxRunTool(image="img")
    assert "ok" in await tool.run({"command": "true"})
    assert DEFAULT_TIMEOUT_S == 60


async def test_sandbox_reports_missing_docker(monkeypatch):
    async def boom(*argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    tool = SandboxRunTool(image="img")

    out = await tool.run({"command": "echo hi"})
    assert "docker is not installed" in out


async def test_preflight_permission_denied_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path))
    seen: list[list[str]] = []

    async def fake_exec(*argv, **kwargs):
        seen.append(list(argv))
        # `docker version` (the preflight) fails with a socket permission error.
        return _FakeProc(
            1, b"permission denied while trying to connect to ... docker.sock"
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tool = SandboxRunTool(image="img")

    out = await tool.run({"command": "echo hi"})
    assert "permission denied" in out.lower()
    assert "orbctl stop && orbctl start" in out
    # Short-circuits: `docker run` is never reached.
    assert not any(a[:2] == ["docker", "run"] for a in seen)


def test_classify_permission_beats_daemon_when_both_present():
    # A socket EACCES can also say "Cannot connect to the Docker daemon" — the
    # permission remedy must win, not "start the daemon".
    msg = _classify_docker_error(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock: "
        "permission denied"
    )
    assert "docker context use orbstack" in msg
    assert "isn't running" not in msg


def test_classify_daemon_down():
    msg = _classify_docker_error(
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?"
    )
    assert "OrbStack is stopped" in msg


def test_classify_remedies_point_at_orbstack():
    # The engine lives in OrbStack's VM — a Linux remedy (usermod, systemctl,
    # wsl --shutdown) would send the operator down a dead end.
    daemon = _classify_docker_error(
        "Cannot connect to the Docker daemon at unix:///Users/x/.orbstack/run/"
        "docker.sock. Is the docker daemon running?"
    )
    perm = _classify_docker_error("permission denied ... docker.sock")
    missing = _classify_docker_error("exec: docker: executable file not found")
    assert "orbctl start" in daemon
    assert "orbctl" in perm
    assert "orbstack" in missing.lower()
    for msg in (daemon, perm, missing):
        for linux_only in ("systemctl", "usermod", "wsl --shutdown", "install_docker.sh"):
            assert linux_only not in msg


def test_classify_unknown_is_bounded():
    msg = _classify_docker_error("x" * 5000)
    assert len(msg) < 600  # the raw stderr is trimmed, not dumped wholesale


async def test_image_fallback_when_prebaked_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_SANDBOX_IMAGE", raising=False)
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path))
    seen: list[list[str]] = []

    async def fake_exec(*argv, **kwargs):
        seen.append(list(argv))
        # image inspect of harness-sandbox:latest "fails" (not built) → fallback.
        if argv[:3] == ("docker", "image", "inspect"):
            return _FakeProc(1)
        return _FakeProc(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    tool = SandboxRunTool()  # no explicit image → resolve path

    out = await tool.run({"command": "echo hi"})
    argv = _run_argv(seen)
    assert "python:3.12-slim" in argv
    assert "harness-sandbox:latest" not in argv
    assert "image not built" in out


async def test_explicit_image_is_trusted_without_inspect(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path))
    seen = _capture(monkeypatch)
    tool = SandboxRunTool(image="custom:1")

    await tool.run({"command": "echo hi"})
    assert "custom:1" in _run_argv(seen)
    # An explicit image is trusted — no `docker image inspect` probe.
    assert not any(a[:3] == ["docker", "image", "inspect"] for a in seen)


def test_missing_lib_hint_fires_on_module_error():
    hint = _hint_missing_lib("Traceback ... ModuleNotFoundError: No module named 'scipy'")
    assert "no network" in hint.lower()
    assert "docker/sandbox/Dockerfile" in hint
    assert _hint_missing_lib("all good") == ""
