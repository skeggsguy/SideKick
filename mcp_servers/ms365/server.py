"""The ms365 MCP server: read-only Outlook mail + calendar over Microsoft
Graph, for a personal Microsoft account (optional connector).

Same shape as the google_workspace server: plain sync tool functions
(FastMCP derives schemas from hints and docstrings), stdio transport,
one-time interactive auth via `uv run ms365-mcp --auth`.
"""

from __future__ import annotations

import sys

from mcp.server.fastmcp import FastMCP

from . import calendar, mail
from .auth import run_auth_flow

mcp = FastMCP("ms365")

for fn in (
    mail.outlook_mail_search,
    mail.outlook_mail_read,
    calendar.outlook_calendar_list_events,
):
    mcp.tool()(fn)


def main() -> None:
    if "--auth" in sys.argv:
        run_auth_flow()
        return
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
