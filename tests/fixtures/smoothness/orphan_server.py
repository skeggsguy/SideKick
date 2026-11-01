"""BK-032 probe server: the REAL gateway app (create_app over a scratch DB) on an
ephemeral 127.0.0.1 port — never agentd's 8787 — with three probe-only GETs
mounted in FRONT of it (the app itself is unchanged):

  /probe/subs     {"subs": broadcaster.subscriber_count} — the server-side count
                  of live /api/events streams (one page under test, so this IS
                  the page's stream count; PageStreams' `[events]` lines go to
                  this process's stderr exactly as in agentd.log)
  /probe/drop     cut every live /api/events response NOW (the sleep's dead
                  sockets): the ASGI call is cancelled, uvicorn closes the
                  connection, the client's EventSource errors and auto-reconnects
  /probe/publish  publish one harmless background event {"type":"probe_ping",
                  "mark":<m>} — no session, so events.js routes it nowhere and
                  the probe counts deliveries in its EventSource wrapper

Run under `uv run python` (orphan.py spawns it via common.spawn_server).
Prints `PORT <n>` once listening. PROBE_WEBUI=<dir> serves that web UI copy (the red-before run)."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus
import harness.gateway.app as gateway_app
from harness.gateway.app import Gateway, create_app
from harness.gateway.broadcast import RunBroadcaster
from harness.store.approvals import ApprovalStore
from harness.store.db import Database
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore


class DropEvents:
    """ASGI wrapper: every /api/events call runs as a task that /probe/drop
    cancels (a dead socket from the client's side: the stream just ends)."""

    def __init__(self, app) -> None:
        self.app = app
        self.live: set[asyncio.Task] = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != "/api/events":
            return await self.app(scope, receive, send)
        task = asyncio.ensure_future(self.app(scope, receive, send))
        self.live.add(task)
        try:
            await task
        except asyncio.CancelledError:
            # Ours (a drop) ends the response with no terminating chunk —
            # uvicorn then closes the connection, which is the point.
            if asyncio.current_task().cancelling():
                raise
            raise RuntimeError("probe drop: stream cut") from None
        finally:
            self.live.discard(task)

    def drop(self) -> int:
        n = len(self.live)
        for t in list(self.live):
            t.cancel()
        return n


async def main() -> None:
    # A fixed scratch dir, wiped at start: SIGTERM ends uvicorn by re-raising the signal, so no
    # exit hook runs, and a per-run mkdtemp would pile up one dir per run.
    tmp = Path(__file__).resolve().parents[3] / "scratch" / "smoothness_fixture" / "db-bk032"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    db = await Database.open(tmp / "probe.db")
    broadcaster = RunBroadcaster()
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy, **kw: None,  # no runs in this probe
        broadcaster=broadcaster,
    )
    if os.environ.get("PROBE_WEBUI"):
        gateway_app.WEBUI_DIR = Path(os.environ["PROBE_WEBUI"])
    inner = DropEvents(create_app(gw))
    outer = FastAPI()

    @outer.get("/probe/subs")
    async def subs():
        return {"subs": broadcaster.subscriber_count}

    @outer.get("/probe/drop")
    async def drop():
        return {"dropped": inner.drop()}

    @outer.get("/probe/publish")
    async def publish(mark: str):
        broadcaster.publish({"type": "probe_ping", "mark": mark})
        return {"subs": broadcaster.subscriber_count}

    outer.mount("/", inner)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    assert port != 8787
    print(f"PORT {port}", flush=True)
    server = uvicorn.Server(uvicorn.Config(outer, log_level="critical"))
    await server.serve(sockets=[sock])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
