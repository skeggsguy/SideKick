"""Minimal FastMCP server used by tests to validate the harness MCP client.

Four tools: echo (succeeds), fail (always errors), note (optional args incl. an
argument literally named `title`, for schema compaction), and rows (returns a
list — FastMCP sends one TextContent per element, the shape that 502'd every
live Tasks approval preview). Run over stdio.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo-fixture")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo: {text}"


@mcp.tool()
def fail(text: str) -> str:
    """Always raises, to exercise error propagation."""
    raise RuntimeError(f"deliberate failure: {text}")


@mcp.tool()
def note(title: str, tags: list[str] | None = None, body: str | None = None, limit: int = 10) -> str:
    """Echo a note back, showing which optional args arrived."""
    return f"note: {title} tags={tags} body={body} limit={limit}"


@mcp.tool()
def rows() -> list[dict]:
    """Return a list — the shape FastMCP splits into one TextContent per item."""
    return [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]


if __name__ == "__main__":
    mcp.run(transport="stdio")
