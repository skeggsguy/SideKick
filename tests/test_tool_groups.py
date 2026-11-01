"""Tool groups (core/tool_groups.py): deferred MCP definitions behind the
load_tools / call_tool meta-tools, and the call_tool unwrap-before-policy
safety invariant in Agent._dispatch."""

from __future__ import annotations

import json
from typing import Any

import pytest

from harness import composition
from harness.core import tool_groups
from harness.core.agent import Agent
from harness.core.policy import ActionClass, Autonomy, PolicyEngine
from harness.core.registry import ActiveToolset, ToolRegistry
from harness.core.session import Session
from harness.core.types import LLMResponse, ToolCall
from harness.mcp.manager import MCPTool
from harness.store.audit import AllowlistStore, AuditStore
from harness.store.runs import RunStore
from harness.tools.base import Tool

from conftest import gotcha
from test_agent_loop import (
    EchoTool,
    FakeFetchTool,
    FakeReadTool,
    ScriptedProvider,
)


class FakeMCP(Tool):
    """An MCP-like (deferrable) tool that records its runs."""

    deferrable = True

    def __init__(
        self,
        name: str,
        classification: ActionClass = ActionClass.READ_ONLY,
        schema: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.description = f"The {name} tool."
        self.classification = classification
        self.input_schema = schema or {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }
        self.runs: list[dict] = []

    async def run(self, args: dict[str, Any]) -> str:
        self.runs.append(args)
        return f"{self.name} ran"


def _registry(tools: list[Tool]) -> ToolRegistry:
    registry = ToolRegistry()
    for t in tools:
        registry.register(t)
    return registry


def _catalog() -> list[Tool]:
    """Interleaved on purpose: registration order must decide group order."""
    return [
        EchoTool(),
        FakeMCP("gmail_search"),
        FakeMCP("calendar_list_events"),
        FakeMCP("gmail_send", ActionClass.OUTWARD),
        FakeMCP("tasklists_list"),
        FakeMCP("task_create", ActionClass.MUTATING_LOCAL),
        FakeMCP("docs_create", ActionClass.MUTATING_LOCAL),
        FakeMCP("sheets_read"),
        FakeMCP("mystery_tool"),  # MCP, but no prefix → stays always-on
    ]


def _agent(
    provider: ScriptedProvider,
    tools: list[Tool],
    *,
    grouped: bool = True,
    policy: PolicyEngine | None = None,
    **kwargs: Any,
) -> Agent:
    return Agent(
        provider=provider,
        registry=_registry(tools),
        system_prompt="test",
        policy=policy or PolicyEngine(),
        group_tools=grouped,
        **kwargs,
    )


def _call(call_id: str, name: str, arguments: Any) -> ToolCall:
    return ToolCall(
        id=call_id, name="call_tool", arguments={"name": name, "arguments": arguments}
    )


def _step(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=list(calls))


class SpyPolicy(PolicyEngine):
    """Records every (name, args, classification) the gate is asked about."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.checked: list[tuple[str, dict, ActionClass]] = []

    async def check(self, tool_name, args, classification, context=None):
        self.checked.append((tool_name, args, classification))
        return await super().check(tool_name, args, classification, context=context)


# ---- grouping / prefix map ---------------------------------------------------


def test_group_of_prefix_map_and_unmatched_stays_always_on():
    assert tool_groups.group_of(FakeMCP("calendar_event_create")) == "calendar"
    assert tool_groups.group_of(FakeMCP("gmail_send")) == "gmail"
    assert tool_groups.group_of(FakeMCP("drive_list")) == "drive"
    assert tool_groups.group_of(FakeMCP("sheets_read")) == "sheets_docs"
    assert tool_groups.group_of(FakeMCP("docs_create")) == "sheets_docs"
    for name in ("task_create", "tasks_list", "tasklist_create", "tasklists_list"):
        assert tool_groups.group_of(FakeMCP(name)) == "tasks"
    assert tool_groups.group_of(FakeMCP("outlook_mail_search")) == "outlook"
    # Unmatched MCP tool: visible, never silently hidden.
    assert tool_groups.group_of(FakeMCP("mystery_tool")) is None
    # In-process tools are never deferred by PREFIX, whatever their name —
    # only an exact GROUP_TOOLS entry opts one in.
    in_process = EchoTool()
    in_process.name = "gmail_lookalike"
    assert tool_groups.group_of(in_process) is None
    in_process.name = "schedule_add"
    assert tool_groups.group_of(in_process) == "schedule"
    in_process.name = "schedule_add_v2"
    assert tool_groups.group_of(in_process) is None


def test_mcp_tool_is_deferrable_and_in_process_tools_are_not():
    assert MCPTool.deferrable is True
    assert Tool.deferrable is False


def test_every_group_has_a_purpose_line():
    """The model sees only names until it loads a group, so the purpose line
    is what routes a request there — a group without one gets guessed at
    (2026-09-28: 'remind me Friday 9am' dithered between Tasks, Calendar and
    schedule for ~700 thinking tokens)."""
    groups = {g for _, g in tool_groups.GROUP_PREFIXES} | set(tool_groups.GROUP_TOOLS.values())
    missing = groups - set(tool_groups.GROUP_DESCRIPTIONS)
    assert not missing, sorted(missing)


def test_every_configured_mcp_tool_has_a_group():
    """servers.toml is the list of real MCP tools: each must land in a group,
    or the local lane silently keeps its definition in every prompt."""
    from harness.mcp.manager import DEFAULT_CONFIG, load_server_configs

    for cfg in load_server_configs(DEFAULT_CONFIG):
        for name in cfg.classifications:
            assert tool_groups.group_of(FakeMCP(name)) is not None, (
                cfg.name, name,
                "a new remote tool's gates + group + composition land in ONE step, or the structural scans go red",
            )


def test_grouped_tools_contents_and_deterministic_order():
    toolset = ActiveToolset(_registry(_catalog()), groups_enabled=True)
    sent = toolset.tools()
    assert [t.name for t in sent] == ["echo", "mystery_tool", "load_tools", "call_tool"]
    load = sent[2]
    assert load.input_schema["properties"]["group"]["enum"] == [
        "gmail",
        "calendar",
        "tasks",
        "sheets_docs",
    ]
    assert load.description.startswith(
        "Load a group of tools so you can use them. The tools below exist but "
        "their definitions are not loaded yet; load the group first, then call "
        "them via call_tool."
    )
    assert (
        "\nGroups:\n- gmail: search/read mail, drafts, send, attachments — "
        "gmail_search, gmail_send\n"
    ) in load.description
    assert "- tasks: Google Tasks to-dos (date-only due dates, no times) — tasklists_list, task_create\n" in load.description
    assert load.description.endswith(
        "- sheets_docs: read/write Google Sheets, create/edit Docs — docs_create, sheets_read"
    )
    assert sent[3] is tool_groups.CALL_TOOL_DEF
    assert sent[3].input_schema["required"] == ["name", "arguments"]
    # Byte-stable across calls (the cached prefix).
    again = ActiveToolset(_registry(_catalog()), groups_enabled=True).tools()
    assert [(t.name, t.description, t.input_schema) for t in again] == [
        (t.name, t.description, t.input_schema) for t in sent
    ]


def test_meta_tools_are_not_registry_tools():
    """load_tools/call_tool are definitions only — never in the catalog
    (so never in GET /api/tools, the allowlist, or Tools-Security)."""
    registry = _registry(_catalog())
    toolset = ActiveToolset(registry, groups_enabled=True)
    toolset.tools()
    names = {t.name for t in toolset.catalog()}
    assert not names & tool_groups.META_TOOLS
    assert registry.get("call_tool") is None and registry.get("load_tools") is None
    # Deferred tools stay reachable by lookup.
    assert toolset.get("gmail_send") is not None


def test_meta_tool_names_are_reserved_in_the_registry():
    """A registry tool named call_tool/load_tools would be shadowed by the
    agent's intercept (never run) — refuse it at registration."""
    for name in tool_groups.META_TOOLS:
        with pytest.raises(ValueError, match="Reserved"):
            _registry([FakeMCP(name)])


def test_unwrap_refuses_tools_outside_the_active_profile():
    """Defence in depth under `grouped`: even if unwrap were reached with a
    profile set, it resolves through the ACTIVE toolset, never the registry."""
    toolset = ActiveToolset(_registry(_catalog()), groups_enabled=True)
    toolset.set_profile(["gmail_search"])
    inner = tool_groups.unwrap_call_tool(_call("c1", "gmail_send", {"query": "x"}), toolset)
    assert isinstance(inner, str) and "unknown tool 'gmail_send'" in inner
    ok = tool_groups.unwrap_call_tool(_call("c2", "gmail_search", {"query": "x"}), toolset)
    assert isinstance(ok, ToolCall) and ok.id == "c2" and ok.name == "gmail_search"


def test_grouping_off_or_nothing_to_defer_sends_full_catalog():
    catalog = _catalog()
    off = ActiveToolset(_registry(catalog), groups_enabled=False)
    assert [t.name for t in off.tools()] == [t.name for t in catalog]
    assert off.grouped is False
    nothing = ActiveToolset(_registry([EchoTool()]), groups_enabled=True)
    assert [t.name for t in nothing.tools()] == ["echo"]
    assert nothing.grouped is False


def test_profile_disables_grouping():
    toolset = ActiveToolset(_registry(_catalog()), groups_enabled=True)
    toolset.set_profile(["gmail_search", "echo"])
    assert toolset.grouped is False
    assert [t.name for t in toolset.tools()] == ["gmail_search", "echo"]


# ---- load_tools --------------------------------------------------------------


def test_load_tools_result_format_and_unknown_group():
    groups = tool_groups.partition(_catalog())[1]
    content, is_error = tool_groups.load_tools_result({"group": "gmail"}, groups)
    assert is_error is False
    lines = content.split("\n")
    assert lines[0] == "Loaded the gmail tools. Call them via call_tool."
    assert lines[1] == '<tools group="gmail">'
    assert lines[-1] == "</tools>"
    defs = [json.loads(line) for line in lines[2:-1]]
    assert [d["name"] for d in defs] == ["gmail_search", "gmail_send"]
    assert set(defs[0]) == {"name", "description", "input_schema"}
    assert defs[0]["input_schema"]["required"] == ["query"]

    content, is_error = tool_groups.load_tools_result({"group": "nope"}, groups)
    assert is_error is True
    assert "gmail, calendar, tasks, sheets_docs" in content


async def test_load_tools_dispatch_is_traced_not_gated(db):
    runs = RunStore(db)
    policy = SpyPolicy()
    provider = ScriptedProvider(
        [
            _step(ToolCall(id="l1", name="load_tools", arguments={"group": "calendar"})),
            LLMResponse(text="done"),
        ]
    )
    agent = _agent(provider, _catalog(), policy=policy, run_store=runs)
    session = Session()
    await agent.run_turn(session, "what's on thursday")

    result = session.messages[2].tool_results[0]
    assert result.tool_call_id == "l1" and result.is_error is False
    assert '"name": "calendar_list_events"' in result.content
    assert policy.checked == []  # READ_ONLY semantics: nothing to prompt
    events = await runs.events(agent.last_run["run_id"])
    kinds = [(e["kind"], e["data"].get("tool")) for e in events]
    assert ("tool_call", "load_tools") in kinds
    assert ("tool_result", "load_tools") in kinds
    # tools_sent counts what was actually sent: echo, mystery_tool + 2 meta.
    sent = [e["data"]["tools_sent"] for e in events if e["kind"] == "model_response"]
    assert sent == [4, 4]
    assert provider.sent_tools[0] == ["echo", "mystery_tool", "load_tools", "call_tool"]


# ---- call_tool unwrap: the safety invariant --------------------------------


async def test_call_tool_policy_sees_inner_name_and_class(db):
    """An OUTWARD MCP tool reached via call_tool is gated EXACTLY as a direct
    call would be: same name, args and class reach policy.check and the
    approver; audit, traces and hooks carry the inner name; the ToolResult
    keeps the wrapper's id and the assistant message keeps call_tool raw."""
    runs, audit = RunStore(db), AuditStore(db)
    send_args = {"query": "to:bob hello"}

    async def run_one(call: ToolCall) -> tuple[Agent, SpyPolicy, list, Session, list]:
        policy = SpyPolicy()
        asked: list[tuple] = []

        async def approver(tool, args, classification, gate_kind=None):
            asked.append((tool, args, classification))
            return "no"

        policy.approver = approver
        provider = ScriptedProvider([_step(call), LLMResponse(text="ok")])
        hooks: list[tuple] = []
        agent = _agent(
            provider,
            _catalog(),
            policy=policy,
            run_store=runs,
            audit=audit,
            on_tool_call=lambda n, a: hooks.append(("call", n, a)),
            on_tool_result=lambda n, c, e: hooks.append(("result", n, e)),
        )
        session = Session()
        await agent.run_turn(session, "send bob hello")
        return agent, policy, asked, session, hooks

    _, direct_policy, direct_asked, _, _ = await run_one(
        ToolCall(id="d1", name="gmail_send", arguments=send_args)
    )
    agent, policy, asked, session, hooks = await run_one(
        _call("w1", "gmail_send", send_args)
    )

    fail = gotcha(
        "call-tool-unwrap",
        "call_tool must be unwrapped BEFORE hooks/traces/taint/policy/egress/audit — never key anything on 'call_tool'",
    )
    assert policy.checked == direct_policy.checked == [
        ("gmail_send", send_args, ActionClass.OUTWARD)
    ], fail
    assert asked == direct_asked == [("gmail_send", send_args, ActionClass.OUTWARD)]

    result = session.messages[2].tool_results[0]
    assert result.tool_call_id == "w1" and result.is_error is True
    assert "denied" in result.content
    assert session.messages[1].tool_calls[0].name == "call_tool"  # raw round-trip
    assert hooks == [("call", "gmail_send", send_args), ("result", "gmail_send", True)]

    cur = await db.conn.execute(
        "SELECT tool, classification, decision FROM audit_log ORDER BY id"
    )
    rows = [tuple(r) for r in await cur.fetchall()]
    assert rows[-1] == ("gmail_send", "OUTWARD", "denied"), fail
    assert all(r[0] != "call_tool" for r in rows), fail

    events = await runs.events(agent.last_run["run_id"])
    by_kind = {e["kind"]: e["data"] for e in events}
    assert by_kind["tool_call"]["tool"] == "gmail_send"
    assert by_kind["tool_call"]["via"] == "call_tool"
    assert by_kind["policy_decision"]["tool"] == "gmail_send"
    assert by_kind["tool_result"]["tool"] == "gmail_send"


async def test_call_tool_runs_read_only_tool_and_always_ask_still_asks(db):
    """READ_ONLY via call_tool auto-runs; an ALWAYS_ASK tool (allowlisted by
    hand) still reaches the approver — the overlay keys on the inner name."""
    allowlist = AllowlistStore(db)
    await allowlist.add("schedule_add")
    search = FakeMCP("gmail_search")
    schedule = FakeMCP("schedule_add", ActionClass.MUTATING_LOCAL)
    policy = SpyPolicy(allowlist=allowlist, autonomy=Autonomy.INTERACTIVE)
    asked: list[str] = []

    async def approver(tool, args, classification, gate_kind=None):
        asked.append(tool)
        return "yes"

    policy.approver = approver
    provider = ScriptedProvider(
        [
            _step(
                _call("c1", "gmail_search", {"query": "from:bank"}),
                _call("c2", "schedule_add", {"query": "x"}),
            ),
            LLMResponse(text="ok"),
        ]
    )
    session = Session()
    await _agent(provider, [search, schedule], policy=policy).run_turn(session, "hi")

    results = {r.tool_call_id: r for r in session.messages[2].tool_results}
    assert results["c1"].content == "gmail_search ran"
    assert results["c2"].content == "schedule_add ran"
    assert search.runs == [{"query": "from:bank"}]
    assert asked == ["schedule_add"]


async def test_call_tool_schema_invalid_args_not_run_not_gated():
    send = FakeMCP("gmail_send", ActionClass.OUTWARD)
    policy = SpyPolicy()
    provider = ScriptedProvider(
        [
            _step(_call("c1", "gmail_send", {"qeury": "typo", "extra": 1})),
            LLMResponse(text="ok"),
        ]
    )
    session = Session()
    await _agent(provider, [send], policy=policy).run_turn(session, "send it")

    result = session.messages[2].tool_results[0]
    assert result.is_error is True
    assert "don't match its definition" in result.content
    assert "'query' is a required property" in result.content
    assert send.runs == []
    assert policy.checked == []


async def test_call_tool_null_args_mean_not_given():
    """compact_schema strips the nullable wrapper from optional MCP args, and
    local models send `"query": null` for "not given". call_tool drops
    top-level nulls at unwrap, so the call validates and runs — and policy,
    audit and the tool all see the same cleaned arguments."""
    search = FakeMCP(
        "gmail_search",
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    )
    policy = SpyPolicy()
    provider = ScriptedProvider(
        [
            _step(_call("c1", "gmail_search", {"query": "x", "max_results": None})),
            LLMResponse(text="ok"),
        ]
    )
    session = Session()
    await _agent(provider, [search], policy=policy).run_turn(session, "search")

    assert session.messages[2].tool_results[0].is_error is False
    assert search.runs == [{"query": "x"}]
    assert policy.checked == [("gmail_search", {"query": "x"}, ActionClass.READ_ONLY)]
    # A null REQUIRED arg is still missing — bounced for self-correction.
    provider = ScriptedProvider(
        [_step(_call("c2", "gmail_search", {"query": None})), LLMResponse(text="ok")]
    )
    session = Session()
    await _agent(provider, [search], policy=policy).run_turn(session, "search")
    result = session.messages[2].tool_results[0]
    assert result.is_error and "'query' is a required property" in result.content


@pytest.mark.parametrize(
    ("inner", "arguments", "expect"),
    [
        ("call_tool", {"name": "gmail_search", "arguments": {}}, "cannot call call_tool"),
        ("load_tools", {"group": "gmail"}, "cannot call load_tools"),
        ("no_such_tool", {}, "unknown tool 'no_such_tool'"),
        (123, {}, "needs 'name'"),
        ("gmail_search", None, "needs 'arguments'"),
        ("gmail_search", "query=x", "needs 'arguments'"),
    ],
)
async def test_call_tool_guards(inner, arguments, expect):
    search = FakeMCP("gmail_search")
    policy = SpyPolicy()
    provider = ScriptedProvider([_step(_call("c1", inner, arguments)), LLMResponse(text="ok")])
    session = Session()
    await _agent(provider, [search], policy=policy).run_turn(session, "x")

    result = session.messages[2].tool_results[0]
    assert result.is_error is True and expect in result.content
    assert search.runs == [] and policy.checked == []


async def test_meta_tools_unknown_when_grouping_off():
    search = FakeMCP("gmail_search")
    provider = ScriptedProvider(
        [
            _step(
                _call("c1", "gmail_search", {"query": "x"}),
                ToolCall(id="l1", name="load_tools", arguments={"group": "gmail"}),
            ),
            LLMResponse(text="ok"),
        ]
    )
    session = Session()
    await _agent(provider, [search], grouped=False).run_turn(session, "x")
    results = {r.tool_call_id: r for r in session.messages[2].tool_results}
    assert results["c1"].content == "Error: unknown tool 'call_tool'"
    assert results["l1"].content == "Error: unknown tool 'load_tools'"
    assert search.runs == []


async def test_profile_run_turns_grouping_off_and_call_tool_cannot_widen():
    """A closed profile (scheduled/autonomous job): its tools are sent in full,
    the meta-tools are not offered, and call_tool can't reach anything."""
    search, send = FakeMCP("gmail_search"), FakeMCP("gmail_send", ActionClass.OUTWARD)
    provider = ScriptedProvider(
        [_step(_call("c1", "gmail_send", {"query": "x"})), LLMResponse(text="ok")]
    )
    agent = _agent(provider, [EchoTool(), search, send])
    session = Session()
    await agent.run_turn(session, "x", tool_profile=["gmail_search"])

    assert provider.sent_tools[0] == ["gmail_search"]
    result = session.messages[2].tool_results[0]
    assert result.content == "Error: unknown tool 'call_tool'"
    assert send.runs == []


# ---- egress rules through call_tool ------------------------------------------


async def test_call_tool_egress_counts_against_budget():
    provider = ScriptedProvider(
        [
            _step(_call("t1", "web_fetch", {"url": "https://ok.test/1"})),
            _step(_call("t2", "web_fetch", {"url": "https://ok.test/2"})),
            LLMResponse(text="done"),
        ]
    )
    agent = _agent(provider, [FakeFetchTool(), FakeMCP("gmail_search")])
    agent.policy.egress_budget = 1
    session = Session()
    await agent.run_turn(session, "walk https://ok.test")

    assert session.messages[2].tool_results[0].content == "fetched"
    over = session.messages[4].tool_results[0]
    assert over.is_error is True and "egress budget" in over.content
    assert agent._turn_ctx.egress_calls == 1


async def test_call_tool_secret_read_taints_and_gates_egress():
    provider = ScriptedProvider(
        [
            _step(
                _call("t1", "read_file", {"path": "/x/.netrc"}),
                _call("t2", "web_fetch", {"url": "https://ok.test/?d=z"}),
            ),
            LLMResponse(text="done"),
        ]
    )
    agent = _agent(provider, [FakeReadTool(), FakeFetchTool(), FakeMCP("gmail_search")])
    session = Session()
    await agent.run_turn(session, "read netrc then fetch https://ok.test")

    results = {r.tool_call_id: r for r in session.messages[2].tool_results}
    assert agent._turn_ctx.secret_read is True
    assert results["t1"].is_error is False
    assert results["t2"].is_error is True and "secret-class" in results["t2"].content


async def test_call_tool_destination_gate_applies():
    """An unseen host via call_tool(web_fetch) hits the destination gate (no
    approver in tests → denied) exactly like a direct fetch."""
    provider = ScriptedProvider(
        [
            _step(_call("t1", "web_fetch", {"url": "https://evil.test/x"})),
            LLMResponse(text="done"),
        ]
    )
    agent = _agent(provider, [FakeFetchTool(), FakeMCP("gmail_search")])
    session = Session()
    await agent.run_turn(session, "summarize my inbox")
    result = session.messages[2].tool_results[0]
    assert result.is_error is True and "denied" in result.content


# ---- enablement (composition) ------------------------------------------------


@pytest.fixture
def groups_env(monkeypatch):
    # Never inherit the real .env (learnings 2026-09-28). The local server
    # is now REQUIRED (no cloud fallback), so the URL is always pinned.
    for name in (
        "HARNESS_LOCAL_URL", "HARNESS_TOOL_GROUPS", "HARNESS_EFFORT",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HARNESS_LOCAL_URL", "http://127.0.0.1:8080")
    return monkeypatch


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, True),   # default ON: the local lane's prefix cache is the point
        ("off", False),
        ("on", True),
        ("bogus", True),  # a stale value keeps the default with a stderr note
    ],
)
def test_tool_groups_enablement(groups_env, override, expected):
    if override is not None:
        groups_env.setenv("HARNESS_TOOL_GROUPS", override)
    assert composition._tool_groups_enabled() is expected
    agent, _ = composition.build_agent(extra_tools=[FakeMCP("gmail_search")])
    assert agent.toolset.grouped is expected
    sent = {t.name for t in agent.toolset.tools()}
    assert ("gmail_search" in sent) is not expected
    assert ("call_tool" in sent) is expected
    # In-process: GROUP_TOOLS members (browser_delegate) defer with the rest;
    # everyday tools are always sent.
    assert ("browser_delegate" in sent) is not expected
    assert {"web_fetch", "current_time", "convert_time"} <= sent


