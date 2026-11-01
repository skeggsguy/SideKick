"""Tool profiles: the closed per-run subsets of the catalog (P6)."""

from __future__ import annotations

from typing import Any

import pytest

from harness import playbooks
from harness.core.agent import Agent
from harness.core.policy import ActionClass, PolicyEngine
from harness.core.registry import ActiveToolset, ToolRegistry
from harness.core.session import Session
from harness.core.types import LLMResponse, ToolCall
from harness.tools.base import Tool
from conftest import gotcha

from test_agent_loop import EchoTool, ScriptedProvider


class WeatherTool(Tool):
    name = "weather_lookup"
    description = "Look up the current weather forecast for a location."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    }
    classification = ActionClass.READ_ONLY

    async def run(self, args: dict[str, Any]) -> str:
        return f"sunny in {args['city']}"


def make_registry(*tools: Tool) -> ToolRegistry:
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return registry


def test_toolset_defaults_to_whole_catalog():
    toolset = ActiveToolset(make_registry(EchoTool(), WeatherTool()))
    assert [t.name for t in toolset.tools()] == ["echo", "weather_lookup"]
    assert toolset.get("echo") is not None


def test_profile_restricts_and_reports_unknown_names():
    toolset = ActiveToolset(make_registry(EchoTool(), WeatherTool()))
    unknown = toolset.set_profile(["weather_lookup", "no_such_tool"])
    assert unknown == ["no_such_tool"]
    assert [t.name for t in toolset.tools()] == ["weather_lookup"]
    assert toolset.get("echo") is None  # inactive
    assert toolset.in_catalog("echo") is True


async def test_profile_is_a_closed_set_for_the_whole_run():
    """Every profile is a closed boundary (search_tools was retired
    2026-08-11, so there is no discovery path any more): a catalog tool
    outside the profile stays inactive, calling it is an error with no
    hint, and the schema list the provider sees never grows mid-run —
    which is what keeps an autonomous job's allocation a real bound and
    the cached prefix byte-stable."""
    registry = make_registry(EchoTool(), WeatherTool())
    provider = ScriptedProvider(
        [
            LLMResponse(
                text=None,
                tool_calls=[
                    ToolCall(id="t1", name="weather_lookup", arguments={"city": "x"})
                ],
            ),
            LLMResponse(text="ok"),
        ]
    )
    agent = Agent(
        provider=provider,
        registry=registry,
        system_prompt="test",
        policy=PolicyEngine(),
    )
    session = Session()

    await agent.run_turn(session, "hi", tool_profile=["echo"])

    # In the catalog, but not in this run's profile — and unreachable.
    assert agent.toolset.in_catalog("weather_lookup") is True
    assert agent.toolset.get("weather_lookup") is None
    result = session.messages[2].tool_results[0]
    assert result.is_error is True
    assert "not active" in result.content
    # No discovery hint: nothing can activate a tool any more, so telling
    # the model to go looking would send it into a loop.
    assert "search_tools" not in result.content
    # The refused call must not have grown the active set for the next step.
    assert provider.sent_tools == [["echo"], ["echo"]]


def test_playbook_frontmatter_declares_tool_profile():
    pb = playbooks.resolve("morning_briefing")
    assert pb.name == "morning_briefing"
    assert pb.tools is not None
    assert "calendar_list_events" in pb.tools
    # The feeds tools ride the briefing profile (step 3): a profile turns
    # tool-grouping OFF, so listing them here is what makes them callable.
    assert {"rss_fetch", "reddit_comments", "hn_comments"} <= set(pb.tools)
    assert "rss_fetch" in pb.prompt  # the instruction paragraph names the tool
    assert len(pb.tools) < 25
    assert not pb.prompt.startswith("---")
    assert "morning briefing" in pb.prompt.lower()


def test_inline_prompt_has_no_profile():
    pb = playbooks.resolve("Remind me: submit the report")
    assert pb.name is None
    assert pb.tools is None
    assert pb.prompt == "Remind me: submit the report"


def test_duplicate_tool_name_is_loud():
    """Registering two tools with the same name must fail loud and raise
    ValueError. A silent collision (shadowing) would be a gotcha — the UI and
    policy layer would silently use the first, while the agent might try to
    call the second and get a different tool.

    This is also caught per-server in servers.toml (ms365 → outlook_*), but
    the registry is the final enforcement.

    Pinned in tests/test_tool_scaling.py."""
    registry = ToolRegistry()
    registry.register(EchoTool())

    class DupeEcho(Tool):
        name = "echo"
        description = "Also named echo"
        input_schema = {"type": "object", "properties": {}}
        classification = ActionClass.READ_ONLY

        async def run(self, args):
            return "different"

    with pytest.raises(ValueError) as excinfo:
        registry.register(DupeEcho())

    assert "Duplicate tool name" in str(excinfo.value), gotcha(
        "mcp-tool-name-collision",
        "registry.register must raise ValueError on duplicate tool name, never silently shadow "
        "(adapted MCP tools keep raw names — prefix per server, as ms365 uses outlook_*)"
    )
