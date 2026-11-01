"""The main system prompt (harness/prompt.py) stays lean: cross-cutting rules
only, tool-specific guidance in the tool descriptions (decisions 2026-10-18,
prompt-trim option A). It rides every request on the local lane's prefix."""

from __future__ import annotations

from harness.core.paths import SCRATCH_DIR
from harness.prompt import SYSTEM_PROMPT
from harness.tools.files import EditFileTool


def test_system_prompt_carries_no_tool_specific_paragraphs():
    """The sandbox and file-area paragraphs moved to sandbox_run's and
    edit_file's descriptions — restating them here is the duplication the
    trim removed (and sandbox_run is not even declared on grouped turns)."""
    assert "sandbox_run" not in SYSTEM_PROMPT
    assert "FILE AREA" not in SYSTEM_PROMPT
    assert str(SCRATCH_DIR) not in SYSTEM_PROMPT


def test_system_prompt_names_recall_and_ops_lessons():
    """Recall was the weak spot (the Mount Franklin chat never called it): the
    prompt must name the tool and the ops-lessons moment, and the save-back."""
    assert "recall" in SYSTEM_PROMPT
    assert "ops lessons" in SYSTEM_PROMPT
    assert "remember" in SYSTEM_PROMPT


def test_system_prompt_keeps_the_timezone_rule():
    assert "current_time" in SYSTEM_PROMPT
    assert "convert_time" in SYSTEM_PROMPT


def test_system_prompt_length_tripwire():
    """≈260 tokens at the trim (1,259 chars). A tripwire, not a goal: growing
    past it means guidance is creeping back in that belongs in a tool
    description — measure with scripts/prompt_budget.py before raising it."""
    assert len(SYSTEM_PROMPT) <= 1500, len(SYSTEM_PROMPT)


def test_edit_file_states_the_absolute_file_area():
    """The file area's real path now lives only in edit_file's description
    (the base prompt's FILE AREA paragraph is gone)."""
    assert str(SCRATCH_DIR) in EditFileTool.description
