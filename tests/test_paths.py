"""Filesystem write jail + read denylist (core/paths.py)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.core.paths import (
    SCRATCH_DIR,
    WriteJailError,
    allowed_write_roots,
    deny_read_name,
    deny_read_reason,
    resolve_for_write,
    roots_env_value,
)


@pytest.fixture
def jail(tmp_path, monkeypatch):
    """One allowed root at tmp_path/root; env otherwise clean."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(root))
    monkeypatch.delenv("HARNESS_SANDBOX_MOUNT", raising=False)
    return root


def test_write_inside_root_accepted(jail):
    assert resolve_for_write(str(jail / "a.txt")) == jail / "a.txt"
    # Not-yet-existing nested path is fine — resolve() is non-strict.
    nested = jail / "deep" / "er" / "b.txt"
    assert resolve_for_write(str(nested)) == nested


def test_write_outside_root_rejected_with_roots_in_message(jail, tmp_path):
    with pytest.raises(WriteJailError) as exc:
        resolve_for_write(str(tmp_path / "escape.txt"))
    assert str(jail) in str(exc.value)
    assert "downloads" in str(exc.value)


def test_symlink_escape_rejected(jail, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (jail / "link").symlink_to(outside)
    with pytest.raises(WriteJailError):
        resolve_for_write(str(jail / "link" / "x.txt"))


def test_home_path_rejected(jail):
    with pytest.raises(WriteJailError):
        resolve_for_write("~/escape.txt")


def test_relative_path_resolves_against_cwd(jail, tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with pytest.raises(WriteJailError):
        resolve_for_write("x.txt")
    with pytest.raises(WriteJailError):
        resolve_for_write(".")
    monkeypatch.chdir(jail)
    assert resolve_for_write("x.txt") == jail / "x.txt"


def test_default_roots_are_scratch(monkeypatch):
    monkeypatch.delenv("HARNESS_FILE_ROOTS", raising=False)
    monkeypatch.delenv("HARNESS_SANDBOX_MOUNT", raising=False)
    assert allowed_write_roots() == [SCRATCH_DIR]


def test_sandbox_mount_outside_scratch_is_added(monkeypatch, tmp_path):
    monkeypatch.delenv("HARNESS_FILE_ROOTS", raising=False)
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(tmp_path / "ws"))
    assert allowed_write_roots() == [SCRATCH_DIR, tmp_path / "ws"]


def test_sandbox_mount_inside_scratch_deduped(monkeypatch):
    monkeypatch.delenv("HARNESS_FILE_ROOTS", raising=False)
    monkeypatch.setenv("HARNESS_SANDBOX_MOUNT", str(SCRATCH_DIR / "sandbox"))
    assert allowed_write_roots() == [SCRATCH_DIR]


def test_roots_env_value_round_trips(monkeypatch, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    monkeypatch.setenv("HARNESS_FILE_ROOTS", os.pathsep.join([str(a), str(b)]))
    assert roots_env_value() == os.pathsep.join([str(a), str(b)])
    assert allowed_write_roots() == [a, b]


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", "token.json", "gcp_token.json", "credentials.json"],
)
def test_deny_read_name_hits(name):
    assert deny_read_name(name)


def test_deny_read_name_misses_ordinary_files():
    assert not deny_read_name("notes.txt")
    assert not deny_read_name("environment.py")


def test_deny_read_reason_hits(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "data" / "harness.db"))
    assert deny_read_reason(str(tmp_path / ".env"))
    assert deny_read_reason(str(tmp_path / "repo" / ".git" / "config"))
    assert deny_read_reason(str(Path.home() / ".ssh" / "id_ed25519"))
    assert deny_read_reason(str(tmp_path / "data" / "anything.txt"))


def test_deny_read_reason_allows_ordinary_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DB_PATH", str(tmp_path / "data" / "harness.db"))
    assert deny_read_reason(str(tmp_path / "notes.txt")) is None
    assert deny_read_reason(str(SCRATCH_DIR / "hello.txt")) is None

# ---- secret-class read detection (turn taint) -------------------------------

def test_is_sensitive_read_matches_secret_shapes(tmp_path):
    from harness.core.paths import is_sensitive_read

    for name in (
        ".netrc",
        "id_rsa",
        "id_ed25519.pub",
        "server.pem",
        "private.key",
        "client_secret.json",
        "my_apikey.txt",
        ".bash_history",
        ".npmrc",
    ):
        assert is_sensitive_read(str(tmp_path / name)), name

    for name in ("notes.txt", "report.pdf", "hello.py"):
        assert not is_sensitive_read(str(tmp_path / name)), name


def test_is_sensitive_read_matches_credential_dirs():
    from pathlib import Path

    from harness.core.paths import is_sensitive_read

    home = Path.home()
    for p in (
        home / ".aws" / "config",
        home / ".kube" / "config",
        home / ".config" / "gh" / "hosts.yml",
        home / ".docker" / "config.json",
        home / ".config" / "gcloud" / "application_default_credentials.json",
    ):
        assert is_sensitive_read(str(p)), str(p)


def test_denylist_blocks_netrc_and_credential_dirs():
    from pathlib import Path

    from harness.core.paths import deny_read_reason

    home = Path.home()
    assert deny_read_reason(str(home / ".netrc")) is not None
    assert deny_read_reason(str(home / ".aws" / "credentials")) is not None
    assert deny_read_reason(str(home / ".kube" / "config")) is not None
    assert deny_read_reason(str(home / ".config" / "gh" / "hosts.yml")) is not None
    assert deny_read_reason(str(home / ".gnupg" / "pubring.kbx")) is not None
