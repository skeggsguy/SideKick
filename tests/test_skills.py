"""
Offline tests for skills and subagents.

These are static path/phrase checks to catch when skills rot against the real codebase.
No harness imports (offline); no MCP spawning.
"""

import re
from pathlib import Path


def test_every_skill_has_frontmatter():
    """Each `.claude/skills/*/SKILL.md` must have name and description in frontmatter."""
    skills_dir = Path(__file__).parent.parent / ".claude" / "skills"
    assert skills_dir.exists(), f"Skills directory not found: {skills_dir}"

    skill_dirs = [d for d in skills_dir.iterdir() if d.is_dir()]
    assert len(skill_dirs) >= 4, f"Expected ≥4 skill dirs, found {len(skill_dirs)}"

    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        assert skill_file.exists(), f"Missing SKILL.md in {skill_dir.name}"

        content = skill_file.read_text()
        # Parse YAML frontmatter
        match = re.match(r'---\n(.*?)\n---', content, re.DOTALL)
        assert match, f"No YAML frontmatter in {skill_file}"

        frontmatter = match.group(1)
        assert re.search(r'name:\s*\S+', frontmatter), f"No 'name' in {skill_file} frontmatter"
        assert re.search(r'description:\s*.+', frontmatter), f"No 'description' in {skill_file} frontmatter"

        # Description should contain a trigger phrase
        assert re.search(r'(?:Use|BEFORE|after|when)', content), \
            f"No trigger phrase (Use/BEFORE/after/when) in {skill_file}"


def test_agent_file_has_frontmatter():
    """The webui-verify agent must have name, description, model, and tools."""
    agent_file = Path(__file__).parent.parent / ".claude" / "agents" / "webui-verify.md"
    assert agent_file.exists(), f"Agent file not found: {agent_file}"

    content = agent_file.read_text()
    match = re.match(r'---\n(.*?)\n---', content, re.DOTALL)
    assert match, "No YAML frontmatter in webui-verify.md"

    frontmatter = match.group(1)
    assert re.search(r'name:\s*webui-verify', frontmatter), "Agent must be named 'webui-verify'"
    assert re.search(r'model:\s*sonnet', frontmatter), "Agent must specify model: sonnet"
    assert re.search(r'tools:\s*Bash.*Read.*Write', frontmatter), "Agent must list Bash, Read, Write tools"
    assert 'description:' in frontmatter, "Agent must have a description"


def test_skill_referenced_paths_exist():
    """All backtick-quoted paths in skills/agents must exist."""
    skills_dir = Path(__file__).parent.parent / ".claude" / "skills"
    agents_dir = Path(__file__).parent.parent / ".claude" / "agents"
    repo_root = Path(__file__).parent.parent

    # Scan all skill files + agent file
    files_to_scan = []
    if skills_dir.exists():
        files_to_scan.extend(skills_dir.rglob("SKILL.md"))
    if agents_dir.exists():
        files_to_scan.extend(agents_dir.glob("*.md"))

    for file_path in files_to_scan:
        content = file_path.read_text()
        # Extract backtick-quoted paths matching the pattern `(docs|src|tests|scripts)/...`
        paths = re.findall(r'`((?:docs|src|tests|scripts)/[^\s`]+)`', content)

        for path in paths:
            target = repo_root / path
            assert target.exists(), f"Path not found in {file_path.name}: {path}"


def test_new_tool_skill_tracks_egress_constant():
    """The /new-tool skill must name EGRESS_TOOLS and the taint test."""
    skill_file = Path(__file__).parent.parent / ".claude" / "skills" / "new-tool" / "SKILL.md"
    content = skill_file.read_text()

    assert "EGRESS_TOOLS" in content, "Skill must reference EGRESS_TOOLS"
    assert "test_taint_gates_egress_tools" in content, "Skill must reference the taint test name"


