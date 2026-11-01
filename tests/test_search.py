"""search_files tool: name/content matching, secret exclusion, caps."""

from __future__ import annotations

import pytest

from harness.core.policy import ActionClass
from harness.tools.search import SearchFilesTool


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def main():\n    launch_rocket()\n")
    (tmp_path / "src" / "util.py").write_text("# helper\nROCKET = 'saturn'\n")
    (tmp_path / "notes.txt").write_text("remember the Rocket demo\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x00binary rocket")
    # vendor dir must be skipped
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.py").write_text("rocket = 1\n")
    # secrets: hard-denied and sensitive-but-readable
    (tmp_path / ".env").write_text("ROCKET_KEY=hunter2\n")
    (tmp_path / "id_rsa").write_text("PRIVATE rocket KEY\n")
    return tmp_path


async def test_content_search_with_line_numbers(tree):
    out = await SearchFilesTool().run({"root": str(tree), "query": "rocket"})
    assert f"{tree}/src/app.py:2: launch_rocket()" in out
    assert "ROCKET = 'saturn'" in out  # case-insensitive by default
    assert "notes.txt:1" in out


async def test_case_sensitive_and_name_glob(tree):
    out = await SearchFilesTool().run(
        {"root": str(tree), "query": "Rocket", "case_sensitive": True}
    )
    assert "notes.txt" in out and "app.py" not in out

    out = await SearchFilesTool().run({"root": str(tree), "name": "*.py"})
    assert "app.py" in out and "util.py" in out and "notes.txt" not in out
    # glob + query compose
    out = await SearchFilesTool().run(
        {"root": str(tree), "name": "*.py", "query": "saturn"}
    )
    assert "util.py:2" in out and "app.py" not in out


async def test_secrets_binaries_and_vendor_dirs_excluded(tree):
    out = await SearchFilesTool().run({"root": str(tree), "query": "rocket"})
    assert "hunter2" not in out and ".env" not in out  # hard-denied: invisible
    assert "PRIVATE" not in out  # sensitive: content never searched
    assert "secret-class file(s) excluded" in out
    assert "node_modules" not in out  # vendor dir pruned
    assert "logo.png" not in out  # binary skipped

    # Name search: sensitive names may be LISTED (metadata), denied names not.
    out = await SearchFilesTool().run({"root": str(tree), "name": "id_*"})
    assert "id_rsa" in out
    out = await SearchFilesTool().run({"root": str(tree), "name": ".env"})
    assert "No matches" in out


async def test_invalid_regex_falls_back_to_literal(tree):
    out = await SearchFilesTool().run({"root": str(tree), "query": "launch_rocket("})
    assert "app.py:2" in out


async def test_result_cap_and_arg_errors(tree, tmp_path):
    for i in range(30):
        (tree / f"gen{i}.txt").write_text("rocket\n" * 3)
    out = await SearchFilesTool().run(
        {"root": str(tree), "query": "rocket", "max_results": 10}
    )
    assert out.startswith("10 match(es):")
    assert "stopped at 10 results" in out

    assert "give `name`" in await SearchFilesTool().run({"root": str(tree)})
    assert "not a directory" in await SearchFilesTool().run(
        {"root": str(tmp_path / "nope"), "query": "x"}
    )


async def test_denied_root_refused(tmp_path, monkeypatch):
    from pathlib import Path

    home_ssh = Path.home() / ".ssh"
    out = await SearchFilesTool().run({"root": str(home_ssh), "query": "key"})
    assert "error" in out and "off-limits" in out


def test_classification_read_only():
    assert SearchFilesTool.classification is ActionClass.READ_ONLY
