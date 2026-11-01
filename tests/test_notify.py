"""Test notify tool."""

import pytest
from harness.tools.notify import NotifyTool
from harness.core.policy import ActionClass
from harness.composition import build_agent


@pytest.fixture
def send_recorder():
    """Mock send function that records calls."""
    calls = []

    async def mock_send(title: str, body: str, url: str) -> int:
        calls.append((title, body, url))
        return 1  # Pretend 1 device subscribed

    mock_send.calls = calls
    return mock_send


def test_notify_classification():
    """notify is MUTATING_LOCAL (allowlistable)."""
    tool = NotifyTool(lambda t, b, u: 0)
    assert tool.classification == ActionClass.MUTATING_LOCAL


def test_notify_remote_false():
    """notify is not a remote egress tool."""
    tool = NotifyTool(lambda t, b, u: 0)
    assert tool.remote is False


@pytest.mark.asyncio
async def test_notify_sends_message(send_recorder):
    """notify calls send with title, body, and hardcoded url."""
    tool = NotifyTool(send_recorder)
    result = await tool.run({"title": "Hello", "body": "World"})

    assert result == "notified 1 device(s)."
    assert len(send_recorder.calls) == 1
    title, body, url = send_recorder.calls[0]
    assert title == "Hello"
    assert body == "World"
    assert url == "/"  # Always /


@pytest.mark.asyncio
async def test_notify_url_never_settable(send_recorder):
    """url parameter is ignored even if provided."""
    tool = NotifyTool(send_recorder)
    result = await tool.run(
        {"title": "T", "body": "B", "url": "/custom"}
    )

    assert len(send_recorder.calls) == 1
    _, _, url = send_recorder.calls[0]
    assert url == "/"  # Always /, never /custom


@pytest.mark.asyncio
async def test_notify_missing_title(send_recorder):
    """Missing title returns error."""
    tool = NotifyTool(send_recorder)
    result = await tool.run({"body": "Body"})

    assert "error" in result.lower()
    assert "title" in result.lower()
    assert len(send_recorder.calls) == 0


@pytest.mark.asyncio
async def test_notify_empty_title(send_recorder):
    """Empty title returns error."""
    tool = NotifyTool(send_recorder)
    result = await tool.run({"title": "   ", "body": "Body"})

    assert "error" in result.lower()
    assert len(send_recorder.calls) == 0


@pytest.mark.asyncio
async def test_notify_title_length_cap(send_recorder):
    """Title over 80 chars returns error."""
    tool = NotifyTool(send_recorder)
    long_title = "x" * 81
    result = await tool.run({"title": long_title, "body": "Body"})

    assert "error" in result.lower()
    assert "80" in result
    assert len(send_recorder.calls) == 0


@pytest.mark.asyncio
async def test_notify_missing_body(send_recorder):
    """Missing body returns error."""
    tool = NotifyTool(send_recorder)
    result = await tool.run({"title": "Title"})

    assert "error" in result.lower()
    assert "body" in result.lower()
    assert len(send_recorder.calls) == 0


@pytest.mark.asyncio
async def test_notify_empty_body(send_recorder):
    """Empty body returns error."""
    tool = NotifyTool(send_recorder)
    result = await tool.run({"title": "Title", "body": "   "})

    assert "error" in result.lower()
    assert len(send_recorder.calls) == 0


@pytest.mark.asyncio
async def test_notify_per_run_cap(send_recorder):
    """Per-instance cap: 5 sends max, 6th fails."""
    tool = NotifyTool(send_recorder)

    # First 5 should succeed
    for i in range(5):
        result = await tool.run({"title": f"T{i}", "body": "B"})
        assert "notified" in result.lower()

    assert len(send_recorder.calls) == 5

    # 6th should hit cap
    result = await tool.run({"title": "T5", "body": "B"})
    assert "cap reached" in result.lower()
    assert len(send_recorder.calls) == 5


@pytest.mark.asyncio
async def test_notify_no_devices(send_recorder):
    """When send returns 0 devices."""

    async def send_no_devices(t, b, u):
        return 0

    tool = NotifyTool(send_no_devices)
    result = await tool.run({"title": "T", "body": "B"})

    assert "no devices" in result.lower()


def test_notify_absent_from_cli_composition():
    """NotifyTool not registered when extra_tools is empty (CLI shape)."""
    from harness.core.registry import ToolRegistry
    from harness.composition import _register_tools

    registry = ToolRegistry()
    _register_tools(
        registry,
        client_config=None,
        triggers=None,
        run_store=None,
        approvals=None,
        memory=None,
        extra_tools=None,
    )
    tool_names = {t.name for t in registry.all()}
    assert "notify" not in tool_names


def test_notify_description():
    """Description tells the model notify is the only alert path for
    scheduled runs (no automatic completion push)."""
    tool = NotifyTool(lambda t, b, u: 0)
    assert "no automatic completion push" in tool.description.lower()
    assert "only way to alert" in tool.description.lower()
