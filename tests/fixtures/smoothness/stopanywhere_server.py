"""BK-034 probe server: the REAL gateway app (create_app over a scratch DB) with
a slow fake agent, on an ephemeral 127.0.0.1 port — never agentd's 8787.

Run under `uv run python` (needs the harness package); stopanywhere.py and
senderstop.py spawn it (common.spawn_server). Prints `PORT <n>` on stdout once
listening. The agent mimics the real Agent's hook contract (StreamedAgent / CancelPersistAgent in
tests/test_chat_recovery.py): on_run_start(run id), add_user, a text delta
every TICK_S for TICKS ticks, on cancel the partial is persisted (shielded)
and CancelledError re-raised — agent.py's _persist_partial_on_cancel shape.

The text "fast" anywhere in the user message makes a short run (FAST_TICKS),
for the finish-while-observed variant. BK-036 (step 2c): "tool" makes a run
shaped like the property chat's browser_delegate stretch — TOOL_LEAD ticks of
text, a tool_call, TOOL_S seconds of SILENCE (only the chat stream's pings,
CHAT_HEARTBEAT_S shortened to 1 s here so a cut is noticed quickly), the
tool_result, then TOOL_TAIL ticks and the end. PROBE_WEBUI=<dir> serves that web UI
copy instead of src/harness/gateway/webui (the red-before run)."""
from __future__ import annotations

import asyncio
import itertools
import os
import shutil
import socket
import sys
from pathlib import Path

import uvicorn

from harness.core.approvals import ApprovalWaiter
from harness.core.events import EventBus
import harness.gateway.app as gateway_app
import harness.gateway.routes.chat_run as chat_run
from harness.gateway.app import Gateway, create_app
from harness.gateway.broadcast import RunBroadcaster
from harness.store.approvals import ApprovalStore
from harness.store.db import Database
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore

TICK_S = 0.1
TICKS = 300  # ~30 s: long enough for B to join, switch away and back, and stop
FAST_TICKS = 40  # ~4 s
TOOL_LEAD, TOOL_S, TOOL_TAIL = 15, 14.0, 30  # ~1.5 s text, 14 s silent tool, ~3 s text
_ids = itertools.count(1)


class SlowAgent:
    def __init__(self) -> None:
        self.on_text_delta = lambda t: None
        self.on_tool_call = lambda n, a: None
        self.on_tool_result = lambda n, c, e: None
        self.on_thinking_delta = lambda t: None
        self.on_thinking_done = lambda: None
        self.on_truncated = lambda: None
        self.on_compaction = lambda phase, info=None: None
        self.on_run_start = None
        self.on_model_call = None
        self.on_answer_complete = None
        self.last_run: dict | None = None

    def request_stop(self) -> bool:
        return True

    async def run_turn(self, session, user_input, **kwargs):
        if self.on_run_start is not None:
            self.on_run_start(f"probe-run-{next(_ids)}")
        await session.add_user(user_input)
        if "tool" in str(user_input):
            return await self._tool_turn(session)
        ticks = FAST_TICKS if "fast" in str(user_input) else TICKS
        said: list[str] = []
        try:
            for i in range(ticks):
                chunk = f"w{i} "
                said.append(chunk)
                self.on_text_delta(chunk)
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            await asyncio.shield(session.add_assistant("".join(said)))
            raise
        said.append("END.")
        self.on_text_delta("END.")
        if self.on_answer_complete is not None:
            self.on_answer_complete()
        text = "".join(said)
        await session.add_assistant(text)
        return text


    async def _tool_turn(self, session):
        said: list[str] = []

        async def words(lo: int, hi: int) -> None:
            for i in range(lo, hi):
                said.append(f"w{i} ")
                self.on_text_delta(f"w{i} ")
                await asyncio.sleep(TICK_S)

        try:
            await words(0, TOOL_LEAD)
            self.on_tool_call("browser_delegate", {"origin": "example.org", "task": "probe"})
            await asyncio.sleep(TOOL_S)  # the long silent tool: no events, pings only
            self.on_tool_result("browser_delegate", "report: ok", False)
            await words(TOOL_LEAD, TOOL_LEAD + TOOL_TAIL)
        except asyncio.CancelledError:
            await asyncio.shield(session.add_assistant("".join(said)))
            raise
        said.append("END.")
        self.on_text_delta("END.")
        if self.on_answer_complete is not None:
            self.on_answer_complete()
        await session.add_assistant("".join(said))
        return "".join(said)


async def main() -> None:
    # A fixed scratch dir, wiped at start: SIGTERM ends uvicorn by re-raising the signal, so no
    # exit hook runs, and a per-run mkdtemp would pile up one dir per run.
    tmp = Path(__file__).resolve().parents[3] / "scratch" / "smoothness_fixture" / "db-bk034"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    db = await Database.open(tmp / "probe.db")
    gw = Gateway(
        sessions=SessionStore(db),
        runs=RunStore(db),
        approval_store=ApprovalStore(db),
        waiter=ApprovalWaiter(ApprovalStore(db)),
        triggers=TriggerStore(db),
        bus=EventBus(),
        push=None,
        agent_factory=lambda autonomy, **kw: SlowAgent(),
        broadcaster=RunBroadcaster(),
    )
    # PROBE_WEBUI: serve another copy of the web UI (the pre-change tree, for
    # the red-before run); create_app reads the module global at call time.
    # A 1 s chat heartbeat (agentd: 15 s) — read per loop as a module global.
    chat_run.CHAT_HEARTBEAT_S = 1.0
    if os.environ.get("PROBE_WEBUI"):
        gateway_app.WEBUI_DIR = Path(os.environ["PROBE_WEBUI"])
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    assert port != 8787
    print(f"PORT {port}", flush=True)
    server = uvicorn.Server(uvicorn.Config(create_app(gw), log_level="warning"))
    await server.serve(sockets=[sock])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
