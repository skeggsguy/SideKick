"""ms365 MCP server scaffold: offline surface checks only.

No Graph or MSAL network calls here — constructing the MSAL app performs
live authority discovery, so auth/token paths are exercised manually via
`uv run ms365-mcp --auth` (docs/ms365-setup.md), never in tests.
"""

from __future__ import annotations

import pytest

from mcp_servers.ms365 import auth, server


async def test_server_registers_outlook_tools():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "outlook_mail_search",
        "outlook_mail_read",
        "outlook_calendar_list_events",
    }
    # FastMCP derives descriptions from docstrings — they must exist for the
    # model to pick the right tool.
    assert all(t.description for t in tools)


def test_missing_client_id_explains_setup(monkeypatch):
    monkeypatch.delenv("MS365_CLIENT_ID", raising=False)
    with pytest.raises(RuntimeError, match="ms365-setup"):
        auth.client_id()
