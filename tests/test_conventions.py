"""Static absence/presence pins for load-bearing conventions.

This file pins invariants that can only be enforced via source scanning or
build-time checks — runtime behavior can't prove absence of logic. A scan-only
pin signals that a violation is detected by grep/regex, not by a test's
assertion, and the scan's brittleness is explicitly accepted for the gotcha's
specific risk.

Everything provable behaviorally is pinned behaviorally in other test files.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import gotcha


_repo_root = Path(__file__).resolve().parent.parent


def test_gcal_has_no_timezone_logic():
    """Timezone conversion lives in the agent (convert_time tool), never the MCP
    subprocess. The MCP server gcal.py must NOT import zoneinfo/pytz/etc or call
    astimezone/utcoffset — it returns raw RFC3339 with the server's offset.

    Pinned in tests/test_conventions.py."""
    gcal_path = _repo_root / "mcp_servers/google_workspace/gcal.py"
    text = gcal_path.read_text()
    forbidden = ("zoneinfo", "pytz", "astimezone", "utcoffset")
    found = [word for word in forbidden if word in text]
    assert not found, gotcha(
        "gcal-no-tz-logic",
        f"gcal.py must not import/use {found}; the agent localizes via convert_time"
    )


def test_migrations_never_create_virtual_tables():
    """vec0 VIRTUAL tables are created at runtime by MemoryStore.setup(),
    never declared in migration .sql files — the migration connection has no
    sqlite-vec loaded and will 400 if it encounters CREATE VIRTUAL TABLE.

    Pinned in tests/test_conventions.py."""
    migration_dir = _repo_root / "src/harness/store/migrations"
    for sql_file in sorted(migration_dir.glob("*.sql")):
        text = sql_file.read_text()
        # Check for CREATE VIRTUAL TABLE (case-insensitive).
        match = re.search(r"create\s+virtual\s+table", text, re.IGNORECASE)
        assert not match, gotcha(
            "migrations-no-virtual-tables",
            f"{sql_file.name} contains CREATE VIRTUAL TABLE — vec0 tables must be "
            "runtime-created by MemoryStore.setup() (the migration connection has no "
            "sqlite-vec); and vec0 rejects INSERT OR REPLACE — DELETE first"
        )


def test_migrations_apply_without_sqlite_vec():
    """The migrations table applies via Database.open(), which attaches
    sqlite-vec on demand. If a migration tries to CREATE VIRTUAL TABLE before
    the extension is loaded, Database.open would crash. This test proves the
    migrations can open without it (by using the same fixture as every other
    db-using test).

    Pinned in tests/test_conventions.py."""
    from harness.store.db import Database
    import tempfile

    # The db fixture already does this — opening a tmp DB and applying
    # migrations. This is a named, diagnosable version of what happens
    # implicitly in every db test.
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test.db"
        # If this raises, a migration tried to use sqlite-vec before it was attached.
        # The error would be something like: "no such module: vec0".
        try:
            import asyncio
            asyncio.run(Database.open(db_path))
        except Exception as e:
            pytest.fail(
                f"Database.open() failed (check migrations for CREATE VIRTUAL TABLE): {e}"
            )


def test_gitignore_keeps_token_pattern():
    """The pattern *token*.json in .gitignore keeps OAuth tokens from being
    committed accidentally. Removing it would make .json token files committable.

    Pinned in tests/test_conventions.py."""
    gitignore_path = _repo_root / ".gitignore"
    text = gitignore_path.read_text()
    assert "*token*.json" in text, gotcha(
        "gitignore-token-pattern",
        "removing the *token*.json pattern makes OAuth token files committable"
    )


def test_smoke_fixture_present():
    """The smoke test (scripts/smoke_test.py) reads scratch/hello.txt for its
    passphrase fixture. Don't "clean up" scratch/.

    Pinned in tests/test_conventions.py."""
    hello_path = _repo_root / "scratch/hello.txt"
    assert hello_path.exists(), gotcha(
        "smoke-test-fixture",
        "scratch/hello.txt is the fixture for scripts/smoke_test.py — don't "
        "\"clean up\" scratch/"
    )
    text = hello_path.read_text()
    assert "passphrase" in text, gotcha(
        "smoke-test-fixture",
        "scratch/hello.txt must contain 'passphrase' for the smoke test fixture"
    )


_PUBLISH_CHECK = _repo_root / "scripts/publish_check.sh"

# One canary per leak class publish_check.sh claims to catch. The public repo is
# a snapshot of this one, and publishing is the only irreversible step in the
# whole workflow — a verifier that has quietly stopped matching would report
# "clean" and mean nothing. Each string below must be reported by its pattern id.
#
# Only the GENERIC patterns appear here. The owner-identifying ones live in
# scripts/publish_check.local (denylisted from the snapshot) precisely so they
# are not written down in a published file — and that includes this one. The
# mechanism that loads them is covered by
# test_publish_check_loads_owner_patterns_from_the_local_file instead.
_LEAK_CANARIES = {
    "lan-address": "the router answered on 192.168.1.1",
    "wsl-address": "the Windows host is 172.28.128.1",
    "anthropic-key": "ANTHROPIC_API_KEY=sk-ant-api03-AbCdEfGh",
    "google-key": "GOOGLE_MAPS_API_KEY=AIzaSyABCDEFGHIJ",
    "private-key": "-----BEGIN EC PRIVATE KEY-----",
    "tailnet-host": "serve proxy https://desktop-abc123.tail0z9x8.ts.net",
}


def _run_check(tree: Path, script: Path = _PUBLISH_CHECK) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script), str(tree)], capture_output=True, text=True, timeout=120
    )


@pytest.fixture
def clean_tree(tmp_path):
    """A minimal output tree that publish_check.sh must pass."""
    tree = tmp_path / "out"
    (tree / ".git").mkdir(parents=True)
    (tree / "README.md").write_text(
        "# Sidekick\n\nSet HARNESS_PUBLIC_URL=https://<host>.<tailnet>.ts.net\n"
    )
    return tree


def test_publish_check_passes_a_clean_tree(clean_tree):
    """The verifier must not cry wolf: documented placeholders are not leaks.

    Pinned in tests/test_conventions.py."""
    result = _run_check(clean_tree)
    assert result.returncode == 0, (
        f"publish_check.sh failed a clean tree:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("pattern_id,canary", sorted(_LEAK_CANARIES.items()))
def test_publish_check_catches_every_leak_class(clean_tree, pattern_id, canary):
    """Each leak class publish_check.sh advertises is actually detected.

    Pinned in tests/test_conventions.py."""
    (clean_tree / "docs").mkdir()
    (clean_tree / "docs" / "note.md").write_text(f"context line\n{canary}\n")

    result = _run_check(clean_tree)
    assert result.returncode == 1, gotcha(
        "publish-check-coverage",
        f"publish_check.sh returned {result.returncode} for a planted "
        f"{pattern_id} leak ({canary!r}); expected 1:\n{result.stdout}{result.stderr}",
    )
    assert pattern_id in result.stdout, gotcha(
        "publish-check-coverage",
        f"a planted {pattern_id} leak was not attributed to that pattern:\n{result.stdout}",
    )
    assert "docs/note.md:2" in result.stdout, gotcha(
        "publish-check-coverage",
        f"publish_check.sh must report file:line for {pattern_id}:\n{result.stdout}",
    )


def test_publish_check_flags_machine_label_before_placeholder_tailnet(clean_tree):
    """Every label of a `*.ts.net` hostname is checked, not just the last one.

    A Tailscale FQDN is `<machine>.<tailnet>.ts.net` and the machine label is
    usually the identifying part (it defaults to the hostname). A real machine
    name in front of a placeholder tailnet label must still be a hit.

    Pinned in tests/test_conventions.py."""
    (clean_tree / "docs").mkdir()
    (clean_tree / "docs" / "note.md").write_text(
        "serve proxy https://laptop-xyz9.your-tailnet.ts.net\n"
    )
    result = _run_check(clean_tree)
    assert result.returncode == 1 and "tailnet-host" in result.stdout, gotcha(
        "publish-check-coverage",
        "a non-placeholder machine label hiding in front of a placeholder "
        f"tailnet label was not flagged:\n{result.stdout}{result.stderr}",
    )


@pytest.mark.parametrize(
    "name", ["harness.db", "vapid_private.pem", "google_token.json", "server.key"]
)
def test_publish_check_catches_credential_filenames(clean_tree, name):
    """scratch/ is a live write root that accumulates keys and databases; the
    verifier must not depend on .gitignore having caught them.

    Pinned in tests/test_conventions.py."""
    (clean_tree / name).write_bytes(b"")
    result = _run_check(clean_tree)
    assert result.returncode == 1, gotcha(
        "publish-check-coverage",
        f"publish_check.sh shipped {name}:\n{result.stdout}{result.stderr}",
    )
    assert name in result.stdout


def test_publish_check_aborts_rather_than_passing_on_a_broken_pattern(
    clean_tree, tmp_path
):
    """A pattern that no longer compiles must ABORT, never report "clean".

    A silently-neutered regex is worse than no check at all: it produces a green
    verdict on a tree nobody scanned. The script's grep wrapper returns 2 on a
    grep error and every caller propagates it.

    Pinned in tests/test_conventions.py."""
    broken = tmp_path / "broken_check.sh"
    text = _PUBLISH_CHECK.read_text()
    original = r"172\\.(28|30)\\.[0-9]"
    assert original in text, "the wsl-address pattern moved; update this test"
    broken.write_text(text.replace(original, r"172\\.(28|30\\.[0-9]"))  # unbalanced (

    result = _run_check(clean_tree, script=broken)
    assert result.returncode == 2, gotcha(
        "publish-check-coverage",
        "publish_check.sh must exit 2 when a pattern fails to compile, not "
        f"report a verdict (got {result.returncode}):\n{result.stdout}{result.stderr}",
    )


def test_publish_check_loads_owner_patterns_from_the_local_file(clean_tree, tmp_path):
    """Owner-identifying patterns come from scripts/publish_check.local.

    They are kept out of the published script on purpose: a verifier that spells
    out "the owner's surname is X and their mail provider is Y" publishes the
    association it exists to protect. This tests the loading mechanism with a
    synthetic pattern — the real ones are just data down the same path.

    Pinned in tests/test_conventions.py."""
    # The script derives repo_root from its own location, so mirror the layout.
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "check.sh"
    script.write_text(_PUBLISH_CHECK.read_text())
    (scripts_dir / "publish_check.local").write_text(
        "CONTENT_PATTERNS+=( $'house-codename\\tvermilion-badger\\ta private codename' )\n"
    )
    (clean_tree / "notes.md").write_text("filed under vermilion-badger\n")

    result = _run_check(clean_tree, script=script)
    assert "loaded scripts/publish_check.local" in result.stdout, gotcha(
        "publish-check-coverage",
        f"publish_check.sh did not source its local pattern file:\n{result.stdout}",
    )
    assert result.returncode == 1 and "house-codename" in result.stdout, gotcha(
        "publish-check-coverage",
        f"a pattern from publish_check.local was not enforced:\n{result.stdout}",
    )


def test_publish_check_local_is_never_published():
    """The owner-pattern file must be denylisted from the snapshot, and the
    published script must not name the owner itself.

    Pinned in tests/test_conventions.py."""
    publisher = (_repo_root / "scripts/publish_public.sh").read_text()
    assert "publish_check\\.local" in publisher, gotcha(
        "publish-check-coverage",
        "scripts/publish_check.local is not denylisted in publish_public.sh — "
        "publishing it would give away the owner association the check protects",
    )
    # The published script carries only generic patterns.
    script = _PUBLISH_CHECK.read_text()
    for pattern_id in ("owner-name", "owner-email"):
        assert f"$'{pattern_id}" not in script, gotcha(
            "publish-check-coverage",
            f"{pattern_id} is defined in the published publish_check.sh; it belongs "
            "in scripts/publish_check.local",
        )


def test_publish_check_skips_only_the_gitignored_snapshot_config(clean_tree):
    """The snapshot's PRESERVE'd Claude Code config is exempt — but only the
    parts .gitignore actually keeps out of the public repo.

    settings.local.json and party.json are denylisted from the manifest AND
    gitignored in the snapshot, so no `git add` can publish them; they collect
    absolute home paths from ordinary use (an approved `Bash(...)` permission
    embeds one) and would otherwise fail every publish. settings.json is NOT
    gitignored there, so it really can be committed and must stay scanned —
    that half is what stops this exemption from quietly widening.

    Pinned in tests/test_conventions.py."""
    canary = _LEAK_CANARIES["lan-address"]
    cfg = clean_tree / ".claude"
    cfg.mkdir()

    (cfg / "settings.local.json").write_text(f'{{"note": "{canary}"}}\n')
    (cfg / "party.json").write_text(f'{{"note": "{canary}"}}\n')
    result = _run_check(clean_tree)
    assert result.returncode == 0, gotcha(
        "publish-check-coverage",
        "publish_check.sh flagged the snapshot's own gitignored config, which "
        f"no publish can carry into the public repo:\n{result.stdout}",
    )

    (cfg / "settings.json").write_text(f'{{"note": "{canary}"}}\n')
    result = _run_check(clean_tree)
    assert result.returncode == 1 and "settings.json" in result.stdout, gotcha(
        "publish-check-coverage",
        ".claude/settings.json is committable in the snapshot, so a leak in it "
        f"must still be reported:\n{result.stdout}",
    )


def test_publish_check_is_executable_and_reachable_from_publish_public():
    """publish_public.sh must actually run the verifier — the mirror step is
    only safe because a hit fails the publish.

    Pinned in tests/test_conventions.py."""
    assert _PUBLISH_CHECK.exists(), "scripts/publish_check.sh is the release gate"
    publisher = _repo_root / "scripts/publish_public.sh"
    assert publisher.exists()
    assert "publish_check.sh" in publisher.read_text(), gotcha(
        "publish-check-coverage",
        "publish_public.sh no longer invokes publish_check.sh on the output tree",
    )
    assert shutil.which("bash"), "these checks need bash"


def test_no_builtins_hash_calls():
    r"""Deterministic test vectors and idempotency keys must use hashlib, not
    builtins.hash() — the latter is salted per-process.

    Pinned in tests/test_conventions.py."""
    # Scan only production code (src/ + mcp_servers/), not tests.
    dirs_to_scan = [
        _repo_root / "src",
        _repo_root / "mcp_servers",
    ]

    violations = []
    for scan_dir in dirs_to_scan:
        if not scan_dir.exists():
            continue
        for py_file in scan_dir.rglob("*.py"):
            text = py_file.read_text()
            for line_num, line in enumerate(text.split("\n"), start=1):
                # Skip def __hash__ definitions (allowed).
                if "def __hash__" in line:
                    continue

                # Skip comment-only lines.
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue

                # Only check the code part (before any trailing comment).
                code_part = line.split("#")[0]

                # Skip if the line is only whitespace after comment removal.
                if not code_part.strip():
                    continue

                # Match bare hash( or builtins.hash( in code.
                if re.search(r"(?<![\w.])hash\(", code_part):
                    violations.append(f"{py_file.relative_to(_repo_root)}:{line_num}")
                elif re.search(r"builtins\.hash\(", code_part):
                    violations.append(f"{py_file.relative_to(_repo_root)}:{line_num}")

    assert not violations, gotcha(
        "no-builtins-hash",
        f"found hash() calls (use hashlib instead — builtins.hash is salted per "
        f"process, so derived ids/vectors change every run):\n" + "\n".join(violations)
    )


def test_fact_categories_synced_across_surfaces():
    """CATEGORIES (memory/store.py) is the source of truth for fact
    categories, but two web UI surfaces hardcode the list (settings.js
    edit-form select, index.html add-fact options) and the extraction prompt
    must define every category or the extractor model will never use it.

    Pinned in tests/test_conventions.py."""
    from harness.memory.extraction import EXTRACTION_SYSTEM
    from harness.memory.store import CATEGORIES

    js = (_repo_root / "src/harness/gateway/webui/settings.js").read_text()
    match = re.search(r"FACT_CATEGORIES = \[(.*?)\];", js, re.S)
    assert match, "FACT_CATEGORIES array not found in settings.js"
    js_cats = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert js_cats == CATEGORIES, gotcha(
        "fact-categories-sync",
        f"settings.js FACT_CATEGORIES {js_cats} != store.CATEGORIES {CATEGORIES} "
        "— categories live in FOUR places (store.CATEGORIES, settings.js, "
        "index.html options, EXTRACTION_SYSTEM); only profile/preference are always-on",
    )

    html = (_repo_root / "src/harness/gateway/webui/index.html").read_text()
    select = re.search(
        r'<select id="memory-add-category">(.*?)</select>', html, re.S
    )
    assert select, "#memory-add-category select not found in index.html"
    html_cats = tuple(re.findall(r'<option value="([^"]+)">', select.group(1)))
    assert html_cats == CATEGORIES, gotcha(
        "fact-categories-sync",
        f"index.html options {html_cats} != store.CATEGORIES {CATEGORIES}",
    )

    undefined = [c for c in CATEGORIES if f"- {c}:" not in EXTRACTION_SYSTEM]
    assert not undefined, gotcha(
        "fact-categories-sync",
        f"EXTRACTION_SYSTEM has no definition line for {undefined}",
    )


# One cohesive stateful class (the CDP tab: transport + targets + dialogs +
# capture) — splitting it further would serve the number, not the code.
_LOC_EXEMPT = {"src/harness/tools/_browser_session.py"}


def test_python_files_under_loc_cap():
    """CLAUDE.md: files over ~400 lines are a smell. The cap is pinned at 420
    (comment headroom over the guideline); growth past it means a split, or a
    deliberate exemption below with a reason.

    Pinned in tests/test_conventions.py."""
    cap = 420
    offenders = []
    for scan_dir in (_repo_root / "src", _repo_root / "mcp_servers"):
        for py in scan_dir.rglob("*.py"):
            rel = str(py.relative_to(_repo_root))
            if rel in _LOC_EXEMPT:
                continue
            n = len(py.read_text().splitlines())
            if n > cap:
                offenders.append(f"{rel}: {n}")
    assert not offenders, gotcha(
        "loc-cap",
        "files over the 420-line cap (split, or exempt with a reason):\n"
        + "\n".join(sorted(offenders)),
    )


def test_webui_files_under_loc_cap():
    """The same cap guards the web UI — its JS/CSS grew into a 905-line app.js
    and a 601-line style.css precisely because nothing checked. Vendored
    minified libs are not ours to split.

    Pinned in tests/test_conventions.py."""
    cap = 420
    webui = _repo_root / "src/harness/gateway/webui"
    offenders = []
    for f in sorted(webui.glob("*.js")) + sorted(webui.glob("*.css")):
        if f.name.endswith(".min.js"):
            continue
        n = len(f.read_text().splitlines())
        if n > cap:
            offenders.append(f"src/harness/gateway/webui/{f.name}: {n}")
    assert not offenders, gotcha(
        "loc-cap",
        "web UI files over the 420-line cap (split per webui-change skill):\n"
        + "\n".join(offenders),
    )


def test_webui_modules_import_sibling_exports_they_call():
    """ES modules are strict, so calling another module's export without
    importing it throws ReferenceError AT THE CALL SITE — no hint at load time.
    The v2 split shipped exactly that: app.js called `updateRenameBtn()` on the
    chat stream's `session` frame, which killed every turn's POST read loop and
    silently moved all chat rendering to /api/events (and made Stop wipe the
    answer, since pendingChat was then set on every turn).

    Pinned in tests/test_conventions.py."""
    webui = _repo_root / "src/harness/gateway/webui"
    files = {p.name: p.read_text() for p in sorted(webui.glob("*.js"))}

    def names_after_kw(text: str, kw: str) -> set[str]:
        out = set(re.findall(rf"^export\s+(?:async\s+)?{kw}\s+(\w+)", text, re.M))
        return out

    exports = {}
    for name, text in files.items():
        found = names_after_kw(text, "function") | names_after_kw(text, "const")
        found |= names_after_kw(text, "let") | names_after_kw(text, "class")
        for group in re.findall(r"^export\s*\{([^}]*)\}", text, re.M):
            found |= {p.split(" as ")[-1].strip() for p in group.split(",") if p.strip()}
        exports[name] = found

    offenders = {}
    for name, text in files.items():
        # Prose is full of `foo()` mentions ("must call unpin() first").
        code = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", " ", code)
        imported: set[str] = set()
        for group in re.findall(r"import\s*\{([^}]*)\}", text, re.S):
            imported |= {p.split(" as ")[-1].strip() for p in group.split(",") if p.strip()}
        defined = set(re.findall(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", code, re.M))
        defined |= set(re.findall(r"(?:^|[\s;{(])(?:const|let|var|class)\s+(\w+)", code))
        alien = set().union(*[e for n, e in exports.items() if n != name] or [set()])
        called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code))
        bad = sorted(called & (alien - imported - defined - exports[name]))
        if bad:
            offenders[name] = bad
    assert not offenders, gotcha(
        "webui-sibling-import",
        "web UI modules calling an export they never imported (ReferenceError "
        f"at runtime): {offenders}"
    )


def test_no_import_cycle_between_thinking_and_transcript():
    """BK-019 criterion 5: the thinking.js ↔ transcript.js ES-module cycle —
    and its sibling legs (thinking↔pills, pills→transcript, which made
    {thinking, pills, transcript} ONE strongly-connected tangle) — broke
    STRUCTURALLY: transcript.js is the only module importing all three and
    it INJECTS the neighbor-closers (wireThinking/wirePills in its module
    body — the replay.js injected-painter precedent). Nothing moved between
    modules, so there is no re-export to shadow; the injected bindings carry
    the real exports' names, so re-adding an import is a duplicate-
    declaration SyntaxError, never a silent second copy (the extract-and-
    re-export gotcha). What remains is wiring, and this pin holds it:

    (1) the webui import graph, DERIVED from every module's real static
        imports AND re-exports (`export … from` is a real evaluation edge —
        and the plan's named banned workaround), is acyclic. Comments are
        stripped — prose must not mint edges. Graph-derived, not a string
        match on two file names: a rename walks past a string match, not
        past this;
    (2) no back edge survives inside the segment triangle — thinking.js and
        pills.js import NEITHER each other NOR transcript.js (transcript →
        leaves stays: it is the painter, the direction the criterion keeps);
    (3) the wiring exists and passes the RIGHT closers: transcript.js calls
        wireThinking with finalizeAgentBubble + breakPillRow and wirePills
        with endThinking + finalizeAgentBubble, and each leaf exports its
        wire fn. Unbound bindings throw TypeError AT THE CALL (the
        webui-sibling-import shape) — the probe's page-error list catches
        that live; this conjunct catches it offline;
    (4) no leaf DEFINES a closer as a function — the extract-and-re-export
        gotcha's leftover-def variant, which would run instead of the
        injected one and stay green until the two copies diverged.

    Pinned in tests/test_conventions.py."""
    webui = _repo_root / "src/harness/gateway/webui"
    files = {
        p.name: p.read_text()
        for p in sorted(webui.glob("*.js"))
        if not p.name.endswith(".min.js")
    }

    def code_of(name: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", files[name], flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", " ", text)

    # All product imports are static sibling imports; a dynamic import()
    # would escape the graph and silently stale this pin — fail loudly.
    IMPORT_RE = re.compile(
        r"""^\s*import\s+(?:[\w*{},\s]+\s+from\s+)?["'](\./[\w.\-]+\.js)["']""",
        re.M,
    )
    # `export … from` is a re-export edge: it evaluates the target exactly like
    # an import, so the plan's banned "re-export indirection that leaves the
    # cycle alive at runtime" would otherwise be invisible to this pin.
    REEXPORT_RE = re.compile(
        r"""^\s*export\s+(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+["'](\./[\w.\-]+\.js)["']""",
        re.M,
    )
    graph: dict[str, set[str]] = {}
    for name in files:
        code = code_of(name)
        assert not re.search(r"\bimport\s*\(", code), gotcha(
            "webui-import-cycle",
            f"{name} gained a dynamic import() — the static graph this pin "
            "derives cannot see it; extend the pin before shipping lazy "
            "imports, or a cycle hides there",
        )
        graph[name] = {dep[2:] for dep in
                       IMPORT_RE.findall(code) + REEXPORT_RE.findall(code)}

    # A scanner that finds no edges is broken, not clean: the painter must
    # still import both segment modules (the one-way direction the criterion
    # keeps). Without this, a rename/parse change turns the whole pin into a
    # vacuous green over an edgeless graph.
    assert {"thinking.js", "pills.js"} <= graph.get("transcript.js", set()), gotcha(
        "webui-import-cycle",
        "the derived graph shows transcript.js importing neither segment "
        "module — the scanner found no edges (renamed files or an import "
        "shape it cannot parse); a pin that scans nothing must not read green",
    )

    # (1) DFS for ANY cycle (WHITE/GRAY/BLACK, sorted for a stable report).
    color = {name: 0 for name in graph}  # 0 unvisited, 1 on-stack, 2 done
    cycle: list[str] | None = None
    for start in sorted(graph):
        if color[start]:
            continue
        color[start] = 1
        stack: list[tuple[str, object]] = [(start, iter(sorted(graph[start])))]
        path = [start]
        while stack and cycle is None:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                color[node] = 2
                stack.pop()
                path.pop()
            elif color[nxt] == 1:
                cycle = path[path.index(nxt):] + [nxt]
            elif not color[nxt]:
                color[nxt] = 1
                path.append(nxt)
                stack.append((nxt, iter(sorted(graph[nxt]))))
        assert cycle is None, gotcha(
            "webui-import-cycle",
            "the webui import graph has a cycle: "
            + " -> ".join(cycle)
            + " — BK-019 broke thinking.js ↔ transcript.js by injecting the "
            "neighbor-closers in transcript.js's module body; a re-export "
            "indirection does NOT count (the cycle stays live at runtime). "
            "Inject, or move the calling branch, or say why the graph owes "
            "a cycle at all",
        )

    # (2) The segment triangle is one-way from transcript.js; the criterion
    # pair shares ZERO edges, and neither leaf reaches back at all.
    back = sorted(
        f"{s} -> {d}"
        for s, d in (
            ("thinking.js", "transcript.js"),
            ("pills.js", "transcript.js"),
            ("thinking.js", "pills.js"),
            ("pills.js", "thinking.js"),
        )
        if d in graph[s]
    )
    assert not back, gotcha(
        "webui-import-cycle",
        f"segment-triangle back edge(s) {back} — the leaves close their "
        "neighbors through the injected bindings; importing is how the "
        "tangle was born",
    )

    # (2b) Wiring happens in transcript.js's module body, so a module that
    # imports a leaf WITHOUT importing transcript evaluates it in a realm
    # where the closers are still null — the sibling-import failure shape,
    # moved to wiring time. Today transcript is the only importer; more are
    # fine only if they also evaluate the wiring module.
    for leaf in ("thinking.js", "pills.js"):
        for importer in sorted({n for n, deps in graph.items() if leaf in deps}
                               - {"transcript.js"}):
            assert "transcript.js" in graph[importer], gotcha(
                "webui-import-cycle",
                f"{importer} imports {leaf} but never evaluates transcript.js "
                "— the injected closers are still null in that realm, so the "
                "first segment/pill call would throw TypeError (import "
                "transcript.js for its wiring, or move the wiring out of a "
                "module body)",
            )

    # (3) The wiring exists and names the right closers (object literals).
    tr = code_of("transcript.js")
    for call, names in (
        ("wireThinking", ("finalizeAgentBubble", "breakPillRow")),
        ("wirePills", ("endThinking", "finalizeAgentBubble")),
    ):
        assert re.search(
            rf"{call}\s*\(\s*\{{[^}}]*\b{names[0]}\b[^}}]*\b{names[1]}\b", tr
        ), gotcha(
            "webui-import-cycle",
            f"transcript.js no longer calls {call}({{ {names[0]}, "
            f"{names[1]} }}) — the injected bindings stay null and the "
            "thinking strip / pill row throws TypeError at its FIRST call "
            "at runtime (offline half; the probe's page-error list is the "
            "live half)",
        )
    for leaf, wire in (("thinking.js", "wireThinking"), ("pills.js", "wirePills")):
        assert re.search(
            rf"^export function {wire}\b", files[leaf], re.M
        ), gotcha(
            "webui-import-cycle",
            f"{leaf} lost its {wire} export — transcript.js's wiring call "
            "would then be the un-imported sibling call (throws at the call)",
        )
    # (4) No leaf may DEFINE an injected closer locally — the shadow variant.
    for leaf, names in (
        ("thinking.js", ("finalizeAgentBubble", "breakPillRow")),
        ("pills.js", ("endThinking", "finalizeAgentBubble")),
    ):
        for fn in names:
            assert not re.search(
                rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{fn}\b",
                code_of(leaf),
                re.M,
            ), gotcha(
                "extract-and-re-export",
                f"{leaf} defines its own function {fn} — it shadows the "
                "injected binding and the suite stays green until the two "
                "copies diverge (planted and reproduced 2026-10-22)",
            )


def test_follow_any_press_in_the_log_unpins_immediately():
    """Chat scroll unpins the instant the reader presses the log — a finger
    (touchstart, 2026-10-13) or a mouse/pen (pointerdown, BK-029). The user
    reaffirmed this on 2026-10-31 ("any tapping of screen to stop the
    following ... user control") over a tap-vs-scroll hold + slop: a bare
    tap mid-stream MUST stop following. The touch history behind it: the
    earlier fixes unpinned on touchmove (drag threshold, then 0px still needs
    the browser to emit a move event, and iOS has a deadzone before that
    fires) — during fast streaming the spring kept yanking content while the
    thumb was still resting, so the user had to start dragging to break free.
    Source pin; the browser half is tests/fixtures/scroll_follow
    (touch-tap-unpins, mouse-click-unpins).

    Pinned in tests/test_conventions.py."""
    js = (_repo_root / "src/harness/gateway/webui/follow.js").read_text()
    assert "DRAG_UNPIN" not in js and "TOUCH_SLOP" not in js, gotcha(
        "follow-touch-unpin-no-threshold",
        "follow.js defines a touch threshold (DRAG_UNPIN/TOUCH_SLOP) — any "
        "press must unpin immediately, a tap included (user, 2026-10-31)")
    # Prose mentions handlers too; scan code only.
    code = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    code = re.sub(r"(?m)//.*$", " ", code)

    def listener(event):
        m = re.search(
            rf'addEventListener\(\s*"{event}"\s*,\s*\(\s*e\s*\)\s*=>\s*\{{(.*?)\n\}}',
            code, re.S)
        assert m, gotcha(
            "follow-touch-unpin-on-touch",
            f"{event} listener not found in follow.js — the unpin-on-press "
            "handler must stay (touchstart also feeds the iOS :active state)")
        return m.group(1)

    start = listener("touchstart")
    assert "unpin()" in start and "touchY0 =" in start, gotcha(
        "follow-touch-unpin-on-touch",
        "the touchstart handler must unpin() (and set touchY0, maybeRepin's "
        "finger guard) — a bare tap mid-stream stops following")
    down = listener("pointerdown")
    assert "unpin()" in down, gotcha(
        "follow-touch-unpin-on-touch",
        "the pointerdown handler no longer calls unpin() — a mouse/pen click "
        "in the log must stop following like a tap does")
    assert '"mouse"' not in down and '"pen"' not in down, gotcha(
        "follow-touch-unpin-on-touch",
        "pointerdown must exclude only touch pointers (touchstart owns them) — "
        "gating on one pointer type drops mouse or pen")
    # A touchmove unpin would re-introduce the drag-the-spring fight, and a
    # hold in frame()/follow() is the rejected tap-vs-scroll design.
    assert not re.search(r'addEventListener\(\s*"touchmove"', code), gotcha(
        "follow-touch-unpin-on-touch",
        "follow.js re-added a touchmove unpin — the drag-to-unpin approach is "
        "what trapped the user at the bottom")
    # The press-unpin reveals "↓ Latest" UNDER the finger, and a tap's click is
    # hit-tested at the lift: an ungated pill click re-pins that same tap.
    jump = re.search(r'jumpBtn\.addEventListener\(\s*"click"\s*,\s*\(\s*e\s*\)\s*=>\s*\{(.*?)\n\}', code, re.S)
    assert jump and re.search(r"pressOffPill\s*&&\s*e\.detail\s*!==\s*0", jump.group(1)), gotcha(
        "follow-pill-ghost-click",
        "the #chat-jump click must act only on a press that started on the pill "
        "(pressOffPill false) or with no press at all (keyboard / VoiceOver) — else a tap on the newest line "
        "unpins, the pill appears under the finger, and the tap's own click re-pins "
        "(scroll_follow press-under-pill-stays-unpinned, red on both engines)")


def test_transcript_has_no_vertical_inner_scroller():
    """BK-031 (plan decision D3): the transcript has ONE vertical scroller,
    #chat-log. The thinking body (40vh) and the tool drawer (50vh) used to be
    inner scroll boxes with overscroll-behavior: contain — on iOS they trapped
    swipes, and a wheel over an open drawer scrolled THAT box to its end and
    never chained to the log (measured 86-711 px short of the bottom). Long
    bodies now flow at full height, clipped to a preview by showall.js with a
    "Show all" button. Allowlist, not a denylist: a new `overflow(-y): auto`
    anywhere in chat.css must be argued into the list. Horizontal-only
    scrollers (wide code blocks, tables) stay allowed. The ?recoverdebug
    capture <pre> (events.js, inline 40vh) is the one deliberate exception:
    debug-only, pinned by test_recover_debug_capture_reads_on_the_phone.

    Pinned in tests/test_conventions.py."""
    webui = _repo_root / "src/harness/gateway/webui"
    css = re.sub(r"/\*.*?\*/", " ", (webui / "chat.css").read_text(), flags=re.S)
    vertical_ok = {"#chat-log", ".modal-fields", "#chat-input"}  # the log; the modal; the composer
    horizontal_ok = {".md pre", ".md-table-wrap"}
    offenders = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sel = " ".join(sel.split())
        for prop, val in re.findall(r"(overflow(?:-[xy])?)\s*:\s*([^;]+)", body):
            if not re.search(r"\b(auto|scroll)\b", val):
                continue
            allowed = vertical_ok if prop != "overflow-x" else vertical_ok | horizontal_ok
            if sel not in allowed:
                offenders.append(f"{sel} {{ {prop}: {val.strip()} }}")
    assert not offenders, gotcha(
        "transcript-one-scroller",
        "chat.css grew a scroll box inside the transcript — on iOS it traps the swipe "
        "and a wheel over it never reaches #chat-log; clip it with showall.js "
        f"(makeClip) instead: {offenders}",
    )
    clip = re.search(r"\.clip \{([^}]*)\}", css)
    assert clip and re.search(r"overflow:\s*clip", clip.group(1)), gotcha(
        "transcript-one-scroller",
        "`.clip` must be overflow: clip — `hidden` is still a scroll container "
        "(focus/find-in-page can scroll a preview out of place), `auto` is the trap itself",
    )
    for name, box in (("thinking.js", "text"), ("pills.js", "body")):
        js = (webui / name).read_text()
        assert re.search(rf"\.append\({box}, makeClip\({box}\)\)", js), gotcha(
            "transcript-one-scroller",
            f"{name} no longer clips its long body with makeClip — with the scroll box "
            "gone, a long body would just run on with no preview",
        )


def test_chat_log_has_no_rubber_band():
    """BK-040: #chat-log is `overscroll-behavior: none`, not `contain`. contain
    stops chaining but KEEPS the element's own rubber-band, and on the iPad a
    trackpad push past the bottom bounced against follow.js's pinned scrollTop
    writes — a visible jitter. The user's rule: at the end it just stops.

    Pinned in tests/test_conventions.py."""
    css = re.sub(r"/\*.*?\*/", " ", (_repo_root / "src/harness/gateway/webui/chat.css").read_text(), flags=re.S)
    log = re.search(r"(?:^|\})\s*#chat-log\s*\{([^}]*)\}", css)
    assert log and re.search(r"overscroll-behavior:\s*none\b", log.group(1)), gotcha(
        "chat-log-no-rubber-band",
        "#chat-log must keep `overscroll-behavior: none` — contain/auto bring back the "
        "bottom bounce that fights the pinned follow writes (jitter on a trackpad push)",
    )


def test_markdown_tables_scroll_via_wrapper_not_display_block():
    """`.md table { display: block }` took tables out of the browser's table
    layout: columns collapsed to min-content, cells stopped aligning, borders
    broke — the iPad-scroll intent now rides the .md-table-wrap div renderMd
    adds AFTER DOMPurify (wrapping pre-sanitize would splice strings over
    untrusted HTML). Source pin — the CSS regression, a wrap step moved before
    sanitize, a regex that matches no table, and a replacer that drops the
    markup it matched must each fail this (2026-10-16).

    Pinned in tests/test_conventions.py."""
    css = (_repo_root / "src/harness/gateway/webui/chat.css").read_text()
    table_rule = re.search(r"\.md table \{([^}]*)\}", css)
    assert table_rule, gotcha(
        "md-table-scroll-wrap", ".md table rule missing from chat.css"
    )
    body = table_rule.group(1)
    assert "display: block" not in body and "overflow-x" not in body, gotcha(
        "md-table-scroll-wrap",
        ".md table re-acquired display:block/overflow-x — that is the bug; "
        "scrolling belongs on the .md-table-wrap div",
    )
    wrap_rule = re.search(r"\.md-table-wrap \{([^}]*)\}", css)
    assert wrap_rule and "overflow-x: auto" in wrap_rule.group(1) and (
        "max-width: 100%" in wrap_rule.group(1)
    ), gotcha(
        "md-table-scroll-wrap",
        ".md-table-wrap rule missing its overflow-x/max-width — wide tables "
        "lost the in-bubble horizontal scroll",
    )
    js = (_repo_root / "src/harness/gateway/webui/dom.js").read_text()
    code = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", " ", code)
    fn = re.search(r"export function renderMd\(.*?\n\}", code, re.S)
    assert fn, gotcha("md-table-scroll-wrap", "renderMd missing from dom.js")
    assert "DOMPurify.sanitize" in fn.group(0), gotcha(
        "md-table-scroll-wrap", "renderMd no longer sanitizes with DOMPurify"
    )
    assert "md-table-wrap" in fn.group(0), gotcha(
        "md-table-scroll-wrap",
        "renderMd no longer wraps tables — the CSS class would go unused "
        "and wide tables stop scrolling",
    )
    assert fn.group(0).index("DOMPurify.sanitize") < fn.group(0).index(
        "md-table-wrap"
    ), gotcha(
        "md-table-scroll-wrap",
        "the table wrap must be applied to DOMPurify's output — moving it "
        "before sanitize splices markup into untrusted HTML",
    )
    # The regex itself carries the fix; a wrap that matches nothing (or drops
    # the markup it matched) leaves the class unused and the table unscrolled.
    wrap = re.search(r"html\.replace\(\s*(/\S*/g)\s*,", fn.group(0))
    assert wrap, gotcha(
        "md-table-scroll-wrap",
        "renderMd's table wrap is no longer a single global regex over "
        "sanitized html — that one line is what adds .md-table-wrap",
    )
    pattern = wrap.group(1)
    tail = "<\\/table>/g"  # the source literal escapes the closing slash
    assert pattern.startswith("/<table") and pattern.endswith(tail), gotcha(
        "md-table-scroll-wrap",
        f"the wrap regex no longer spans <table …> through </table>: {pattern}",
    )
    assert "[^>]*" in pattern, gotcha(
        "md-table-scroll-wrap",
        "the wrap only matches a bare <table> — raw-HTML tables that carry "
        "attributes (border/style/class) then lose the in-bubble scroll",
    )
    assert "\\s\\S" in pattern, gotcha(
        "md-table-scroll-wrap",
        "the wrap regex can no longer cross newlines — marked's own tables are "
        "multi-line, so nothing would ever be wrapped",
    )
    assert "${m}" in fn.group(0), gotcha(
        "md-table-scroll-wrap",
        "the wrap replacer no longer re-emits the matched table markup — the "
        "wrapper must wrap the table, never replace it",
    )


def _css_rules(css: str) -> list[tuple[list[str], str]]:
    """(selector list, body) for every innermost rule — @media wrappers fall
    away because their body holds braces. Comments are stripped first."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return [
        ([s.strip() for s in sel.split(",")], body)
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    ]


def _split_media(css: str) -> tuple[str, list[tuple[str, str]]]:
    """(rules outside any @media, [(condition, block body), …]) — `_css_rules` keeps
    the rules but throws the condition away, and for BK-015 the `max-width: 600px`
    gate is the reason desktop and iPad are untouched."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    outside: list[str] = []
    blocks: list[tuple[str, str]] = []
    i = 0
    for m in re.finditer(r"@media\s+([^{]+?)\s*\{", css):
        depth, j = 1, m.end()
        while j < len(css) and depth:
            depth += (css[j] == "{") - (css[j] == "}")
            j += 1
        outside.append(css[i : m.start()])
        blocks.append((" ".join(m.group(1).split()), css[m.end() : j - 1]))
        i = j
    outside.append(css[i:])
    return "".join(outside), blocks


def test_mobile_md_table_cells_size_on_words_not_glyphs():
    """BK-015 (2026-11-01): `.msg { word-break: break-word }` inherits into `td`/`th`,
    and that alias is a break opportunity for INTRINSIC sizing — a cell's min-content is
    ONE glyph (measured 9.3px). Auto table layout therefore shrank short columns to one
    character per line on the phone (the board table's "Pri" stacked P/r/i; COLLAPSE 12
    at 390px WebKit, 6 on chromium) and `.md-table-wrap`'s scroll never engaged because
    the table shrank to FIT instead of overflowing. The phone block sizes cells on WORDS
    (`word-break: normal` + `overflow-wrap: break-word`) over a 3em column floor, and
    long code spans/URLs break with `overflow-wrap: anywhere`: `break-word` inherited
    from the cell is not enough, since unlike `anywhere` it leaves min-content alone, so
    the token demands its unbroken width and pushes the column out of the bubble.
    Both halves measured in scratch/bk015 (report-fixed.json). The rules must live
    INSIDE `@media (max-width: 600px)` — desktop and iPad keep the sizing they shipped
    with (criterion 4; the iPad's narrowest is 744px).

    Pinned in tests/test_conventions.py."""
    css = (_repo_root / "src/harness/gateway/webui/chat.css").read_text()
    outside, blocks = _split_media(css)
    phone = " ".join(b for cond, b in blocks if re.search(r"max-width:\s*600px", cond))
    assert phone, gotcha(
        "md-table-word-sizing",
        "chat.css lost its @media (max-width: 600px) block — the phone table sizing has "
        "nowhere to live, and living outside the gate it would resize iPad/desktop too",
    )
    rules = _css_rules(phone)
    trap = (
        "the phone lost its word-level cell sizing: `.msg`'s `word-break: break-word` "
        "inherits into td/th and is a break opportunity for intrinsic sizing, so a cell's "
        "min-content is ONE glyph and auto table layout shreds short columns into one "
        "glyph per line (\"Pri\" stacked P/r/i at 390px) while .md-table-wrap's scroll "
        "never engages — the table shrinks to fit instead of overflowing (BK-015)"
    )

    def declared(target: str, prop: str) -> list[str]:
        return [
            m.group(1).strip()
            for sels, body in rules
            if target in sels
            for m in [re.search(rf"{prop}\s*:\s*([^;]+)", body)]
            if m
        ]

    for target in (".md th", ".md td"):
        assert declared(target, "word-break") == ["normal"], gotcha(
            "md-table-word-sizing",
            f"{target} does not set `word-break: normal` in the phone block "
            f"(found {declared(target, 'word-break') or 'nothing'}) — " + trap,
        )
        assert declared(target, "overflow-wrap"), gotcha(
            "md-table-word-sizing",
            f"{target} has no `overflow-wrap` in the phone block — with word-break off, "
            "a token wider than the column would overflow it instead of breaking: " + trap,
        )

    def em(value: str) -> float | None:
        m = re.fullmatch(r"([\d.]+)em", value)
        return float(m.group(1)) if m else None

    floors = [em(v) for t in (".md th", ".md td") for v in declared(t, "min-width")]
    fixed_shape = any(
        ".md-table-wrap table" in sels and re.search(r"table-layout\s*:\s*fixed", body)
        for sels, body in rules
    )
    assert fixed_shape or (
        len(floors) == 2 and all(f and f >= 2 for f in floors)
    ), gotcha(
        "md-table-word-sizing",
        "the phone cells lost their column floor (`min-width` >= 2em on th and td, or the "
        "`table-layout: fixed` shape): word-level sizing alone still leaves a column of "
        "nothing but long code spans a ONE-glyph min-content (starved 90px wrap: 35 line "
        "boxes for a 35-char token with the floor off, ~2 glyphs per line with 3em), and "
        "the floor is also what lifts the short columns off their bare word-min — " + trap,
    )

    anywhere = {
        s for sels, body in rules if re.search(r"overflow-wrap\s*:\s*anywhere", body) for s in sels
    }
    for target in (".md th code", ".md td code", ".md th a", ".md td a"):
        assert target in anywhere, gotcha(
            "md-table-word-sizing",
            f"`{target}` lost `overflow-wrap: anywhere` — the cell's inherited "
            "`break-word` breaks the token but leaves min-content at its unbroken width, "
            "so the column grows to the token and pushes the table out of the bubble "
            "instead of wrapping it (scratch/bk015 report-fixed.json: "
            "noCodeAnywhere / noAnchorAnywhere rows)",
        )

    # The gate is half the fix: the same sizing unconditioned re-sizes every table on
    # the iPad and desktop, which shipped fine (criterion 4).
    assert not any(
        (".md th" in sels or ".md td" in sels)
        and re.search(r"word-break|min-width|overflow-wrap", body)
        for sels, body in _css_rules(outside)
    ), gotcha(
        "md-table-word-sizing",
        "the phone cell sizing moved OUT of the @media (max-width: 600px) gate — "
        "desktop and iPad tables would change sizing with it (criterion 4)",
    )


def test_transcript_tappables_have_no_tap_highlight_and_a_pressed_state():
    """BK-026 (smoothness step 1): WebKit paints a dark rgba(0,0,0,.4) square
    over a tapped pill or thinking header — the "flash" on iPad/iPhone. Every
    tappable in the transcript turns the default highlight off AND carries an
    :active pressed state, or the tap loses its feedback entirely. :active
    only fires on iOS because follow.js has a touchstart listener on #chat-log.
    The browser half is the smoothness probe's tap_highlight sweep (computed
    style on WebKit); this is the offline source pin.

    Pinned in tests/test_conventions.py."""
    webui = _repo_root / "src/harness/gateway/webui"
    rules = [r for f in sorted(webui.glob("*.css")) for r in _css_rules(f.read_text())]
    for target in (".pill", ".think-head", ".md a", ".md summary", ".attach-open"):
        assert any(
            target in sels
            and re.search(r"-webkit-tap-highlight-color:\s*transparent", body)
            for sels, body in rules
        ), gotcha(
            "tap-highlight",
            f"{target} lost `-webkit-tap-highlight-color: transparent` — WebKit "
            "flashes a dark square over it on every tap",
        )
        assert any(
            any(s.startswith(target + ":active") for s in sels) and body.strip()
            for sels, body in rules
        ), gotcha(
            "tap-highlight",
            f"{target} has no :active pressed state — with the highlight off, "
            "a tap on it gives no feedback at all",
        )


def test_session_picker_new_chat_top_insert_and_atomic_swap():
    """Step 2 of the UI glitch sweep (2026-10-16). Two silent dropdown
    regressions: (a) handleChatEvent appendChild()ed a brand-new session at the
    BOTTOM, so it visibly jumped to index 1 when loadSessions() rebuilt the
    sorted list; (b) loadSessions() cleared with `sel.innerHTML = ""` before
    repopulating, so an open picker flashed blank. Both are browser-only JS with
    no JS test tier — source pins, like follow.js and the table wrap.

    Pinned in tests/test_conventions.py."""
    app = (_repo_root / "src/harness/gateway/webui/app.js").read_text()
    sessions = (_repo_root / "src/harness/gateway/webui/sessions.js").read_text()

    def code_only(text: str) -> str:
        c = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", " ", c)

    appc = code_only(app)
    sc = code_only(sessions)
    # (a) the fresh session lands at index 1 — before the current first session.
    # BK-019 Step 4: the insert moved into sessions.js's showNewChatOption —
    # the `pickerOpenNew` effect body both transports' fx maps bind (one
    # implementation, two bindings; design §9 permits re-pointing the pin).
    # app.js carrying its own copy again would be the drift this guards.
    opt = re.search(r"export function showNewChatOption\([^)]*\).*?\n\}", sc, re.S)
    assert opt, gotcha(
        "session-picker-top-insert",
        "showNewChatOption is gone from sessions.js — the pickerOpenNew fx "
        "binding has no single implementation for both transports to share",
    )
    assert re.search(
        r"\.insertBefore\(\s*o\s*,\s*sel\.children\[1\]\s*\|\|\s*null\s*\)", opt.group(0)
    ), gotcha(
        "session-picker-top-insert",
        "showNewChatOption no longer inserts a new session right after "
        "'+ New chat' (index 1, via sel.children[1]) — appending at the bottom "
        "makes it jump when loadSessions() rebuilds the sorted list",
    )
    # And the app.js session branch must not bottom-append the option again —
    # it routes through runstate's T02, whose pickerOpenNew effect reaches the
    # implementation above.
    assert 'ev.type === "session"' in appc and "sessionAnnounce" in appc, gotcha(
        "session-picker-top-insert",
        "app.js no longer routes the `session` frame through sessionAnnounce "
        "— the transport grew its own picker code again",
    )
    branch = appc[
        appc.index('ev.type === "session"') : appc.index('ev.type === "turn_complete"')
    ]
    assert "appendChild" not in branch, gotcha(
        "session-picker-top-insert",
        "the session branch appendChild()s the new option — that is the "
        "bottom-insert bug the fix replaced",
    )

    # (b) loadSessions() builds off-DOM and swaps in one call (no blank frame).
    # pwa-accurate-resume Step 2: the signature gained an OPTIONAL preloaded
    # list (boot-restore renders the list it already fetched) — deliberately
    # widened here in the same commit; the substance pinned below (atomic
    # swap, off-DOM build, array return) is untouched.
    fn = re.search(r"export async function loadSessions\([^)]*\).*?\n\}", sc, re.S)
    assert fn, gotcha(
        "session-picker-atomic-swap", "loadSessions missing from sessions.js"
    )
    body = fn.group(0)
    assert "innerHTML" not in body, gotcha(
        "session-picker-atomic-swap",
        "loadSessions still clears the dropdown with innerHTML — an open "
        "picker flashes blank mid-rebuild",
    )
    assert "createDocumentFragment" in body and "replaceChildren" in body, gotcha(
        "session-picker-atomic-swap",
        "loadSessions no longer builds off-DOM and swaps with replaceChildren — "
        "the atomic rebuild (no blank flash) is gone",
    )
    assert "return sessions" in body, gotcha(
        "session-picker-atomic-swap",
        "loadSessions stopped returning the sessions array — its callers read "
        "the `active` flags off it",
    )


def test_settings_subtab_is_a_scroll_containment_context():
    """Step 3 of the UI glitch sweep (2026-10-16), corrected same day on-device.

    `.subtab` needs `min-height: 0` AND a non-visible overflow: only then does
    `.cards`'s `flex: 1; overflow-y: auto` resolve against the pane's remaining
    space instead of its own content height.

    Containment alone is NOT the whole bug. iOS WebKit composites every
    touch-scrollable box as its own layer, and nested same-axis scrollers
    translate against each other mid-gesture: cards "swing" off their place on a
    phone swipe and snap back at rest. Headless Chromium cannot see this, which
    is why the first fix (adding `overflow-y: auto` to `.subtab`) measured green
    and still swung on a phone — it had stacked a THIRD nested scroller. The
    invariant that actually holds the device behaviour is ONE vertical touch
    scroller per pane:
      - `.subtab` stays `auto` (panes with no list, e.g. contacts, scroll);
      - `.subtab:has(> .cards)` drops to `hidden` — containment without a
        touch layer, so `.cards` is the only scroller;
      - `#tab-settings` must NOT be a scroller (as `auto` it was the outer layer);
      - the Schedules accordion, taller than a phone pane when open, caps and
        scrolls itself (a SIBLING of `.cards`, never an ancestor).

    Browser-only CSS with no JS tier — a source pin, like the table wrap and
    the session picker.

    Pinned in tests/test_conventions.py."""
    base = (_repo_root / "src/harness/gateway/webui/base.css").read_text()
    rule = re.search(r"\.subtab \{([^}]*)\}", base)
    assert rule, gotcha(
        "settings-pane-scroll-containment", ".subtab rule missing from base.css"
    )
    body = rule.group(1)
    assert "min-height: 0" in body, gotcha(
        "settings-pane-scroll-containment",
        "`.subtab` lost `min-height: 0` — a flex column keeps its auto minimum "
        "(content height) and the pane grows past the viewport again",
    )
    over = re.search(r"overflow(?:-y)?:\s*([a-z]+)", body)
    assert over and over.group(1) != "visible", gotcha(
        "settings-pane-scroll-containment",
        "`.subtab` no longer establishes a containment context (overflow "
        "visible or absent) — `.cards`'s flex:1 falls back to content height, "
        "the pane runs past the viewport and the cards detach off-screen: the "
        "2026-10-16 memory-cards bug",
    )
    panels = (_repo_root / "src/harness/gateway/webui/panels.css").read_text()
    cards = re.search(r"\.cards \{([^}]*)\}", panels)
    assert cards, gotcha(
        "settings-pane-scroll-containment", ".cards rule missing from panels.css"
    )
    cards_body = cards.group(1)
    assert "flex: 1" in cards_body and "overflow-y: auto" in cards_body, gotcha(
        "settings-pane-scroll-containment",
        ".cards lost flex:1/overflow-y:auto — it is the in-pane scroller for "
        "memory, schedules, runs and tools; without it the pane (not the cards) "
        "becomes the thing that scrolls and the fix has nothing to constrain",
    )
    # One-touch-scroller rule: list panes delegate to `.cards`, so the pane
    # itself never opens a second touch layer to parallax against it.
    delegate = re.search(r"\.subtab:has\(> \.cards\) \{([^}]*)\}", base)
    assert delegate, gotcha(
        "settings-pane-scroll-containment",
        "`.subtab:has(> .cards)` delegation rule missing — `.subtab` scrolls AND "
        "`.cards` scrolls: two nested same-axis touch layers, and iOS parallaxes "
        "the cards against the pane mid-swipe (the phone-only 'swing')",
    )
    d_over = re.search(r"overflow(?:-y)?:\s*([a-z]+)", delegate.group(1))
    assert d_over and d_over.group(1) in ("hidden", "clip"), gotcha(
        "settings-pane-scroll-containment",
        "`.subtab:has(> .cards)` must be hidden/clip — `auto` keeps a second "
        "touch scroller nested around `.cards` (iOS swing), `visible` loses the "
        "flex containment and the cards detach off-screen again",
    )
    # `#tab-settings` was the outermost of the three stacked scrollers.
    outer = re.search(r"#tab-settings \{([^}]*)\}", base)
    assert not (outer and "auto" in outer.group(1)), gotcha(
        "settings-pane-scroll-containment",
        "`#tab-settings` is a scroll container again — a third same-axis scroller "
        "wrapping every pane; iOS composites it separately and the panes parallax",
    )
    # The open Schedules/Memory accordion stays reachable without a pane scroller.
    acc = re.search(r"\.schedule-acc \{([^}]*)\}", panels)
    assert acc and "overflow-y: auto" in acc.group(1) and "max-height" in acc.group(1), gotcha(
        "settings-pane-scroll-containment",
        "`.schedule-acc` lost its self-scroll cap — with the pane no longer "
        "scrolling, an open create/edit form taller than a phone pane puts Save "
        "out of reach with nothing to scroll",
    )


def test_card_titles_cannot_widen_a_cards_pane():
    """Memory facts are free text — extraction writes long URLs and long
    hyphenated identifiers. `.card .title` is a FLEX container, so the fact text
    is an anonymous flex item with `min-width: auto`: it cannot shrink below
    min-content, and min-content of an unbreakable token is the WHOLE token. One
    such fact made the card 535px wide in a 393px phone pane, and `.cards` —
    whose `overflow-x` computes to `auto` from its `overflow-y: auto` — became a
    sideways scroller, so a diagonal swipe slid the cards off screen (the
    2026-10-16 "cards swing/detach" report; the cards were literally off the
    right edge). Triggers/Connections share the layout and never showed it,
    which is how it was pinned to content rather than layout.

    Two halves, both needed: `.title` breaks long tokens (like `.meta` already
    did), and `.cards` is a VERTICAL-only scroller so any future overflow
    degrades to clipped, never to swipe-the-cards-away.

    Browser-only CSS with no JS tier — a source pin.

    Pinned in tests/test_conventions.py."""
    panels = (_repo_root / "src/harness/gateway/webui/panels.css").read_text()
    title = re.search(r"\.card \.title \{([^}]*)\}", panels)
    assert title, gotcha(
        "cards-pane-no-horizontal-scroll", ".card .title rule missing from panels.css"
    )
    assert ("word-break" in title.group(1) or "overflow-wrap" in title.group(1)), gotcha(
        "cards-pane-no-horizontal-scroll",
        "`.card .title` lost its token breaking — an unbreakable fact (URL, long "
        "identifier) sets the card's min-content past the pane and the list "
        "becomes a sideways scroller: the memory-cards swing",
    )
    cards = re.search(r"\.cards \{([^}]*)\}", panels)
    assert cards, gotcha("cards-pane-no-horizontal-scroll", ".cards rule missing")
    ox = re.search(r"overflow-x:\s*([a-z]+)", cards.group(1))
    assert ox and ox.group(1) in ("hidden", "clip"), gotcha(
        "cards-pane-no-horizontal-scroll",
        "`.cards` is a two-axis scroller again (overflow-x computes to auto from "
        "overflow-y) — wide content becomes a sideways swipe instead of clipping",
    )


def test_transcript_replay_buffers_live_events():
    """Opening a still-RUNNING session replays stored messages while live
    events keep arriving: unbuffered, they paint above the not-yet-fetched
    history and their tool_results finish the replay's pills (scheduled-run
    sessions rendered as missing/garbled tool calls, 2026-10-16). So
    dispatchStream must BUFFER events while renderSession replays, and the
    release must belong to the NEWEST renderSession — an older call releasing
    the flag while the newest replay is mid-fetch reopens the same race. The
    replay side passes its pill explicitly, never through the shared global.

    Amended 2026-10-17 (thinking-block-vanish): the flush is now conditional —
    after a NORMAL render the buffer flushes as before; after a RECOVERY render
    it is DISCARDED: those events are the just-completed turn rerouted
    mid-fetch, already contained in the authoritative replay — flushing them
    repaints the stale tail over it.

    Amended 2026-10-18 (pwa-accurate-resume Step 7): the flag-and-shadow pair
    (_replaying + _pendingEvents) is ONE state — `_buffer`, null when no
    replay owns the DOM — and recovery is a PARAMETER
    (renderSession(id, recovering)), nothing ambient to arm.

    Browser-only JS with no JS tier — a source pin, like follow.js.

    Pinned in tests/test_conventions.py."""
    js = (_repo_root / "src/harness/gateway/webui/transcript.js").read_text()

    assert re.search(
        r"export function dispatchStream\(ev\) \{\s*"
        r"(?://[^\n]*\n\s*)*"
        r"if \(_buffer\) \{[^}]*_buffer\.push\(ev\)[^}]*return;",
        js,
    ), gotcha(
        "replay-event-queue",
        "dispatchStream no longer buffers events while a replay is in flight — "
        "events arriving during renderSession's fetch paint above the replayed "
        "history and race its pill state",
    )

    body = js[js.index("export async function renderSession"):]
    assert "_buffer = []" in body and body.index("_buffer = []") < body.index(
        "await api("
    ), gotcha(
        "replay-event-queue",
        "renderSession must open the buffer BEFORE awaiting the message fetch — "
        "the events worth buffering arrive during that window",
    )
    assert re.search(
        r"if \(seq === renderSeq\) \{(?:\s|//)*const queued = _buffer;"
        r"(?:\s|//)*_buffer = null;",
        body,
    ), gotcha(
        "replay-event-queue",
        "the buffer release is no longer owned by the newest renderSession (a "
        "seq === renderSeq check) — a superseded call releasing it unlocks the "
        "screen mid-replay of the newest one",
    )
    assert re.search(
        r"_buffer = null;.{0,300}?if \(recovering\) \{\s*"
        r"(?://[^\n]*\n\s*)*"
        r"tlog\(\"renderSession:flush\", \{ seq, queued: queued\.length, discarded: true \}\);\s*"
        r"\} else \{\s*"
        r"(?://[^\n]*\n\s*)*"
        r"tlog\(\"renderSession:flush\", \{ seq, queued: queued\.length \}\);\s*"
        r"(?://[^\n]*\n\s*)*"
        # Amended 2026-10-18 (pwa-accurate-resume Step 6): flushed events land
        # on the REBUILT DOM, so the run's seq marks (replay.js) must cover
        # them — the sink gate marks a slot that resetTranscript then clears,
        # and unmarked flushes would be re-painted by the next /live resync.
        # But an event the /messages live join already painted (it reached the
        # sink during the fetch AND sat in the join's tail) must be SKIPPED,
        # or the reopen double-paints it (cleric, Step 6) — flushBuffered owns
        # that rule (replay.js).
        r"flushBuffered\(id, queued, dispatchStream\);\s*\}",
        body,
        re.S,
    ), gotcha(
        "replay-event-queue",
        "buffered events must FLUSH (in arrival order, onto the replayed tail, "
        "skipping anything the /messages live join already painted, and "
        "marking the rest for the seq dedupe) after a NORMAL render but be "
        "DISCARDED (cleared, never dispatched) after a RECOVERY render — "
        "dropping the flush loses events; dropping the discard repaints the "
        "completed turn's stale tail over the fresh replay; flushing the "
        "join's own tail double-paints it (2026-10-17 thinking-block-vanish, "
        "Step 6 reopen-dedupe)"
    )

    pills = (_repo_root / "src/harness/gateway/webui/pills.js").read_text()
    assert re.search(
        r"function finishToolPill\(isError, text, chars, pill\)",
        pills,
        # pills.js (BK-017 LOC split) — the signature pin follows the function
    ), gotcha(
        "replay-event-queue",
        "finishToolPill lost its explicit pill parameter — replayed results "
        "again resolve through the shared pendingPill global"
    )
    assert js.count(", null, pill);") == 2, gotcha(
        "replay-event-queue",
        "both replay paths (content_blocks + legacy rows) must pass the pill "
        "they just created to finishToolPill explicitly (startToolPill's "
        f"return, since the pills.js split); found {js.count(', null, pill);')}"
    )
    # ...and the pill passed must BE that call's pill: startToolPill's return.
    # Delete the `return pill` and `const pill = startToolPill(...)` is
    # undefined, finishToolPill falls back to the shared pendingPill, and the
    # count above stays green — the old silent wrong-pill behaviour, restored
    # by a one-word edit (2026-10-29 review).
    assert js.count("const pill = startToolPill(") == 2, gotcha(
        "replay-event-queue",
        "a replay path no longer binds its OWN pill from startToolPill's "
        f"return; found {js.count('const pill = startToolPill(')} binding(s)"
    )
    assert re.search(r"pendingPill = pill;\s*\n\s*return pill;", pills), gotcha(
        "replay-event-queue",
        "startToolPill must RETURN the pill it created (replay passes this "
        "return explicitly) — without it finishToolPill resolves through the "
        "shared pendingPill again and a live event can repoint which pill a "
        "replayed result finishes"
    )


def test_pill_drawer_opens_only_on_the_users_tap():
    """Step 3b (frontend-smoothness, 2026-10-31): a tool pill's drawer opens and
    closes ONLY when the user taps it. finishToolPill used to force-open every
    failed pill, and it runs on every session replay too — so each re-render
    (chat switch, resume heal, observe heal) re-opened failed pills the user
    had closed, and a later failure in the row stole the drawer the user had
    open. The behaviour is proven in a real WebKit by
    scratch/smoothness_probe/pills.py; this pin keeps the one-caller shape.

    Browser-only JS with a DOM at import (no jsunit tier) — a source pin.

    Pinned in tests/test_conventions.py."""
    pills = (_repo_root / "src/harness/gateway/webui/pills.js").read_text()
    calls = re.findall(r"(?<!function )\btogglePill\(([^)]*)\)", pills)
    assert calls == ["row, pill"], gotcha(
        "pill-tap-only-toggle",
        "togglePill must have exactly ONE caller, the pill's click handler, "
        f"with no force argument; found calls {calls} — any programmatic open "
        "re-runs on every session replay and re-opens drawers the user closed",
    )
    assert re.search(
        r'addEventListener\("click", \(\) => togglePill\(row, pill\)\)', pills
    ), gotcha(
        "pill-tap-only-toggle",
        "the pill's click handler no longer calls togglePill(row, pill) — the "
        "user's tap must stay the one way a drawer opens or closes",
    )
    # Three writes: ensurePillRow's init, togglePill's close and open. A
    # fourth is a drawer opened (or closed) outside the user's tap.
    writes = re.findall(r"_openPill = ", pills)
    assert len(writes) == 3, gotcha(
        "pill-tap-only-toggle",
        f"row._openPill is written {len(writes)} times in pills.js (expected 3: "
        "ensurePillRow's init + togglePill's close/open) — something else "
        "opens or closes a drawer outside the user's tap",
    )


def test_recovery_renders_arm_and_consume_the_discard_flag():
    """The stale-buffer discard (thinking-block-vanish, 2026-10-17) rewritten
    for the Step-7 collapse. The old shape — a module-level `_recovering` armed
    immediately before each of three refetch sites — is what let the bug class
    RETURN: the flag was consumed synchronously at renderSession entry to stop
    a superseded recovery render leaking it into the next NORMAL render (whose
    live events would then be discarded as stale and LOST), and that
    arm/consume discipline existed only because the state was ambient. A
    parameter cannot leak: every recovery render states its purpose AT THE
    CALL (renderSession(id, recovering)), so this pin now guards the collapse
    itself —

    (1) the ambient flag is gone for good: no `_recovering`, no
        `setRecovering` anywhere in the webui (their leak class is impossible
        by construction only while nothing ambient exists);
    (2) ONE resync entry point owns every recovery decision: BK-019 Step 4
        moved it from events.js into runstate.js (BK-034: split verbatim
        into resync.js at the line cap) — resyncVerdict (T27) is the
        only verdict tree, called by runDoneHeal, visibleResume and
        watchdogTick (the old resyncRun name is gone, and events.js asks
        /live zero times itself — a second decision tree is what drifted
        before);
    (3) every heal states the discard at the call: the persisted-heal tail
        renders renderSession(id, true, true) and the cold join's render
        carries its terminal verdict renderSession(id, terminal, true) (the
        third argument is BK-017's preserveRead, likewise stated at the
        call). Step 4 moved the calls into the fx maps' bindings, so the
        claim is now: the transition pushes the rerenderHeal/rerenderCold
        EFFECT (runstate, guard included) and BOTH routers bind those tokens
        to renderSession with the discard args;
    (4) the one state is ONE write each way, and both writes live in
        runstate.js alone (arm: socketDied; clear: abandonRun) — the
        transports write neither (the full write-ownership pin is
        test_runstate_is_the_only_writer_of_the_lifecycle_fields).

    The probe exercises the run_done heal end-to-end; the visibilitychange and
    watchdog call sites and the no-ambient-flag shape are source-only.

    Pinned in tests/test_conventions.py."""
    root = _repo_root / "src/harness/gateway/webui"
    events = (root / "events.js").read_text()
    app = (root / "app.js").read_text()
    runstate = (root / "runstate.js").read_text()
    # BK-034 split runstate.js at its line cap: the verdict half (T27 and its
    # askers T17/T20/T21) moved verbatim to resync.js. The tree pins below
    # read THAT file; runstate.js keeps the field writes (abandonRun's clear,
    # socketDied's arm) and must hold no copy of the tree.
    resync = (root / "resync.js").read_text()
    state = (root / "state.js").read_text()

    # (1) The ambient discard flag stays dead. Its arm/consume dance was the
    # leak; the parameter replaced it 2026-10-18 (Step 7). The claim is
    # "nowhere in the webui", so the scan reads EVERY webui JS file: an
    # ambient flag reintroduced in replay.js or thinking.js is the same leak
    # class, wherever it sits.
    ambient = {f.name: f.read_text() for f in sorted(root.rglob("*.js"))}
    hits = sorted(
        name
        for name, text in ambient.items()
        if "_recovering" in text or "setRecovering" in text
    )
    assert not hits, gotcha(
        "replay-event-queue",
        "the ambient recovery flag is back (_recovering / setRecovering) in "
        f"{', '.join(hits)} — three arm sites around one consumed module "
        "boolean is the exact shape the stale-tail class returned through; "
        "recovery is the renderSession `recovering` PARAMETER now (this is a "
        "SUBSTRING ban: a doc comment naming the retired flag trips it too — "
        "reword the comment)",
    )
    # (2) One entry point, three callers — and it no longer lives in events.js.
    # BK-019 Step 4: the verdict TREE is runstate's resyncVerdict (T27);
    # resyncRun must stay dead (a second tree beside T27 is the drift class
    # both collapses exist to kill). BK-034 moved it to resync.js: it appears
    # there only as the definition plus the runDoneHeal / visibleResume /
    # watchdogTick / observeTick (BK-034's observe watch: the observed run
    # asks the SAME tree) call sites, and NOWHERE else in the webui.
    assert "resyncRun" not in events and "resyncRun" not in app, gotcha(
        "replay-event-queue",
        "the old resyncRun entry point is back in a transport — Step 4 moved "
        "the ONE tree to runstate.js's resyncVerdict (T27); a copy beside it "
        "can drift",
    )
    def _code(text: str) -> str:  # prose names the entry point everywhere
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        return re.sub(r"//[^\n]*", " ", text)

    callers = {f.name: _code(f.read_text()).count("resyncVerdict(")
               for f in sorted(root.glob("*.js")) if f.name != "resync.js"}
    callers = {k: v for k, v in callers.items() if v}
    assert resync.count("resyncVerdict(") == 5 and not callers, gotcha(
        "replay-event-queue",
        "resync.js must hold exactly the resyncVerdict definition plus its "
        "four call sites (runDoneHeal, visibleResume, watchdogTick, and "
        "BK-034's observeTick) and no other webui module may call it — found "
        f"{resync.count('resyncVerdict(')} in resync.js, elsewhere {callers}; "
        "a second heal path outside the entry point is a second decision "
        "tree that can drift",
    )
    # The /live question is asked ONCE, inside the entry point; the transports
    # hand the fetch in as a dep (RUN_DEPS) and never call it themselves.
    assert events.count("fetchLive(") == 0 and app.count("fetchLive(") == 0, gotcha(
        "replay-event-queue",
        "a transport calling fetchLive directly is a second verdict path — "
        "the /live ask belongs to resyncVerdict alone (injected via deps)",
    )
    assert resync.count("deps.fetchLive(") == 1 and "deps.fetchLive(" not in runstate, gotcha(
        "replay-event-queue",
        "the resync entry point must ask /live exactly once per verdict — a "
        "second deps.fetchLive call is a second ask beside the one tree",
    )
    # (3) Heals state the discard at the call, not through an armed flag.
    # Step 4: the transition pushes the rerender EFFECT and the routers' fx
    # maps bind the token to renderSession WITH the discard args — pin both
    # halves (the push inside the terminal branch, the binding at the call).
    assert re.search(r"resyncVerdict\(ev\.session_id, true, deps\)", resync) \
        and re.search(r'verdict: "terminal"', resync), gotcha(
        "replay-event-queue",
        "run_done must hand the entry point the server's OWN terminal word "
        '(resyncVerdict(id, true), verdict "terminal") — the event is the '
        "verdict; re-asking /live can contradict it while the persisted heal "
        "is due",
    )
    heal_branch = resync[resync.index("const effects = abandonRun(id);"):]
    push_at = heal_branch.index('effects.push(["rerenderHeal", id])')
    ret_at = heal_branch.index('verdict: "terminal"')
    assert push_at < ret_at, gotcha(
        "replay-event-queue",
        "the persisted heal's terminal branch must carry the rerenderHeal "
        "effect (renderSession(id, true) downstream) BEFORE returning the "
        "terminal verdict — without it the completed turn's rerouted events "
        "flush over the fresh replay (zombie .think, tail bubble) — the "
        "thinking-block-vanish class",
    )
    assert re.search(
        r"if \(ui\.sessionId === id && !deps\.isReplaying\(\)\) \{\s*"
        r"effects\.push\(\[\"rerenderCold\", id, why === \"terminal\"\]\);",
        resync,
    ), gotcha(
        "replay-event-queue",
        "the cold join must push its rerenderCold effect under the "
        "screen-and-not-replaying guard, carrying the batch's own terminal "
        "verdict as the discard argument — a terminal batch's rerouted tail "
        "races the join (second-device heals)",
    )
    for transport in (events, app):  # both fx maps state the discard at the binding
        assert re.search(
            r"rerenderHeal:\s*\(id\) =>\s*renderSession\(id, true, true\)", transport
        ) and re.search(
            r"rerenderCold:\s*\(id, terminal\) =>\s*renderSession\(id, terminal, true\)",
            transport,
        ), gotcha(
            "replay-event-queue",
            "an fx map no longer binds rerenderHeal/rerenderCold to "
            "renderSession with the discard (+preserveRead) arguments — the "
            "heal/join render must CARRY the discard, stated at the call",
        )
    # (4) The one state, one write each way (arm in socketDied, clear in
    # abandonRun) — and after Step 4 BOTH live in runstate.js alone. The
    # exhaustive no-stray-writes claim is the dedicated pin below; this is the
    # Step-7 shape's successor: one owner, not scattered sites.
    assert runstate.count("ui.resync = null;") == 1, gotcha(
        "replay-event-queue",
        "abandonRun is THE owner of ui.resync's clear — scattered clears are "
        "how pendingChat/catchingUp drifted apart",
    )
    assert runstate.count("ui.resync = {") == 1, gotcha(
        "replay-event-queue",
        "socketDied (T15) is THE arming site of ui.resync — a second arm is a "
        "second recovery-state producer",
    )
    assert "ui.resync =" not in events and "ui.resync =" not in app, gotcha(
        "replay-event-queue",
        "a transport writes ui.resync again — the transports route through "
        "transitions now; the field has exactly one writer module (runstate.js)",
    )
    assert "catchingUp" not in state and "pendingChat" not in state, gotcha(
        "replay-event-queue",
        "the TWO-flag state is back in state.js — resync = {id, seen} is the "
        "one recovery state (Step 7 collapse)",
    )


def test_resume_branches_on_live_not_the_capped_sessions_page():
    """pwa-accurate-resume Step 6: the two "the run finished while I was away"
    verdicts (visibilitychange + the detach watchdog) must ask the RUN — GET
    /api/chat/{sid}/live's `active` predicate (gateway/live.py) — never the
    CAPPED 50-row /api/sessions page. A row's `active` is registry membership
    (stale through the done→_reap tick) and an off-page live run reads as
    ABSENT on that page; either lie armed a mid-call PERSISTED-store render
    that wipes the on-screen partial head (probe_live's partial-head-
    survives; decisions 2026-10-17 amended). Source pin: browser behaviour,
    no JS tier — the probe covers the resume path end-to-end.

    Pinned in tests/test_conventions.py."""
    root = _repo_root / "src/harness/gateway/webui"
    events = (root / "events.js").read_text()
    # BK-034: the entry point moved verbatim from runstate.js to resync.js.
    runstate = (root / "resync.js").read_text()
    assert "sessions.find(" not in events, gotcha(
        "resume-live-predicate",
        "events.js consults the /api/sessions page for run liveness again — "
        "that page is capped at the 50 most recent rows (live DB ~950 "
        "sessions), so an off-page LIVE run reads as finished and the armed "
        "heal re-renders from the store mid-call",
    )
    # Step 7: both verdict sites run through ONE entry point, which asks /live
    # once, through fetchLive (replay.js: /live at OUR watermark), and
    # branches on the response's `active`, not on a row. BK-019 Step 4: the
    # entry point is runstate.js's resyncVerdict (T27) — the transport hands
    # the fetch in via RUN_DEPS, so the /live ASK and the `active` branch are
    # scanned in runstate.js; events.js keeps the timer and the dep wiring.
    assert "fetchLive" in events and events.count("fetchLive(") == 0, gotcha(
        "resume-live-predicate",
        "events.js must INJECT fetchLive (RUN_DEPS) and never call it — the "
        "ask is the entry point's; a transport-side call is a second tree",
    )
    assert re.search(r"if \(res\.active === true\)", runstate), gotcha(
        "resume-live-predicate",
        "the resync's still-mid-call branch must key on /live's `active` "
        "predicate — anything else re-renders the store mid-call",
    )
    heal = runstate[runstate.index("export async function resyncVerdict"):]
    assert 'verdict: "offline"' in heal and 'verdict: "missing"' in heal \
        and 'verdict: "terminal"' in heal, gotcha(
        "resume-live-predicate",
        "the entry point's fallthrough — unreachable / pre-/live server / "
        "ended-run — must stay ONE tree: no `active:true` and no live answer "
        "means the persisted heal (or ask again), never a row lookup",
    )
    # A gap-filled batch must not paint over an in-flight replay: the guard
    # reads the injected dep inside the transition, and the transport supplies
    # the real predicate.
    assert "deps.isReplaying()" in runstate and "isReplaying" in events, gotcha(
        "resume-live-predicate",
        "gap-fill needs the isReplaying() guard — painting a /live batch "
        "above a renderSession replay races the atomic swap",
    )


def test_gap_fill_dedupes_by_painted_not_scalar_cutoff():
    """pwa-accurate-resume Step 6 dedupe rule (probe_live's no-duplicate-words):
    a replay batch may skip ONLY what this DOM actually painted. The high-water
    mark (slot.water) is the request's `since`; it is NEVER the batch's skip
    test — the batch legitimately carries a seq BELOW the mark (the live stream
    skipped that word and the batch is its only source; a `seq <= last-seen`
    cutoff silently drops it). Also pinned: seq-LESS events (background runs
    share /api/events) pass through the dedupe gate untouched — "no seq" ≠
    "old". And the watermark the client resends is a server-issued `seq` —
    live.seq — never live.since.

    Pinned in tests/test_conventions.py."""
    replay = (_repo_root / "src/harness/gateway/webui/replay.js").read_text()
    gate = replay[replay.index("export function alreadyPainted"):]
    gate = gate[:gate.index("export function markPainted")]
    assert re.search(r"ev\.seq == null\) return false", gate), gotcha(
        "resume-seq-dedupe",
        "the dedupe gate must PASS seq-less events through — background-run "
        "events on /api/events carry no seq and treating that as 'old' eats them",
    )
    assert "s.painted" in gate and "ev.seq >= lo" in gate, gotcha(
        "resume-seq-dedupe",
        "alreadyPainted must test the PAINTED intervals, not the high-water "
        "mark — a scalar `seq <= last-seen` cutoff drops the gap-fill word "
        "that sits below a later live event's seq (no-duplicate-words)",
    )
    assert "s.water" not in gate, gotcha(
        "resume-seq-dedupe",
        "the membership test must not read the watermark — the moment the skip "
        "test consults the high-water mark, no-duplicate-words regresses",
    )
    # seq space is RUN-LOCAL: a new run's seq 1 must not read as painted by
    # the previous run's intervals, or its first events are swallowed whole
    # (the gate checks BEFORE markPainted's reset).
    assert "ev.run_id !== s.runId" in gate, gotcha(
        "resume-seq-dedupe",
        "alreadyPainted must answer 'no' for an event from a DIFFERENT run — "
        "seq restarts at 1 per run, so a new run's early events otherwise sit "
        "inside the old run's painted intervals and are dropped unseen",
    )
    assert "syncRun(id, s, ev.run_id)" in replay, gotcha(
        "resume-seq-dedupe",
        "markPainted must reset the slot on a run_id change (syncRun): an old "
        "run's watermark would ask the new run to skip its own first events",
    )
    fill = replay[replay.index("export function paintGapFill"):]
    assert re.search(
        r"if \(!RENDERED\[ev\.type\] \|\| alreadyPainted\(id, ev\)\) continue;\s*"
        r"paint\(ev\);\s*markPainted\(id, ev\);",
        fill,
    ), gotcha(
        "resume-seq-dedupe",
        "the gap-fill loop must skip by alreadyPainted, paint through the "
        "injected raw painter (transcript's paintStream — render state stays "
        "private there), and mark only what it painted; bookkeeping types stay "
        "unmarked for the live handler",
    )
    # The normal reopen's flush obeys the same rule: a rerouted event that
    # sits in BOTH the /messages join's tail and the replay buffer paints once.
    flush = replay[replay.index("export function flushBuffered"):]
    assert re.search(
        r"if \(alreadyPainted\(id, ev\)\) continue;\s*"
        r"markPainted\(id, ev\);\s*dispatch\(ev\);",
        flush,
    ), gotcha(
        "resume-seq-dedupe",
        "flushBuffered must skip an event the /messages live join already "
        "painted — join-then-flush double-paints it on every reconnect-reopen",
    )
    # The watermark the client resends is live.seq — never live.since.
    join = replay[replay.index("export function consumeLiveJoin"):]
    assert re.search(r"live\.seq > s\.water\) s\.water = live\.seq", join) and not re.search(
        r"s\.water = live\.since", join
    ), gotcha(
        "resume-seq-dedupe",
        "the client watermark is live.seq (the run's last issued number); "
        "live.since is the SERVER's persisted join point — sending it re-asks "
        "the whole tail (and seqs restart at 1 per run, so it rots across "
        "runs anyway)",
    )


def test_run_identity_rule_batches_cold_join_and_terminal_runs_are_ignored():
    """pwa-accurate-resume Step 6 identity rule (cleric's handed-back, three
    faces, one rule): a slot vouches for exactly ONE run, and BATCH resync may
    only paint what the slot vouches for.

    (1) A /live batch naming a DIFFERENT run than the slot (my run ended,
    another started — possibly on another device), meeting a PROVISIONAL slot
    (an empty-tail join names no run), or naming a TERMINAL run (run_done
    passed it): gap-filling would skip the new run's head (my watermark is out
    of its seq space) or repaint over my own head — so the ONE batch-resync
    path (Step 7's resyncRun, BK-019 Step 4: runstate.js's resyncVerdict T27,
    fed by the resume AND the watchdog tick) must cold-join instead: the one
    sanctioned fetch, renderSession's live join (the rerenderCold effect,
    bound by the routers' fx maps), its discard stated as the terminal verdict
    the batch itself carries.
    (2) A slot reset MARKS THE DISPLACED RUN TERMINAL (syncRun choke point +
    observed run_done stamps it), and a terminal run's events never touch the
    slot — the carried "stopped detached run publishes `title` after
    run_done" case must relabel the picker without wiping a newer run's
    watermark (probe_live's late-old-run-title).
    (3) An empty-tail join leaves a PROVISIONAL slot with NO marks and NO
    watermark: unattributed marks would swallow a new run's seq 1..N — token
    loss, the worst failure class.
    (4) /live 404 = an agentd predating the route (deploy-day skew): the
    {missing:true} sentinel makes the watchdog STOP probing (deterministic,
    not an outage — network-nulls keep retrying) and never store-render (an
    agentd this old has no live join, so that render is the lossy path).

    Pinned in tests/test_conventions.py; faces (1)/(2) behaviourally by
    probe_live's second-device-run / late-old-run-title scenarios."""
    root = _repo_root / "src/harness/gateway/webui"
    replay = (root / "replay.js").read_text()
    events = (root / "events.js").read_text()
    runstate = (root / "runstate.js").read_text()
    # BK-034: the verdict half (T27 + its askers) moved verbatim to resync.js.
    resync = (root / "resync.js").read_text()
    # (2) terminal marking at BOTH sources, and the guard at the choke point.
    assert re.search(r"markRunDone\(id, s\.runId\)", replay), gotcha(
        "resume-seq-dedupe",
        "syncRun must mark the DISPLACED run terminal on a reset — a run whose "
        "run_done died with the socket is proven over by its successor, and "
        "only a terminal note stops its late events resetting a newer slot",
    )
    assert re.search(r"noteRunTerminal\(ev\.session_id, ev\.run_id\)", runstate), gotcha(
        "resume-seq-dedupe",
        "run_done must stamp its run terminal (it names the run that ended) — "
        "the post-run_done title publisher is a known late event (BK-019: the "
        "stamp lives in runstate's T18/T19; replay's markRunDone delegates)",
    )
    mark = replay[replay.index("export function markPainted"):]
    mark = mark[: mark.index("\nexport function") if "\nexport function" in mark[1:] else len(mark)]
    assert "isTerminalRun(id, ev.run_id)" in mark, gotcha(
        "resume-seq-dedupe",
        "markPainted must refuse a terminal run's events at the choke point — "
        "one guard covers every sink and painter gate at once",
    )
    # (1) The ONE batch-resync path cold-joins on an unverifiable batch.
    # Step 7 collapsed the two resync sites into resyncRun; BK-019 Step 4
    # moved the entry point into runstate.js's resyncVerdict (T27): the
    # identity verdict and its cold-join branch live in the transition, and
    # EVERY trigger (resume, watchdog tick, run_done) decides there — so one
    # branch covers what used to be two, and the pins below guard that a
    # second batch path cannot grow back beside it.
    assert re.search(
        r"const why = deps\.joinReason\(id, res\.events, res\.seq\);\s*\n"
        r"\s*if \(why\) \{",
        resync,
    ), gotcha(
        "resume-seq-dedupe",
        "the identity verdict must be replay.js's joinReason over the slot AND "
        "the batch (run_id + the batch's `seq`), and the entry point must "
        "route ANY non-null verdict to the one-fetch live join: the slot is "
        "the only thing that knows which run its numbers describe, and "
        "gap-filling across a seq restart silently drops the new run's head "
        "(second-device case, quest goal). Passing `seq` is what catches the "
        "EMPTY batch whose seq is below my watermark — a restarted run, not "
        "'caught up'",
    )
    assert resync.count("joinReason(") == 1 and runstate.count("joinReason(") == 0 \
            and events.count("joinReason(") == 0, gotcha(
        "resume-seq-dedupe",
        "resync.js must hold exactly ONE identity-verdict site — "
        "resyncVerdict — and the transport none (it injects the arithmetic as "
        "a dep). A second joinReason call means a second batch-resync decision "
        "tree grew back beside the entry point: the drift the Step-7 collapse "
        "exists to prevent",
    )
    tick = events[events.index("export function armDetachWatchdog"):]
    assert re.search(r"await watchdogTick\(RUN_DEPS\)", tick), gotcha(
        "resume-seq-dedupe",
        "the watchdog tick must ask the SAME entry point (runstate's "
        "watchdogTick → resyncVerdict) — its reroute may never have armed, "
        "and a run-mismatched batch must cold-join on that trigger exactly as "
        "it does on the resume",
    )
    # Identity also has a monotonicity witness for an unnamed (empty) batch.
    assert 'seq < water ? "mismatch" : null' in replay, gotcha(
        "resume-seq-dedupe",
        "joinReason must treat a batch seq BELOW my watermark as another run "
        "(seq is monotonic per run): a short new run's empty batch would "
        "otherwise stay invisible (second-device, seq restart)",
    )
    raise_body = replay[replay.index("export function raiseWatermark"):]
    raise_body = raise_body[: raise_body.index("\nexport function")]
    assert "slot(id)" not in raise_body, gotcha(
        "resume-seq-dedupe",
        "raiseWatermark must NOT create a slot: a missing slot means nothing "
        "is attributed yet, and creating a provisional one WITH a watermark "
        "is exactly the unattributed state the identity rule forbids",
    )
    # The cold-join branch (BK-019 Step 4: inside resyncVerdict, the render
    # reaches the DOM through the routers' rerenderCold fx binding).
    cjoin = resync[resync.index("if (why) {"):]
    cjoin = cjoin[: cjoin.index("// verdictGapFill")]
    assert "noteRunTerminal(id, slot.runId)" in cjoin and re.search(
        r'effects\.push\(\["rerenderCold", id, why === "terminal"\]\);', cjoin
    ) and re.search(
        r"rerenderCold:\s*\(id, terminal\) =>\s*renderSession\(id, terminal, true\)",
        events,
    ), gotcha(
        "resume-seq-dedupe",
        "the cold join must mark the displaced run terminal itself (a "
        "bookkeeping-only tail would leave the slot un-reset) and its render "
        "must CARRY the batch's own terminal verdict as the discard argument "
        "(Step 7: a parameter stated at the call, never an armed flag — "
        "BK-019: stated in the rerenderCold binding) — a terminal batch's "
        "rerouted tail races the fetch",
    )
    # (3) provisional slot: empty-tail join holds nothing attributable.
    assert re.search(
        r"lastRun == null\) \{[\s\S]{0,600}?slot\(id\);\s*\n\s*return;", replay
    ) and "for (let n = 1; n <= (live.since || 0); n++)" in replay, gotcha(
        "resume-seq-dedupe",
        "an empty-tail join must leave a PROVISIONAL slot (no head marks, no "
        "watermark) — unattributed head marks answer 'painted' for a NEW run's "
        "seq 1..N, which is token loss; the named-run head-marking stays",
    )
    assert re.search(
        r"syncRun\(id, s, lastRun\);[\s\S]{0,700}?if \(s\.runId == null\) s\.runId = lastRun;",
        replay,
    ), gotcha(
        "resume-seq-dedupe",
        "a NAMED tail whose run went terminal during the fetch must still "
        "attribute the fresh slot (the payload is the only witness) — else it "
        "becomes provisional and takes live.seq as a watermark (face 3's "
        "second seam; probe_live terminal-join-attribution)",
    )
    # (4) the missing-route sentinel, consumed by the watchdog only. After
    # Step 7 the SENTINEL lives in replay.js's fetchLive and the STOP is the
    # entry point's "missing" verdict read by the tick — pin both halves:
    # the deterministic 404 verdict must survive to the timer, and only the
    # spent retries may end the loop.
    assert re.search(r"r\.status === 404\) return \{ missing: true \}", replay), gotcha(
        "resume-live-predicate",
        "fetchLive must distinguish 404 (agentd predates /live — deterministic) "
        "from a network null (retry) — same retry forever, opposite cure",
    )
    heal = resync[resync.index("export async function resyncVerdict"):]
    assert re.search(r'res\.missing\) return \{ verdict: "missing"', heal), gotcha(
        "resume-live-predicate",
        "the entry point must translate the sentinel into a distinct \"missing\" "
        "verdict — folding it into \"offline\" makes the watchdog probe a server "
        "that can never answer forever, and folding it into the terminal branch "
        "store-renders on the agentd that has no live join (the lossy path)",
    )
    wd = events[events.index("detachTimer = setInterval"):]
    assert re.search(
        r"tries < \d+ && \(verdict === \"live\" \|\| verdict === \"missing\"\)"
        r"[\s\S]{0,1200}?else if \(verdict === \"missing\"\) \{[\s\S]{0,160}?"
        r"clearInterval\(detachTimer\)",
        wd,
    ) and wd.count("clearInterval(detachTimer)") == 3, gotcha(
        "resume-live-predicate",
        "the watchdog must retry the /detach POST while the missing-route "
        "sentinel might still be version skew, then STOP on it (run_done stays "
        "the healer) — a permanent probe loop against a server that will "
        "never answer is the worst of the three skew behaviours",
    )


def test_runstate_is_the_only_writer_of_the_lifecycle_fields():
    """BK-019 Step 4's write-ownership pin (design §9): the four lifecycle
    fields (ui.resync / ui.liveTurn / ui.liveSeq / ui.liveDone) get EVERY
    write inside runstate.js — the whole drift class this refactor kills was
    two sites each believing it owned the state (pendingChat/catchingUp, the
    two resync entry points, the scattered liveTurn writes). The ban covers
    direct assignment, slot-entry creation (`ui.liveSeq[id] = …`), `delete`
    of an entry, and NESTED field writes (ui.liveTurn.detached, ui.resync.seen
    — the latch arm and the seen flip are transitions, T03/T16).

    Scope: src/harness/gateway/webui/*.js (runstate.js excepted). jsunit
    fixtures that arm the fields directly are the TEST SEAM, not violations —
    they live outside this directory on purpose. Slot CONTENT arithmetic
    (s.runId = …, s.water = …, s.painted.splice) stays in replay.js: it
    mutates objects obtained from runstate's slotFor, never through `ui.`,
    and the ban above reads only the `ui.`-anchored form, which is exactly
    the key/entry/field ownership boundary (D6).

    Pinned in tests/test_conventions.py."""
    root = _repo_root / "src/harness/gateway/webui"
    # BK-027 adds ui.bgLive (the background-run in-flight marks, T28/T29) —
    # map-shaped like liveSeq/liveDone, so it rides their entry rules too.
    # BK-034 adds ui.observed (the third Stop source, T30/T31/T06 arm, the
    # terminals clear) — object-shaped like resync, so it rides those rules.
    fields = r"(resync|liveTurn|liveSeq|liveDone|bgLive|observed)"
    bans = [
        (rf"ui\.{fields}\s*=(?!=)", "direct assignment"),
        (rf"ui\.(liveSeq|liveDone|bgLive)\s*\[[^\]]*\]\s*=(?!=)", "slot-entry write"),
        (rf"delete\s+ui\.(liveSeq|liveDone|bgLive)\s*\[", "slot-entry delete"),
        (rf"ui\.bgLive\s*\[[^\]]*\]\s*(\?\.)?\.\w+\s*=(?!=)", "mark field write"),
        (rf"ui\.(resync|liveTurn|observed)\s*(\?\.)?\.\w+\s*=(?!=)", "nested field write"),
    ]

    def strip(text: str) -> str:  # comments carry design prose about writes
        c = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", " ", c)

    offenders = {}
    for f in sorted(root.glob("*.js")):
        if f.name == "runstate.js" or f.name.endswith(".min.js"):
            continue
        code = strip(f.read_text())
        hits = [label for pat, label in bans if re.search(pat, code)]
        if hits:
            offenders[f.name] = hits
    assert not offenders, gotcha(
        "runstate-write-ownership",
        "modules outside runstate.js write the lifecycle fields again — every "
        "one of them is a second source of truth beside the transition table: "
        f"{offenders}",
    )
    # The claim must not pass by vacuity: runstate itself is THE writer, so
    # all four fields see a write there (arm/clear/entry/stamp) — the slot
    # maps are written through their entry form (`ui.liveSeq[id] = …`).
    rs = strip((root / "runstate.js").read_text())
    own = {
        "resync": r"ui\.resync\s*=(?!=)",
        "liveTurn": r"ui\.liveTurn\s*=(?!=)",
        "liveSeq": r"ui\.liveSeq\s*\[[^\]]*\]\s*=(?!=)",
        "liveDone": r"ui\.liveDone\s*\[[^\]]*\]\s*=(?!=)",
        "bgLive": r"ui\.bgLive\s*\[[^\]]*\]\s*=(?!=)",
        "observed": r"ui\.observed\s*=(?!=)",
    }
    for field, pat in own.items():
        assert re.search(pat, rs), gotcha(
            "runstate-write-ownership",
            f"runstate.js no longer writes ui.{field} — if the writes moved, "
            "this pin's exclusion just hid them; re-point the scan",
        )


def test_webui_state_fields_are_declared_where_read():
    """BK-019 Step 4 (design §9): every `ui.<field>` read in the webui must be
    a declared key of state.js's literal. An undeclared read is a phantom — it
    reads undefined, silently — and with four lifecycle fields a typo like
    `ui.liveTurn2` or a resurrected `ui.pendingChat` would mimic real state
    forever (undefined is falsy, so guards "work" wrong). The declaration is
    the whole vocabulary; this pin makes adding a field a deliberate act."""
    root = _repo_root / "src/harness/gateway/webui"
    state = (root / "state.js").read_text()
    decl = re.search(r"export const ui = \{(.*?)\n\};", state, re.S)
    assert decl, gotcha(
        "state-declared-fields",
        "state.js no longer holds the ui literal in the pinned shape — the "
        "field vocabulary must stay one parseable declaration",
    )
    declared = set(re.findall(r"(?m)^\s+(\w+):", decl.group(1)))
    assert {"sessionId", "resync", "liveTurn", "liveSeq", "liveDone", "bgLive",
            "observed"} <= declared, gotcha(
        "state-declared-fields",
        f"the ui declaration lost a lifecycle field; found {sorted(declared)}",
    )
    for f in sorted(root.glob("*.js")):
        if f.name.endswith(".min.js"):
            continue
        code = re.sub(r"/\*.*?\*/", " ", f.read_text(), flags=re.S)
        code = re.sub(r"(?m)^\s*//.*$", " ", code)
        used = set(re.findall(r"\bui\.([A-Za-z]\w*)", code)) - declared
        assert not used, gotcha(
            "state-declared-fields",
            f"{f.name} reads undeclared ui field(s) {sorted(used)} — reads of "
            "an undeclared field are always undefined; declare it in state.js "
            "(and give it an owner) or fix the name",
        )


def test_both_fxs_bind_every_effect_token_runstate_emits():
    """BK-019 Step 4 (design §5, §9): applyEffects THROWS on an unbound token,
    so each transport's fx map must be TOTAL over the tokens the transition
    table can emit — not merely along the paths a test happens to drive. This
    is the static proof: scan runstate.js's effect literals for the token set,
    then require every token to be named inside BOTH CHAT_FX (app.js) and
    RUN_FX (events.js). The maps bind the chat-only and events-only tokens of
    each other on purpose: dispatch totality is the contract, and an unbound
    token is wiring drift that must never reach a runtime event nobody
    rehearsed."""
    root = _repo_root / "src/harness/gateway/webui"
    # BK-034: the table spans runstate.js + resync.js (its verdict half).
    rs = "\n".join((root / f).read_text() for f in ("runstate.js", "resync.js"))
    rs = re.sub(r"(?m)^\s*//.*$", " ", rs)
    rs = re.sub(r"/\*.*?\*/", " ", rs, flags=re.S)
    tokens = set(re.findall(r'\["([A-Za-z][A-Za-z0-9]*)"', rs))
    assert len(tokens) >= 23, gotcha(
        "fx-total-binding",
        f"the effect-token scan found only {len(tokens)} tokens — the table "
        "changed shape (or the scan broke); re-check it against design §2.6 "
        "before trusting the binding claims below",
    )

    def fx_block(path: str, name: str) -> str:
        text = (root / path).read_text()
        start = text.index(f"const {name} = {{")
        end = text.index("\n};", start)
        assert start > text.index("import"), (
            f"{name} must be a module-level map in {path}"
        )
        # Comments inside the map cannot bind a token — strip them (full-line
        # AND trailing), or a prose mention would satisfy this pin while
        # applyEffects still throws on the unbound token.
        block = re.sub(r"/\*.*?\*/", " ", text[start:end], flags=re.S)
        return re.sub(r"//[^\n]*", " ", block)

    blocks = {
        "app.js/CHAT_FX": fx_block("app.js", "CHAT_FX"),
        "events.js/RUN_FX": fx_block("events.js", "RUN_FX"),
    }
    for where, block in blocks.items():
        missing = sorted(t for t in tokens if not re.search(rf"\b{t}\b", block))
        assert not missing, gotcha(
            "fx-total-binding",
            f"{where} does not bind every token the table can emit — missing "
            f"{missing}; applyEffects would THROW the first time that "
            "transition fires on this transport (silently, until then)",
        )


def test_transport_routers_route_lifecycle_through_runstate():
    """BK-019 Step 4 (design §5.2): the transports are ROUTERS. Inside the two
    event sinks (handleChatEvent, handleRunEvent) no renderer, status-row, or
    composer function may be called directly — every such effect reaches the
    DOM through applyEffects on the transport's fx map, so the transition
    table is the only place a lifecycle decision hides. (Non-chat router
    duties are allowed and expected: the working indicator, the toast, picker
    freshness on run_start, the sink-side dedupe mark, the tlog names.)"""
    root = _repo_root / "src/harness/gateway/webui"
    banned_common = [
        "statusStart", "statusStop", "statusWarn", "statusPhase", "statusEvent",
        "statusLane", "setSendMode", "dispatchStream", "closeSegments",
        "finalizeAgentBubble", "addErrorMsg", "addSystemMsg", "addUserMsg",
        "renderSession", "paintGapFill", "showNewChatOption", "updateRenameBtn",
    ]

    def body(path: str, start: str, end: str) -> str:
        text = (root / path).read_text()
        s = text.index(start)
        e = text.index(end, s)
        code = re.sub(r"/\*.*?\*/", " ", text[s:e], flags=re.S)
        return re.sub(r"(?m)^\s*//.*$", " ", code)

    chat = body("app.js", "function handleChatEvent", "/* ---- client timezone")
    runs = body("events.js", "function handleRunEvent", "// `chatWorkPending`")
    for name, code in (("handleChatEvent", chat), ("handleRunEvent", runs)):
        assert "applyEffects(" in code, gotcha(
            "router-purity",
            f"{name} no longer dispatches through applyEffects — the routers "
            "route transitions + effects now; a body without dispatch has "
            "grown its own decision path",
        )
        hits = sorted(n for n in banned_common if re.search(rf"\b{n}\s*\(", code))
        assert not hits, gotcha(
            "router-purity",
            f"{name} calls renderer/row/composer functions directly {hits} — "
            "those are EFFECTS: call the transition and let the fx map bind "
            "the name (design §5.2; direct calls are the second decision path "
            "BK-019 removes)",
        )
    # The BOOKKEEPING literals are parsed routing tables: a type listed there
    # but never routed by name would be marked and then fall through to the
    # paint tail (the "lists stay in sync" gotcha — pair the literal with a
    # USE assertion in the handler). This is the chat sink's ONLY wiring
    # coverage: no browser test drives T03/T10–T14 there (the scroll probe
    # runs the happy path; pwa_resume drives handleRunEvent only), and the
    # tier tests the transitions, never this router.
    def bookkeeping(path: str) -> set[str]:
        text = (root / path).read_text()
        m = re.search(r"const BOOKKEEPING = \{(.*?)\};", text, re.S)
        assert m, gotcha(
            "router-purity",
            f"{path} lost its BOOKKEEPING routing table — the sink-side dedupe "
            "mark cannot see which types it owns",
        )
        return set(re.findall(r"(\w+)\s*:", m.group(1)))

    chat_types = {"title": "titleSeen(", "turn_complete": "turnComplete(",
                  "done": "runFinish(", "stopped": "runStopped(", "error": "runError("}
    run_types = {"title": "titleSeen(", "turn_complete": "turnComplete(",
                 "stopped": "runStopped(", "error": "runError("}
    for path, types, code, required in (
        ("app.js", chat_types, chat, ("screenLeave(", "sessionAnnounce(", "paintChat(")),
        ("events.js", run_types, runs, ("catchingUp(", "paintRerouted(", "runDoneHeal(",
                                        "runDoneWatched(", "runDoneOther(")),
    ):
        declared = bookkeeping(path)
        for ev_type, transition in types.items():
            assert ev_type in declared, gotcha(
                "router-purity",
                f"{path}'s BOOKKEEPING no longer lists `{ev_type}` — the sink "
                "would stop claiming that frame for the dedupe and fall through "
                "to the paint tail",
            )
            assert transition in code, gotcha(
                "router-purity",
                f"{path} no longer routes `{ev_type}` through {transition} — a "
                "dropped transition call is invisible to the tier (it tests the "
                "transition, not this router) and no browser test drives this "
                "sink beyond the happy path",
            )
        for call in required:
            assert call in code, gotcha(
                "router-purity",
                f"{path}'s sink no longer calls {call} — that event path has "
                "lost its named transition/fx route",
            )


def test_deps_bags_supply_every_member_the_transitions_read():
    """BK-019 Step 4: runstate's transitions reach the outside world ONLY
    through injected `deps` bags — a bag missing a member the transition
    reads is a TypeError at the first real event, invisible to the DOM-free
    tier (whose fixtures pass a complete fake) and to the fx-map pins. This
    is the ES-module sibling of test_webui_modules_import_sibling_exports_
    they_call, and it earned its keep: the first browser smoke of the wired
    router died on exactly `deps.alreadyPainted is not a function`. Static
    rule: for every transition called with a given bag, all `deps.<k>` reads
    inside that transition must be keys of that bag."""
    root = _repo_root / "src/harness/gateway/webui"
    # BK-034: transitions live in runstate.js + resync.js (the verdict half).
    rs = "\n".join((root / f).read_text() for f in ("runstate.js", "resync.js"))

    def deps_reads(fn: str) -> set[str]:
        start = rs.index(f"export function {fn}(") if f"export function {fn}(" in rs \
            else rs.index(f"export async function {fn}(")
        end = rs.index("\n}", start)
        return set(re.findall(r"\bdeps\.(\w+)", rs[start:end]))

    # Which transition each router calls with which bag (last arg = the bag).
    calls = {
        "app.js": (("paintChat", "CHAT_DEPS"),),
        "events.js": (("paintRerouted", "RUN_DEPS"), ("runDoneHeal", "RUN_DEPS"),
                      ("visibleResume", "RUN_DEPS"), ("watchdogTick", "RUN_DEPS")),
    }
    bags = {
        "CHAT_DEPS": ("app.js", "CHAT_DEPS"),
        "RUN_DEPS": ("events.js", "RUN_DEPS"),
    }
    keys = {}
    for bag, (path, name) in bags.items():
        text = (root / path).read_text()
        block = re.search(rf"const {name} = \{{(.*?)\}};", text, re.S)
        assert block, gotcha(
            "deps-bag-completeness",
            f"{name} is no longer a literal object bag in {path} — the "
            "injection shape changed; re-point this pin",
        )
        keys[bag] = set(
            re.findall(r"([A-Za-z]\w*)(?=\s*(?:[:,]|\s*$))", block.group(1), re.M)
        )
        assert keys[bag], gotcha(
            "deps-bag-completeness",
            f"{name} in {path} parsed empty — the scan broke; fix it before "
            "trusting the coverage claim",
        )
    for path, pairs in calls.items():
        for fn, bag in pairs:
            # Confirm the call really exists with that bag (guards the table
            # above drifting from the routers).
            text = (root / path).read_text()
            assert re.search(rf"{fn}\([^)]*\b{bag}\b", text), gotcha(
                "deps-bag-completeness",
                f"{path} no longer calls {fn} with {bag} — this pin's call "
                "table went stale; re-point it",
            )
            need = deps_reads(fn)
            missing = sorted(need - keys[bag])
            assert not missing, gotcha(
                "deps-bag-completeness",
                f"{bag} lacks {missing} that runstate's {fn} reads via deps.* "
                "— TypeError on the first real event that routes there",
            )
    # T27 runs INSIDE runDoneHeal/visibleResume/watchdogTick — the same
    # RUN_DEPS bag flows into it, so its six deps reads belong to that bag too.
    missing = sorted(deps_reads("resyncVerdict") - keys["RUN_DEPS"])
    assert not missing, gotcha(
        "deps-bag-completeness",
        f"RUN_DEPS lacks {missing} that runstate's resyncVerdict reads via "
        "deps.* — the verdict driver TypeErrors mid-heal on the first real "
        "resume/run_done/watchdog tick",
    )
    # BK-034: the observe watch's timer (watch.js) receives events.js's bag
    # and hands it to resync.js's observeTick (→ T27) — the same bag flows.
    events_src = (root / "events.js").read_text()
    watch_src = (root / "watch.js").read_text()
    assert re.search(r"watchObserved\(RUN_DEPS, RUN_FX\)", events_src) \
            and re.search(r"observeTick\(deps\)", watch_src), gotcha(
        "deps-bag-completeness",
        "the observe watch no longer hands events.js's RUN_DEPS through "
        "watch.js to observeTick — this pin's call chain went stale; re-point it",
    )
    missing = sorted(deps_reads("observeTick") - keys["RUN_DEPS"])
    assert not missing, gotcha(
        "deps-bag-completeness",
        f"RUN_DEPS lacks {missing} that resync.js's observeTick reads via deps.*",
    )


def test_recover_debug_capture_reads_on_the_phone():
    """The ?recoverdebug=1 on-device capture exists for the iPhone's Safari /
    WebKit, and two details decide whether it is usable there:
    (1) Error.stack frames read `fn@url:line:col` — NOT V8's `at fn (url)`
    (measured with the bundled jsc). The pump's frame filter was copied from
    the Chromium probe's logLine, which is V8-only: on the phone it dropped
    every frame, leaving the CALLER — the one column the capture was upgraded
    to collect — empty. (2) A default <pre> is `white-space: pre`, and a stack
    line is a long URL: on a phone it widens #chat-log into a sideways scroller
    (the card-swing gotcha) and the trace cannot be read. Both are pinned here
    because the browser tier cannot see them (Chromium-only probe).

    Pinned in tests/test_conventions.py."""
    # BK-023 Step 2 moved the frame filter verbatim from app.js's capLine into
    # trace.js's renderEntry (the capture's formatter now lives behind the
    # DOM-free jsunit tier's reach of app.js's 420-line cap). The guarantee is
    # "the filter exists and accepts both dialects" — re-pointed to its home.
    trace = (_repo_root / "src/harness/gateway/webui/trace.js").read_text()
    assert re.search(
        r'\.filter\(\(s\) => \(s\.startsWith\("at "\) \|\| s\.includes\("@"\)\)',
        trace,
    ), gotcha(
        "recoverdebug-webkit-stack",
        "the on-device capture's stack-frame filter (trace.js renderEntry) is "
        "V8-only, gone, or left trace.js — Safari/WebKit frames "
        "(`fn@url:line:col`) are dropped, so ?recoverdebug=1 shows the "
        "diagnostic entries with no caller on the very device it was built for",
    )
    app = (_repo_root / "src/harness/gateway/webui/app.js").read_text()
    events = (_repo_root / "src/harness/gateway/webui/events.js").read_text()
    assert re.search(
        r'pre\.style\.whiteSpace = "pre-wrap";[\s\S]{0,200}?'
        r'pre\.style\.maxHeight = "40vh";',
        events,
    ), gotcha(
        "recoverdebug-webkit-stack",
        "the capture <pre> lost its wrap/height style: long stack URLs make "
        "#chat-log a horizontal scroller on a phone (and disable the pump's "
        "scrollTop follow) — the trace is unreadable where it is meant to be read",
    )
    # BK-022 extension (never weakening the two above) — the premises the
    # debug-icon arming path rests on: arming reads ONLY the launch URL (the
    # debug manifest's start_url is what puts ?recoverdebug=1 there; a second
    # arming source, or one that reads anything else, silently breaks the
    # icon path), and the pump's per-tick re-ensure is what survives
    # resetTranscript() wiping the <pre> (BK-019's board note) — the re-arm
    # must live INSIDE the 2 s interval body, not merely at boot.
    assert '/[?&]recoverdebug=1(&|$)/.test(location.search)' in events, gotcha(
        "recoverdebug-arming",
        "armRecoverDebug no longer gates on the location.search regex — the "
        "debug icon arms the capture ONLY by launching a URL that carries "
        "?recoverdebug=1, so arming must keep reading the launch URL",
    )
    # Anchored on the pump's FIRST statement: the other setInterval in app.js
    # (stall detector) would otherwise match across to this interval's closer,
    # and the boot-time armRecoverDebug() call inside that span would keep
    # this pin green over a pump that stopped re-arming.
    pump = re.search(
        r"setInterval\(\(\) => \{\s*const pre = ([\s\S]*?)\}, 2000\);", app
    )
    assert pump and "_thinkLog" in pump.group(1), gotcha(
        "recoverdebug-arming",
        "app.js's 2 s capture pump (the interval draining _thinkLog) no "
        "longer matches its pinned shape — re-point this pin before "
        "trusting the re-arm claim",
    )
    assert "armRecoverDebug()" in pump.group(1), gotcha(
        "recoverdebug-arming",
        "the 2 s pump no longer re-arms inside its body — after "
        "resetTranscript() wipes the <pre> the capture never returns: the "
        "trace dies mid-run on the suspend/resume case it exists to record",
    )


def test_thinklog_entries_stamp_event_time_at_push():
    """BK-023 criterion 2: the ?recoverdebug=1 capture must tell a paced stream
    from a catch-up burst, and only the EVENT time can do that — the pump
    drains every 2 s, so stamps taken at drain time collapse both shapes onto
    one timestamp. That makes `Date.now()` INSIDE the tlog push load-bearing in
    all THREE copies of the helper (thinking/transcript/events keep it as
    copies: the thinking↔transcript↔pills triangle is cycle-broken by
    injection, no shared module). And the stack must stay at index 2 —
    pwa_resume probe F/G/H read entries positionally (e[0]/e[1]/e[2]) — so a
    no-stack entry pads index 2 with an explicit null and the stamp is always
    e[3]. A future edit silently dropping the stamp would pass every behavioral
    probe while unmaking the criterion this pin guards.

    Pinned in tests/test_conventions.py."""
    for rel in ("thinking.js", "transcript.js", "events.js"):
        text = (_repo_root / "src/harness/gateway/webui" / rel).read_text()
        assert re.search(
            r"L\.push\(\[name, state, withStack \? new Error\(\)\.stack : null, "
            r"Date\.now\(\)\]\)",
            text,
        ), gotcha(
            "recoverdebug-event-time",
            f"{rel}'s tlog no longer stamps event time at push, or moved the "
            "stack off index 2 — the capture cannot distinguish a paced stream "
            "from a catch-up burst (BK-023 criterion 2) and the probe's "
            "positional e[0..2] readers misalign",
        )


def test_debug_manifest_arms_its_own_icon():
    """BK-022: the DEBUG home-screen icon is what arms the ?recoverdebug=1
    capture — its manifest's start_url carries the param the production "/"
    cannot. iOS keys installs on manifest identity, so the debug manifest
    must carry an EXPLICIT id distinct from production's EFFECTIVE id: an
    id-less debug manifest still resolves distinct via its start_url, which a
    naive "id differs" check would pass while silently returning iOS to
    default-identity behavior (D4) — so the pin requires the member PRESENT,
    and resolves both ids the same way (production has no id ⇒ effective id
    is its start_url, per the W3C default). name/short_name must differ (the
    add-sheet label is part of iOS identity), display must be standalone
    (what makes the icon open as a web app — the PWA being tested), scope
    explicit "/", and the icons listed must exist (parsed list + the USE:
    agreement without application is the sync-pin gotcha).

    Pinned in tests/test_conventions.py."""
    import json
    from urllib.parse import urljoin

    webui = _repo_root / "src/harness/gateway/webui"
    dbg_path = webui / "manifest-debug.webmanifest"
    assert dbg_path.exists(), gotcha(
        "recoverdebug-debug-manifest",
        "webui/manifest-debug.webmanifest is gone — the debug icon has no "
        "manifest to install, so the capture cannot arm from a home-screen "
        "launch (BK-022 criterion 1)",
    )
    try:
        dbg = json.loads(dbg_path.read_text())
        prod = json.loads((webui / "manifest.webmanifest").read_text())
    except ValueError as exc:
        pytest.fail(gotcha(
            "recoverdebug-debug-manifest",
            f"a manifest is not valid JSON ({exc}) — iOS rejects it and the "
            "debug icon installs the normal app instead",
        ))
    assert "recoverdebug=1" in str(dbg.get("start_url", "")), gotcha(
        "recoverdebug-debug-manifest",
        "the debug manifest's start_url no longer carries recoverdebug=1 — "
        "the icon launches with an empty location.search, armRecoverDebug() "
        "returns null, and the whole point of this icon is gone",
    )
    assert "id" in dbg, gotcha(
        "recoverdebug-debug-manifest",
        "the debug manifest's id member was removed — with the default id "
        "(start_url) iOS is back to default-identity behavior toward the two "
        "installs; this pin requires the member present, not merely a value "
        "that happens to differ",
    )
    dbg_id = urljoin("http://host/manifest-debug.webmanifest", dbg["id"])
    prod_id = urljoin("http://host/manifest.webmanifest",
                      prod.get("id") or prod["start_url"])
    assert dbg_id != prod_id, gotcha(
        "recoverdebug-debug-manifest",
        f"debug id resolves to {dbg_id} == production's effective id "
        f"{prod_id} — iOS treats the installs as one app: the debug icon "
        "replaces (or arms) the normal Sidekick (D2/D4)",
    )
    for member in ("name", "short_name"):
        assert dbg.get(member) and dbg[member] != prod.get(member), gotcha(
            "recoverdebug-debug-manifest",
            f"the debug manifest's {member} is missing or equal to "
            "production's — the two icons are indistinguishable at add time, "
            "and the name is part of iOS install identity",
        )
    assert dbg.get("display") == "standalone", gotcha(
        "recoverdebug-debug-manifest",
        "the debug manifest lost display:standalone — the icon opens as a "
        "browser tab, not the standalone PWA the capture was built for",
    )
    assert dbg.get("scope") == "/", gotcha(
        "recoverdebug-debug-manifest",
        "the debug manifest lost scope:\"/\" — the spec's stated best "
        "practice; it keeps the debug app's scope honest if start_url moves",
    )
    icons = dbg.get("icons") or []
    assert icons, gotcha(
        "recoverdebug-debug-manifest",
        "the debug manifest lists no icons — iOS falls back to a screenshot "
        "or monogram for the install",
    )
    for icon in icons:
        assert (webui / str(icon.get("src", "")).lstrip("/")).is_file(), gotcha(
            "recoverdebug-debug-manifest",
            f"the debug manifest lists icon {icon.get('src')!r}, which does "
            "not exist under webui/ — the icon path is broken at add time",
        )


def test_production_manifest_stays_clean():
    """D2 byte-contract guard: the PRODUCTION manifest stays out of this
    whole affair — start_url "/" is the production launch contract, the
    device checklist's stale-cache diagnosis reads that exact value, and the
    normal icon must NEVER arm a debug capture. First pin on that file; any
    later edit creeping the param (or anything recoverdebug-shaped) in goes
    red here.

    Pinned in tests/test_conventions.py."""
    import json

    prod_path = _repo_root / "src/harness/gateway/webui/manifest.webmanifest"
    raw = prod_path.read_text()
    assert "recoverdebug" not in raw, gotcha(
        "recoverdebug-production-clean",
        "recoverdebug crept into the production manifest — the normal "
        "home-screen icon would arm a debug capture (D2: it must never)",
    )
    try:
        prod = json.loads(raw)
    except ValueError as exc:
        pytest.fail(gotcha(
            "recoverdebug-production-clean",
            f"manifest.webmanifest is not valid JSON ({exc}) — the installed "
            "PWA loses its manifest entirely",
        ))
    assert prod.get("start_url") == "/", gotcha(
        "recoverdebug-production-clean",
        f"production start_url is {prod.get('start_url')!r}, not \"/\" — the "
        "production launch contract moved, and the checklist's stale-cache "
        "diagnosis leans on this exact value",
    )
    assert prod.get("display") == "standalone", gotcha(
        "recoverdebug-production-clean",
        "the production manifest lost display:standalone — the saved icon "
        "opens as a browser tab, not a web app",
    )


def test_debug_page_links_the_debug_manifest_and_never_redirects():
    """debug.html is the launcher whose linked manifest iOS installs (the
    add-time install starts from the page being added), so the link tag IS
    the debug-icon install path — and the URL bar must still read
    /debug.html when Share → Add to Home Screen opens: a redirect anywhere
    would add the NORMAL page with the NORMAL manifest (D4). The launcher is
    static by design, so this bans the whole navigation class, not just a
    meta-refresh pointed at "/": no script tag, no http-equiv, no location
    writes (script-src 'self' would refuse an inline script anyway — the ban
    keeps the page honest to read). The href is parsed and RESOLVES to the
    file the manifest pin checks — agreement without application is the
    sync-pin gotcha.

    Pinned in tests/test_conventions.py."""
    from urllib.parse import urljoin

    webui = _repo_root / "src/harness/gateway/webui"
    page = webui / "debug.html"
    assert page.exists(), gotcha(
        "recoverdebug-launcher-static",
        "webui/debug.html is gone — there is no page to install the debug "
        "icon FROM, so the debug manifest is never read at add time",
    )
    html = page.read_text()
    link = re.search(r'<link\s+rel="manifest"[^>]*href="([^"]+)"', html)
    assert link, gotcha(
        "recoverdebug-launcher-static",
        'debug.html has no <link rel="manifest"> — iOS reads the manifest '
        "linked from the page being added; without the link the install "
        "picks up the normal app",
    )
    # EXACTLY one manifest link, because the search above finds the FIRST
    # match: a second one (a commented-out debug link ahead of a live
    # /manifest.webmanifest link reads this way) would satisfy the checks
    # below while the install picks the normal app — a green light over the
    # exact failure this launcher exists to prevent.
    n_links = html.count('rel="manifest"')
    assert n_links == 1, gotcha(
        "recoverdebug-launcher-static",
        f"debug.html carries {n_links} manifest links, not 1 — the checks "
        "below judge the FIRST match, so a second (or a commented-out) link "
        "can hide a page that really installs /manifest.webmanifest",
    )
    target = urljoin("/debug.html", link.group(1))
    assert target == "/manifest-debug.webmanifest", gotcha(
        "recoverdebug-launcher-static",
        f"debug.html links {target!r}, not /manifest-debug.webmanifest — the "
        "debug icon would install whatever that manifest describes (D4)",
    )
    assert (webui / target.lstrip("/")).is_file(), gotcha(
        "recoverdebug-launcher-static",
        f"debug.html links {target!r} but that file does not exist — a 404 "
        "manifest at add time installs the normal app",
    )
    assert 'href="/?recoverdebug=1"' in html, gotcha(
        "recoverdebug-launcher-static",
        "debug.html lost its /?recoverdebug=1 anchor — the BK-019 Safari-tab "
        "vehicle is no longer reachable from the launcher",
    )
    # BK-023: the capture now renders event-stamped lines and collapses runs
    # of no-op endThinking closes into one ×N line. This page is what a device
    # run is read against, so the retired "numbered, one line per event" shape
    # must not survive here — an armed capture would read as broken.
    for retired in ("numbered trace", "one line per logged event"):
        assert retired not in html, gotcha(
            "recoverdebug-launcher-static",
            f"debug.html still describes the capture as a {retired!r} trace — "
            "BK-023 replaced ordinals with event stamps and collapses no-op "
            "runs, so a correctly armed capture would look un-armed on device",
        )
    for banned in ("<script", "http-equiv", "window.location", "location.href"):
        assert banned not in html, gotcha(
            "recoverdebug-launcher-static",
            f"debug.html contains {banned!r} — the launcher is static by "
            "design; ANY navigation (meta-refresh, script, location write) "
            "can move the URL bar off /debug.html before Add to Home Screen, "
            "and the saved icon picks up the NORMAL manifest (D4)",
        )


def test_attach_picker_extension_lists_stay_in_sync():
    """The 📎 picker's accept= (index.html), attach.js's client-side gate, and
    gateway/media.py's _DOC_EXTS are three lists of one decision. The server
    list's sync pins (MEDIA_ID_RE, _EXT_TO_MIME) do NOT cover the other two:
    add .csv server-side and the picker silently hides it; widen accept= alone
    and the client gate rejects the file before the server ever sniffs it.
    Source pin — all three must agree (plan doc-attachments Step 3).

    Pinned in tests/test_conventions.py."""
    from harness.gateway import media

    doc_exts = {e.lstrip(".") for e in media._DOC_EXTS}

    html = (_repo_root / "src/harness/gateway/webui/index.html").read_text()
    accept = re.search(r'id="chat-attach-input"[^>]*accept="([^"]+)"', html)
    assert accept, gotcha(
        "doc-attach-ext-sync", "chat-attach-input accept= missing from index.html"
    )
    tokens = {t.strip() for t in accept.group(1).split(",") if t.strip()}
    assert "image/*" in tokens, gotcha(
        "doc-attach-ext-sync", "accept= lost image/* — photos must stay pickable"
    )
    picked = tokens - {"image/*"}
    assert picked == {f".{e}" for e in doc_exts}, gotcha(
        "doc-attach-ext-sync",
        f"index.html accept= docs {sorted(picked)} != media._DOC_EXTS "
        f"{sorted(media._DOC_EXTS)}",
    )

    js = (_repo_root / "src/harness/gateway/webui/attach.js").read_text()
    gate = re.search(r"DOC_EXT_RE\s*=\s*/\\\.\(\?:([a-z|]+)\)\$/i", js)
    assert gate, gotcha(
        "doc-attach-ext-sync",
        "attach.js DOC_EXT_RE literal drifted from its pinned shape",
    )
    gated = set(gate.group(1).split("|"))
    assert gated == doc_exts, gotcha(
        "doc-attach-ext-sync",
        f"attach.js gate {sorted(gated)} != media._DOC_EXTS {sorted(doc_exts)}",
    )
    # Non-vacuity: the literal must actually GATE the picker. A defined but
    # unused DOC_EXT_RE would pass every sync above while onPick rejected
    # every document (isImg false, no gate match).
    assert "DOC_EXT_RE.test(file.name)" in js, gotcha(
        "doc-attach-ext-sync",
        "attach.js defines DOC_EXT_RE but no longer applies it in onPick",
    )


def test_bubble_doc_chips_are_display_only():
    """Doc chips must NEVER grow into links or previews: GET /media/<doc>
    serves Content-Disposition: attachment, and ANY navigation out of the
    standalone PWA strands the user with no back affordance (the target=_blank
    trap, see lightbox.js's header — the reason v1 chips are display-only). Filenames are
    server-sanitized but still untrusted-shaped, so they ride textContent.
    Source pin: the docChip builder (last function in thumbs.js — the slice
    from there to EOF is the pin's scope) creates spans via el() only and
    never touches href/target/innerHTML/lightbox plumbing; the image branch
    keeps living BEFORE that slice, so the pin cannot pass vacuously.

    Pinned in tests/test_conventions.py."""
    js = (_repo_root / "src/harness/gateway/webui/thumbs.js").read_text()
    start = js.find("function docChip")
    assert start >= 0, gotcha(
        "doc-chip-display-only", "docChip builder missing from thumbs.js"
    )
    region = js[start:]
    assert 'el("span", "doc-chip")' in region, gotcha(
        "doc-chip-display-only", "docChip no longer builds a plain <span>"
    )
    for banned in ("href", "target", "innerHTML", "attach-open", "window.open",
                   "createObjectURL", "blob:"):
        assert banned not in region, gotcha(
            "doc-chip-display-only",
            f"docChip region mentions {banned!r} — document chips are "
            "display-only; a link or generated URL navigates (or exfils) "
            "out of the standalone PWA",
        )
    assert 'el("a", "attach-open")' in js[:start], gotcha(
        "doc-chip-display-only",
        "the image branch no longer builds a.attach-open before docChip — "
        "either photos lost the lightbox or the pin's scope moved; re-check "
        "both before changing thumbs.js",
    )


def test_composer_doc_chips_are_display_only():
    """The composer's doc chip (attach.js addChip) is the OTHER render path —
    same display-only rule: no link, no preview, no generated URL (a tap that
    navigates out of the standalone PWA strands the user). Photos there render
    as a bare <img>, so the whole module is in scope; `e.target` in the pick
    handler is not a link. The `el("span", "doc-name", file.name)` anchor
    keeps the scan non-vacuous: deleting the chip code or re-routing the name
    through markup both go red.

    Pinned in tests/test_conventions.py."""
    js = (_repo_root / "src/harness/gateway/webui/attach.js").read_text()
    for banned in ('el("a"', 'setAttribute("href"', 'setAttribute("target"',
                   "innerHTML", "window.open", "createObjectURL",
                   "location.assign", "location.href"):
        assert banned not in js, gotcha(
            "doc-chip-display-only",
            f"attach.js mentions {banned!r} — composer doc chips are "
            "display-only; a link or generated URL navigates (or exfils) out "
            "of the standalone PWA",
        )
    assert 'el("span", "doc-name", file.name)' in js, gotcha(
        "doc-chip-display-only",
        "composer doc chip no longer renders the picked filename as plain "
        "text via el() (textContent) — re-check the XSS/display-only rules",
    )
