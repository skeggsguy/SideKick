"""File tools honor the write jail and read denylist."""

from __future__ import annotations

import os

import pytest

from harness.tools.files import CopyFileTool, EditFileTool, ListFilesTool, ReadFileTool


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    return tmp_path


async def test_edit_file_creates_inside_root(root):
    out = await EditFileTool().run(
        {"path": str(root / "note.txt"), "old_str": "", "new_str": "hi"}
    )
    assert "Created" in out
    assert (root / "note.txt").read_text() == "hi"


async def test_edit_file_edits_inside_root(root):
    p = root / "note.txt"
    p.write_text("hello world")
    out = await EditFileTool().run(
        {"path": str(p), "old_str": "world", "new_str": "there"}
    )
    assert out == f"Edited {p}."
    assert p.read_text() == "hello there"


async def test_edit_file_rejects_outside_root(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "x.txt"
    out = await EditFileTool().run(
        {"path": str(outside), "old_str": "", "new_str": "nope"}
    )
    assert out.startswith("Error:")
    assert "writable area" in out
    assert str(root) in out
    assert not outside.exists()


async def test_read_file_denies_env(root):
    (root / ".env").write_text("SECRET=1")
    out = await ReadFileTool().run({"path": str(root / ".env")})
    assert out.startswith("Error:")
    assert "SECRET" not in out


async def test_read_file_allows_ordinary(root):
    (root / "a.txt").write_text("data")
    assert await ReadFileTool().run({"path": str(root / "a.txt")}) == "data"


async def test_list_files_omits_secret_names(root):
    (root / "token.json").write_text("{}")
    (root / "a.txt").write_text("x")
    out = await ListFilesTool().run({"path": str(root)})
    assert "a.txt" in out
    assert "token.json" not in out


async def test_list_files_denies_ssh_root(root, monkeypatch, tmp_path):
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    out = await ListFilesTool().run({"path": str(fake_home / ".ssh")})
    assert out.startswith("Error:")


# ---- copy_file ---------------------------------------------------------

# Non-UTF-8 bytes: a text-mode copy (or a decode anywhere) would corrupt this.
PDF_BYTES = b"%PDF-1.4\n\x00\x01\xff\xfe\x80 binary payload \x7e\x00"


def _mk(root, name, data=PDF_BYTES):
    p = root / name
    p.write_bytes(data)
    return p


async def test_copy_file_classification_and_locality():
    """MUTATING_LOCAL is the only allowlistable gated class and copy_file
    must be gated; remote=False keeps it off the connectivity pre-flight and
    out of the EGRESS_TOOLS structural scan."""
    from harness.core.policy import ActionClass

    assert CopyFileTool.classification is ActionClass.MUTATING_LOCAL
    assert CopyFileTool.remote is False


async def test_copy_file_binary_round_trip(root):
    src = _mk(root, "a1b2c3.pdf")
    dest = root / "invoice.pdf"
    out = await CopyFileTool().run({"src": str(src), "dest": str(dest)})
    assert "Copied" in out
    assert dest.read_bytes() == PDF_BYTES


async def test_copy_file_preserves_mtime(root):
    src = _mk(root, "doc.bin")
    os.utime(src, (1_600_000_000, 1_600_000_000))
    dest = root / "doc-copy.bin"
    await CopyFileTool().run({"src": str(src), "dest": str(dest)})
    assert dest.stat().st_mtime == 1_600_000_000  # copy2 metadata, not copyfile


async def test_copy_file_creates_missing_parent_dirs(root):
    src = _mk(root, "a.pdf")
    dest = root / "sub" / "deep" / "a.pdf"
    out = await CopyFileTool().run({"src": str(src), "dest": str(dest)})
    assert "Copied" in out
    assert dest.read_bytes() == PDF_BYTES


async def test_copy_file_relative_dest_resolves_against_cwd(root, monkeypatch):
    # The jail check runs on the RESOLVED path, so a relative dest is judged
    # where it actually lands (the daemon's cwd is the repo root).
    monkeypatch.chdir(root)
    src = _mk(root, "a.pdf")
    out = await CopyFileTool().run({"src": str(src), "dest": "sub/b.pdf"})
    assert "Copied" in out
    assert (root / "sub" / "b.pdf").read_bytes() == PDF_BYTES


async def test_copy_file_refuses_dest_outside_write_root(root, tmp_path_factory):
    src = _mk(root, "a.pdf")
    outside = tmp_path_factory.mktemp("outside") / "x.pdf"
    out = await CopyFileTool().run({"src": str(src), "dest": str(outside)})
    assert out.startswith("Error:")
    assert "writable area" in out
    assert not outside.exists()


async def test_copy_file_refuses_traversal_dest(root):
    src = _mk(root, "a.pdf")
    escaped = root.parent / "evil.pdf"
    out = await CopyFileTool().run(
        {"src": str(src), "dest": str(root / ".." / "evil.pdf")}
    )
    assert out.startswith("Error:")
    assert "writable area" in out
    assert not escaped.exists()


async def test_copy_file_refuses_symlinked_dest_dir(root, tmp_path_factory):
    # A link INSIDE the jail that points OUT is not a write root once
    # resolve() is done with it — the bytes must not follow the escape.
    outside = tmp_path_factory.mktemp("outside")
    (root / "escape").symlink_to(outside)
    src = _mk(root, "a.pdf")
    out = await CopyFileTool().run({"src": str(src), "dest": str(root / "escape" / "x.pdf")})
    assert out.startswith("Error:")
    assert not (outside / "x.pdf").exists()


async def test_copy_file_refuses_symlinked_dest_file_over_write(root, tmp_path_factory):
    # dest being a symlink onto an EXISTING outside file must not become an
    # overwrite=true write-through to it.
    victim = tmp_path_factory.mktemp("outside") / "victim.bin"
    victim.write_bytes(b"original")
    (root / "link.bin").symlink_to(victim)
    src = _mk(root, "a.bin")
    out = await CopyFileTool().run(
        {"src": str(src), "dest": str(root / "link.bin"), "overwrite": True}
    )
    assert out.startswith("Error:")
    assert victim.read_bytes() == b"original"


async def test_copy_file_refuses_denied_src(root):
    (root / ".env").write_text("SECRET=1")
    out = await CopyFileTool().run({"src": str(root / ".env"), "dest": str(root / "out.txt")})
    assert out.startswith("Error:")
    # Assert the denylist's OWN reason, not just any Error: is_sensitive_read
    # is a superset and would refuse too — without the deny guard first, the
    # model loses the specific reason (mutation-pinned 2026-10-20).
    assert "may contain secrets" in out
    assert not (root / "out.txt").exists()


async def test_copy_file_refuses_sensitive_src(root):
    # "*secret*" is secret-class (taints reads) but NOT on the hard denylist —
    # read_file would serve it. A copy MOVES the bytes somewhere the egress
    # taint never follows, so copy_file refuses the whole sensitive class
    # (the read_document precedent).
    _mk(root, "my_secret.bin", b"payload")
    out = await CopyFileTool().run(
        {"src": str(root / "my_secret.bin"), "dest": str(root / "out.bin")}
    )
    assert out.startswith("Error:")
    assert not (root / "out.bin").exists()


async def test_copy_file_refuses_clobber(root):
    src = _mk(root, "new.bin", b"new bytes")
    dest = _mk(root, "keep.bin", b"original")
    out = await CopyFileTool().run({"src": str(src), "dest": str(dest)})
    assert out.startswith("Error:")
    assert "overwrite" in out
    assert dest.read_bytes() == b"original"


async def test_copy_file_overwrite_true_replaces(root):
    src = _mk(root, "new.bin", b"new bytes")
    dest = _mk(root, "old.bin", b"original")
    out = await CopyFileTool().run(
        {"src": str(src), "dest": str(dest), "overwrite": True}
    )
    assert "Copied" in out
    assert dest.read_bytes() == b"new bytes"


async def test_copy_file_refuses_copy_onto_self(root):
    src = _mk(root, "a.bin")
    out = await CopyFileTool().run(
        {"src": str(src), "dest": str(src), "overwrite": True}
    )
    assert out.startswith("Error:")
    # The guard's own wording: without it, shutil's SameFileError lands in
    # the generic "copy to ... failed:" OSError path, which would also match
    # a bare "same file" substring (mutation-pinned 2026-10-20).
    assert "nothing to copy" in out
    assert src.read_bytes() == PDF_BYTES


async def test_copy_file_refuses_self_via_symlink_dest(root):
    # dest resolves back onto src — same inode, same refusal (and the bytes
    # survive: copy2 onto itself truncates only after this guard).
    src = _mk(root, "a.bin")
    (root / "b.bin").symlink_to(src)
    out = await CopyFileTool().run(
        {"src": str(src), "dest": str(root / "b.bin"), "overwrite": True}
    )
    assert out.startswith("Error:")
    assert "nothing to copy" in out
    assert src.read_bytes() == PDF_BYTES


async def test_copy_file_refuses_directory_dest(root):
    src = _mk(root, "a.bin")
    (root / "adir").mkdir()
    # overwrite=true so this exercises the directory guard, not the earlier
    # clobber refusal a bare existing dest would hit (mutation-pinned).
    out = await CopyFileTool().run(
        {"src": str(src), "dest": str(root / "adir"), "overwrite": True}
    )
    assert out.startswith("Error:")
    assert "directory" in out
    assert not (root / "adir" / "a.bin").exists()  # no shutil copy-into-dir


async def test_copy_file_missing_src(root):
    out = await CopyFileTool().run({"src": str(root / "nope.bin"), "dest": str(root / "x.bin")})
    assert out.startswith("Error:")
    assert "no such file" in out
    assert not (root / "x.bin").exists()


async def test_copy_file_refuses_directory_src(root):
    (root / "adir").mkdir()
    out = await CopyFileTool().run({"src": str(root / "adir"), "dest": str(root / "x.bin")})
    assert out.startswith("Error:")
    assert "directory" in out


async def test_copy_file_refuses_non_regular_src(root):
    """A device is not a file copy: /dev/null would 'succeed' as a 0-byte file
    and /dev/zero would spin forever filling the disk. Only regular files are
    copyable (isfile follows symlinks, so links to real files still work)."""
    devnull = "/dev/null"
    if not os.path.exists(devnull):
        pytest.skip("no /dev/null on this host")
    out = await CopyFileTool().run({"src": devnull, "dest": str(root / "null.bin")})
    assert out.startswith("Error:")
    assert "not a regular file" in out, (
        "shutil.copy2 accepts CHARACTER DEVICES (/dev/zero fills the disk) — any copy tool needs os.path.isfile(src)"
    )
    assert not (root / "null.bin").exists()


async def test_copy_file_does_not_copy_uploads_sidecar(root):
    # The .name sidecar is upload-store metadata keyed to the hash name in
    # uploads/ (only media.resolve() reads it there); the copy's own name is
    # the requested one. Recorded under DECISIONS in the build report.
    up = root / "uploads"
    up.mkdir()
    (up / "abcd1234.pdf").write_bytes(PDF_BYTES)
    (up / "abcd1234.pdf.name").write_text("invoice.pdf")
    dest = root / "invoice.pdf"
    out = await CopyFileTool().run({"src": str(up / "abcd1234.pdf"), "dest": str(dest)})
    assert "Copied" in out
    assert dest.read_bytes() == PDF_BYTES
    assert not (root / "invoice.pdf.name").exists()
    assert not (root / "abcd1234.pdf.name").exists()
