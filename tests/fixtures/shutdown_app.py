"""agentd's shutdown path in miniature, for tests/test_agentd.py's real-signal
tests: the same _Server + install_shutdown_signals, an endless SSE route like
/api/events, and an aiosqlite Database whose close must run (an unclosed one
hangs interpreter exit on its non-daemon thread).

Usage: python shutdown_app.py <port> <db_path> <graceful_timeout_s>
Prints READY once serving, CLOSED after the DB close."""

from __future__ import annotations

import asyncio
import sys

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from harness.gateway.agentd import build_server, install_shutdown_signals
from harness.store import Database

app = FastAPI()


@app.get("/events")
async def events():
    async def stream():
        yield ": connected\n\n"
        while True:
            await asyncio.sleep(3600)
            yield ": ping\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


async def main(port: int, db_path: str, graceful: float) -> None:
    db = await Database.open(db_path)
    try:
        server = build_server(app, "127.0.0.1", port, graceful_timeout=graceful)
        install_shutdown_signals(server)
        serving = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        print("READY", flush=True)
        await serving
        # An await after serve(), like amain's MCP close: the bug's stray
        # cancel landed exactly here and skipped the DB close.
        await asyncio.sleep(0.05)
    finally:
        await db.close()
        print("CLOSED", flush=True)


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]), sys.argv[2], float(sys.argv[3])))