async def test_real_catalog_split_keeps_everyday_tools_always_on(groups_env, db):
    """Against the REAL in-process catalog: every GROUP_TOOLS name really
    exists (a rename would silently un-group it), none of them is sent up
    front, and the tools the system prompt tells the model to use unprompted
    (current_time, convert_time, recall) plus the everyday ones stay always-on."""
    from harness.memory import MemoryStore
    from harness.store.approvals import ApprovalStore
    from harness.store.client_config import ClientConfigStore
    from harness.store.triggers import TriggerStore

    groups_env.setenv("HARNESS_TOOL_GROUPS", "on")
    agent, _ = composition.build_agent(
        run_store=RunStore(db),
        triggers=TriggerStore(db),
        memory=MemoryStore(db),
        client_config=ClientConfigStore(db),
        approvals=ApprovalStore(db),
        extra_tools=[FakeMCP("gmail_search")],
    )
    catalog = {t.name for t in agent.toolset.catalog()}
    sent = {t.name for t in agent.toolset.tools()}
    # maps_* register only with a Maps key, so they may be absent here.
    required = set(tool_groups.GROUP_TOOLS) - {"maps_search", "maps_route"}
    assert required <= catalog, (
        sorted(required - catalog),
        "every GROUP_TOOLS entry must name a REGISTERED tool — a rename silently un-groups it",
    )
    assert not sent & set(tool_groups.GROUP_TOOLS)
    always_on = {"current_time", "convert_time", "recall", "web_fetch", "weather"}
    # web_search registers only with a Brave key; when it does, it's sent.
    if "web_search" in catalog:
        always_on.add("web_search")
    assert always_on <= sent, sorted(always_on - sent)
    # Exactly the always-on set plus the two meta-tools (notify is unwired
    # here: it registers only with push) — anything else rides every prompt.
    assert sent == always_on | {"load_tools", "call_tool"}, sorted(sent - always_on)
    groups = agent.toolset.groups()
    # The occasional file/sandbox tools are deferred, and their group heads
    # the load_tools catalog (registration order).
    files = ["read_file", "list_files", "search_files", "edit_file", "copy_file", "sandbox_run", "read_document"]
    assert [t.name for t in groups["files"]] == files
    assert list(groups)[0] == "files"
    load = next(t for t in agent.toolset.tools() if t.name == "load_tools")
    assert load.input_schema["properties"]["group"]["enum"][0] == "files"
    assert "\nGroups:\n- files: " in load.description
    assert [t.name for t in groups["browser"]] == ["browser_fetch", "browser_delegate"]
    assert [t.name for t in groups["memory_admin"]] == ["remember", "forget"]
