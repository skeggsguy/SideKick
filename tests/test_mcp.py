"""MCP plumbing, end-to-end against a real FastMCP fixture server over stdio."""

from __future__ import annotations

import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from harness.core.policy import ActionClass
from harness.mcp.manager import (
    MCPManager,
    ServerConfig,
    compact_schema,
    load_server_configs,
)

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"


@asynccontextmanager
async def echo_manager():
    # Not a pytest fixture: the MCP stdio transport's cancel scopes must be
    # entered and exited in the same task, and pytest-asyncio runs fixture
    # setup/teardown in different tasks than the test body.
    cfg = ServerConfig(
        name="echo_fixture",
        command=sys.executable,
        args=[str(FIXTURE_SERVER)],
        classifications={"echo": ActionClass.READ_ONLY},
    )
    mgr = MCPManager([cfg])
    try:
        await mgr.start()
        yield mgr
    finally:
        await mgr.close()


async def test_tools_listed_and_classified():
    async with echo_manager() as mgr:
        by_name = {t.name: t for t in mgr.tools}
        assert set(by_name) == {"echo", "fail", "note", "rows"}
        assert by_name["echo"].classification is ActionClass.READ_ONLY
        # Unlisted tools must default to OUTWARD — gated, never a free pass.
        assert by_name["fail"].classification is ActionClass.OUTWARD
        assert by_name["echo"].input_schema["type"] == "object"


async def test_call_remote_tool():
    async with echo_manager() as mgr:
        echo = next(t for t in mgr.tools if t.name == "echo")
        assert await echo.run({"text": "hello"}) == "echo: hello"


async def test_list_tool_arrives_as_concatenated_docs_and_previews_decode_it():
    """The REAL FastMCP wire shape for a list-returning tool: one TextContent
    per element, newline-joined by the client — NOT a JSON array. This is what
    502'd every live Tasks approval preview: the preview's stub used to serve a
    tidy array and hid it. Feeding the fixture's own output through the
    task_complete resolver pins the shape against a FastMCP serialization
    change, not just our decoder."""
    from harness.gateway.preview import ApprovalPreviews

    async with echo_manager() as mgr:
        rows = next(t for t in mgr.tools if t.name == "rows")
        text = await rows.run({})
        assert text == (
            '{\n  "id": "1",\n  "title": "a"\n}\n'
            '{\n  "id": "2",\n  "title": "b"\n}'
        )
        with pytest.raises(ValueError):
            json.loads(text)
        previews = ApprovalPreviews(tool_handles={"tasks_list": rows})
        out = await previews.resolve("task_complete", {"task_id": "2"})
        assert out == {"title": "b", "list": "", "due": "", "status": ""}, (
            "list-returning tools arrive as one TextContent per element ({...}\\n{...}), not a JSON array — "
            "preview.py's _load_concatenated must decode it"
        )


async def test_remote_error_surfaces_as_text():
    async with echo_manager() as mgr:
        fail = next(t for t in mgr.tools if t.name == "fail")
        result = await fail.run({"text": "boom"})
        assert result.startswith("Error from echo_fixture.fail")
        assert "deliberate failure: boom" in result


async def test_adapted_schema_is_compacted_and_optional_args_still_work():
    """The adapter sends the compacted schema, and the server (which still
    validates against its own pydantic model) accepts calls shaped by it:
    optional args omitted, or supplied as plain values."""
    async with echo_manager() as mgr:
        note = next(t for t in mgr.tools if t.name == "note")
        assert note.input_schema == {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "body": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["title"],
        }
        assert await note.run({"title": "t"}) == (
            "note: t tags=None body=None limit=10"
        )
        assert await note.run(
            {"title": "t", "tags": ["a"], "body": "b", "limit": 3}
        ) == "note: t tags=['a'] body=b limit=3"


