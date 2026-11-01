"""Playbooks: named markdown prompts for scheduled runs.

resolve() returns the playbook when the name matches a .md file here;
otherwise the name itself is treated as an inline prompt — that's how
one-off reminders work ("Remind me: submit the report" is stored directly
in the trigger's playbook column).

A playbook may open with a frontmatter block declaring its tool profile —
the fixed subset of tools its runs expose to the model (small and
deterministic, so the prompt-cache prefix stays stable). No `tools:` line
(or an inline prompt) means the full toolset:

    ---
    tools: gmail_search, gmail_read, calendar_list_events
    ---
    Produce my morning briefing...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Rows made from a template hold a COPY of it (schedule_tools / the UI copy at
# creation): a load-bearing fix to a .md here must also be patched into live rows.
PLAYBOOKS_DIR = Path(__file__).parent

_NAME_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass
class Playbook:
    prompt: str
    #: None when the input was an inline prompt rather than a named playbook.
    name: str | None
    #: Tool profile; None means the run gets the full toolset.
    tools: list[str] | None = None


def resolve(name_or_prompt: str) -> Playbook:
    if _NAME_RE.match(name_or_prompt):
        path = PLAYBOOKS_DIR / f"{name_or_prompt}.md"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            meta, prompt = _split_frontmatter(text)
            return Playbook(prompt=prompt, name=name_or_prompt, tools=meta.get("tools"))
    return Playbook(prompt=name_or_prompt, name=None)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse an optional leading `---` block of `key: value` lines. Only
    `tools` (comma-separated) is meaningful today; unknown keys are ignored
    so playbooks can grow metadata without code churn."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta: dict = {}
    for line in text[4:end].splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "tools" and value.strip():
            meta["tools"] = [t.strip() for t in value.split(",") if t.strip()]
    return meta, text[end + 5 :].lstrip("\n")


def available() -> list[str]:
    return sorted(p.stem for p in PLAYBOOKS_DIR.glob("*.md"))


@dataclass
class ResolvedJob:
    playbook: Playbook
    #: Effective tool allocation: the trigger row's list when set (new-style
    #: self-contained jobs), else playbook frontmatter, else None (full set).
    tools: list[str] | None
    autonomy: str
    #: Set when the stored state was unsafe and got downgraded (traced by
    #: callers so a hand-edited row doesn't fail silently).
    downgraded: str | None = None


def resolve_job(
    playbook_field: str,
    row_tools: list[str] | None,
    autonomy: str,
) -> ResolvedJob:
    """Resolve a trigger row into what its run should get. Shared by the
    orchestrator and the CLI inline-fire path so the two can't drift.

    Fail-safe: an unknown autonomy value, or an autonomous job with no tool
    allocation (only reachable via a hand-edited row — create/edit validate
    it), downgrades to supervised rather than running unbounded."""
    pb = resolve(playbook_field)
    tools = row_tools if row_tools is not None else pb.tools
    downgraded = None
    if autonomy not in ("interactive", "supervised", "autonomous", "restricted"):
        downgraded = f"unknown autonomy {autonomy!r} → supervised"
        autonomy = "supervised"
    if autonomy == "autonomous" and not tools:
        downgraded = "autonomous job with no tool allocation → supervised"
        autonomy = "supervised"
    return ResolvedJob(
        playbook=pb,
        tools=tools,
        autonomy=autonomy,
        downgraded=downgraded,
    )
