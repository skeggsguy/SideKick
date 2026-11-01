"""Network reachability pre-flight.

A remote (MCP-backed) tool that runs while the network is down doesn't fail
cleanly — the call's cancel scope can unwind as a CancelledError and, mid-turn,
leave a half-written tool step behind. So before running a remote tool the agent
asks here whether we're online and, if not, refuses the call up front with a
clear message instead of attempting it.

Deliberately simple: a single short TCP probe to a well-known host. "Is the
internet reachable at all", not "is this particular backend up" — per-server
probing can come later if it earns its keep.
"""

from __future__ import annotations

import asyncio

# Cloudflare's resolver on the TLS port — anycast, near-universally reachable,
# and a TCP connect needs no DNS of its own (the host is already an IP).
PROBE_HOST = "1.1.1.1"
PROBE_PORT = 443
DEFAULT_TIMEOUT_S = 2.0


async def is_online(
    timeout_s: float = DEFAULT_TIMEOUT_S,
    host: str = PROBE_HOST,
    port: int = PROBE_PORT,
) -> bool:
    """Return True if a TCP connection to ``host:port`` opens within
    ``timeout_s``. Any failure (timeout, refused, DNS, no route) means offline."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout_s
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True