async def test_start_injects_file_roots_env(tmp_path, monkeypatch):
    """Spawned servers get the write-jail roots so their downloads obey it."""
    from harness.mcp import manager as manager_mod

    captured: list = []

    class _FakeConn:
        def __init__(self, name, params):
            captured.append(params)

        async def start(self, stack):
            pass

        async def list_tools(self):
            return []

    monkeypatch.setenv("HARNESS_FILE_ROOTS", str(tmp_path))
    monkeypatch.setattr(manager_mod, "MCPConnection", _FakeConn)
    mgr = MCPManager(
        [
            ServerConfig(name="plain", command="x", args=[]),
            ServerConfig(
                name="override",
                command="x",
                args=[],
                env={"HARNESS_FILE_ROOTS": "custom"},
            ),
        ]
    )
    await mgr.start()
    assert captured[0].env["HARNESS_FILE_ROOTS"] == str(tmp_path.resolve())
    # Explicit config env still wins over the injected default.
    assert captured[1].env["HARNESS_FILE_ROOTS"] == "custom"
    await mgr.close()


def test_load_server_configs(tmp_path):
    cfg_file = tmp_path / "servers.toml"
    cfg_file.write_text(
        """
[servers.example]
command = "python"
args = ["-m", "example"]
read_only = ["a_read"]
outward = ["a_send"]
env = { FOO = "bar" }
"""
    )
    (cfg,) = load_server_configs(cfg_file)
    assert cfg.name == "example"
    assert cfg.classify("a_read") is ActionClass.READ_ONLY
    assert cfg.classify("a_send") is ActionClass.OUTWARD
    assert cfg.classify("anything_else") is ActionClass.OUTWARD
    assert cfg.env == {"FOO": "bar"}


def test_load_server_configs_rejects_double_listing(tmp_path):
    cfg_file = tmp_path / "servers.toml"
    cfg_file.write_text(
        """
[servers.example]
command = "python"
read_only = ["dup"]
outward = ["dup"]
"""
    )
    with pytest.raises(ValueError, match="more than one classification"):
        load_server_configs(cfg_file)


def test_missing_config_is_empty(tmp_path):
    assert load_server_configs(tmp_path / "nope.toml") == []


def test_real_servers_toml_classifies_workspace_writes():
    """The shipped servers.toml loads cleanly (no double-classification) and
    puts every write tool in the right risk class."""
    from harness.mcp.manager import DEFAULT_CONFIG

    configs = load_server_configs(DEFAULT_CONFIG)
    gw = next(c for c in configs if c.name == "google_workspace")
    for local in (
        "gmail_draft_create",
        "gmail_draft_reply",
        "calendar_event_create",
        "calendar_event_update",
        "task_create",
        "task_complete",
        "docs_create",
        "docs_edit_text",
        # Downloads write to the local file area — MUTATING_LOCAL like
        # drive_download, never READ_ONLY (or supervised/restricted runs
        # could write files ungated).
        "gmail_download_attachment",
        "drive_download",
    ):
        assert gw.classify(local) is ActionClass.MUTATING_LOCAL, local
    assert gw.classify("gmail_send") is ActionClass.OUTWARD
    assert gw.classify("gmail_send_draft") is ActionClass.OUTWARD
    # Guest management + RSVP notify other people — outward like send.
    assert gw.classify("calendar_event_attendees") is ActionClass.OUTWARD
    assert gw.classify("calendar_event_respond") is ActionClass.OUTWARD
    assert gw.classify("calendar_event_delete") is ActionClass.DESTRUCTIVE
    assert gw.classify("gmail_search") is ActionClass.READ_ONLY
    # Approval-preview reads must stay READ_ONLY: the gateway dispatches them
    # outside a run to render real content in approval modals (a mutating
    # dispatch there would be a gate bypass).
    assert gw.classify("gmail_get_draft") is ActionClass.READ_ONLY
    assert gw.classify("drive_file_info") is ActionClass.READ_ONLY
    # Trash tools are the first (deliberate) DESTRUCTIVE class members:
    # always prompt, denied unattended, never allowlistable.
    assert gw.classify("gmail_trash_message") is ActionClass.DESTRUCTIVE
    assert gw.classify("drive_trash_file") is ActionClass.DESTRUCTIVE