def test_adding_tool_doc_has_cited_sections():
    """The adding-a-tool doc must contain sections the skill cites."""
    doc_file = Path(__file__).parent.parent / "docs" / "adding-a-tool.md"
    content = doc_file.read_text()

    # Sections the skills point at
    assert "Safety tiers" in content, "Doc must contain 'Safety tiers' section"
    assert "easy-to-forget refresh" in content, "Doc must name the refresh section"
    assert "EGRESS_TOOLS" in content or "outbound tool" in content, "Doc must discuss outbound tools"


def test_playbook_skill_names_fire_routing():
    """The /new-playbook skill must explain the two fire recipes."""
    skill_file = Path(__file__).parent.parent / ".claude" / "skills" / "new-playbook" / "SKILL.md"
    content = skill_file.read_text()

    assert "HARNESS_PORT" in content, "Skill must explain the port routing issue"
    assert "recipe (a)" in content or "inline" in content, "Skill must name the inline recipe"
    assert "recipe (b)" in content or "scratch daemon" in content, "Skill must name the scratch-daemon recipe"
    assert "confirm" in content.lower() or "CONFIRM" in content, "Skill must require a confirmation gate for real runs"


def test_webui_skill_names_cache_busted_assets():
    """The /webui-change skill must mention cache-busting."""
    skill_file = Path(__file__).parent.parent / ".claude" / "skills" / "webui-change" / "SKILL.md"
    content = skill_file.read_text()

    assert "?v=" in content, "Skill must mention cache-busting with ?v="
    assert "style.css" in content, "Skill must mention style.css"
    assert "app.js" in content, "Skill must mention app.js entry tag"


def test_webui_verify_agent_is_isolated():
    """The webui-verify agent must have isolation rules and prohibitions."""
    agent_file = Path(__file__).parent.parent / ".claude" / "agents" / "webui-verify.md"
    content = agent_file.read_text()

    # Required env vars
    assert "HARNESS_PORT=8899" in content, "Agent must isolate to port 8899"
    assert "HARNESS_DB_PATH" in content, "Agent must use a scratch DB"
    assert "HARNESS_GMAIL_WATCHER=off" in content, "Agent must disable the watcher"
    assert "HARNESS_DB_BACKUP=off" in content, "Agent must disable backups"
    assert "ANTHROPIC_API_KEY=invalid-scratch" in content, "Agent must use a dummy API key"

    # Hard prohibitions
    assert "NEVER" in content and "#chat-input" in content, "Agent must prohibit chat input"
    assert "NEVER" in content and "/api/chat" in content, "Agent must prohibit chat API"
    assert "never modify any file inside the repo" in content.lower(), "Agent must prohibit repo modifications"

    # Playwright setup
    assert "playwright install" in content, "Agent must install Playwright"

    # No paths that would expose secrets
    assert ".local/share/harness" not in content, "Agent must not reference live harness dir"
    assert "/tmp" in content or "tmp" in content, "Agent must write to /tmp for isolation"


def test_release_check_gates_spend():
    """The /release-check skill must gate spend-bearing steps."""
    skill_file = Path(__file__).parent.parent / ".claude" / "skills" / "release-check" / "SKILL.md"
    content = skill_file.read_text()

    assert "pytest -m live" in content, "Skill must mention the live test tier"
    assert "smoke_test.py" in content, "Skill must mention the smoke test"
    assert "CONFIRM WITH TOM" in content, "Skill must gate spend-bearing steps"


def test_release_check_migration_mechanics():
    """The /release-check skill must explain migration mechanics clearly."""
    skill_file = Path(__file__).parent.parent / ".claude" / "skills" / "release-check" / "SKILL.md"
    content = skill_file.read_text()

    assert "mode=ro" in content, "Skill must show read-only DB query syntax"
    assert "schema_migrations" in content, "Skill must reference the schema_migrations table"
    assert "re-run" in content.lower(), "Skill must describe re-checking after migration apply"