# --- compact_schema: token filler out, meaning kept --------------------------


def test_compact_schema_strips_filler_keeps_meaning():
    pydantic_shaped = {
        "title": "docs_createArguments",
        "type": "object",
        "properties": {
            # An ARGUMENT named title must survive — only the labels go.
            "title": {"title": "Title", "type": "string", "description": "Doc name"},
            "folder": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
                "title": "Folder",
                "description": "Parent folder id",
            },
            "tags": {
                "anyOf": [
                    {"type": "array", "items": {"title": "Tag", "type": "string"}},
                    {"type": "null"},
                ],
                "default": None,
                "title": "Tags",
            },
            "sheet": {"default": "Sheet1", "title": "Sheet", "type": "string"},
            "mode": {"enum": ["a", "b"], "title": "Mode", "type": "string"},
        },
        "required": ["title"],
    }
    assert compact_schema(pydantic_shaped) == {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Doc name"},
            "folder": {"type": "string", "description": "Parent folder id"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "sheet": {"default": "Sheet1", "type": "string"},
            "mode": {"enum": ["a", "b"], "type": "string"},
        },
        "required": ["title"],
    }
    # Input untouched (MCP SDK objects may be shared).
    assert pydantic_shaped["properties"]["folder"]["default"] is None


def test_compact_schema_leaves_real_unions_alone():
    """Only the exact two-way optional wrapper collapses; a real union (or a
    three-way one with null) carries meaning and is kept."""
    union = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
    three = {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}
    assert compact_schema(union) == union
    assert compact_schema(three) == three


def _sample(schema: dict, full: bool) -> dict:
    """An argument dict built from a schema: required args only, or all."""
    def value(s: dict):
        s = next((v for v in s.get("anyOf", []) if v != {"type": "null"}), s)
        if "enum" in s:
            return s["enum"][0]
        if "type" not in s:  # Any (e.g. sheet cell values)
            return "x"
        if "type" not in s:  # Any (e.g. sheet cell values)
            return "x"
        if s["type"] == "array":
            item = value(s.get("items", {"type": "string"}))
            return [item] * max(1, s.get("minItems", 1))
        return {
            "string": "x",
            "integer": max(1, s.get("minimum", 1)),
            "number": 1.5,
            "boolean": True,
        }[s["type"]]

    names = schema.get("properties", {}) if full else schema.get("required", [])
    return {n: value(schema["properties"][n]) for n in names}


async def test_real_workspace_schemas_compact_equivalently():
    """Against every real Google Workspace tool: the same argument names,
    required list and descriptions reach the model, no filler remains, and
    any call the model can shape from the compacted schema validates against
    the ORIGINAL one (and vice versa), so nothing the server accepts is lost."""
    import json

    from jsonschema import Draft202012Validator as V

    from mcp_servers.google_workspace.server import mcp as gw_mcp

    tools = await gw_mcp.list_tools()
    assert tools
    for t in tools:
        orig, comp = t.inputSchema, compact_schema(t.inputSchema)
        assert set(comp["properties"]) == set(orig["properties"]), t.name
        assert comp.get("required") == orig.get("required"), t.name
        for name, prop in orig["properties"].items():
            assert comp["properties"][name].get("description") == prop.get("description")
        dumped = json.dumps(comp)
        assert '"title": "' not in dumped.replace('"title": {', ""), t.name
        assert '{"type": "null"}' not in dumped and '"default": null' not in dumped
        for full in (False, True):
            args = _sample(orig, full)
            assert V(orig).is_valid(args) and V(comp).is_valid(args), (t.name, args)
        # A wrong-typed required arg is still rejected by the compacted schema.
        for name in orig.get("required", []):
            bad = {**_sample(orig, False), name: {"not": "valid"}}
            assert not V(comp).is_valid(bad), (t.name, name)
