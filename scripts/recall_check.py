"""Manual live check: does the main agent call `recall` before going to the web?

Drives ONE real turn — "Which supermarket near me has the cheapest
sparkling water 12-pack?" — through Agent.run_turn on the LOCAL
lane and passes if the model calls `recall` before its first `web_search`
(plan prompt-trim, step 3).

Everything the model sees is built by the REAL harness code: build_agent()
over the real MCP tool list (listing tools spawns the MCP server, no Google
API call) with throwaway stores on a temp DB — never harness.db. The live
DB's ACTIVE facts are copied in READ-ONLY (sqlite ``mode=ro``; ids,
categories and dates kept) and embedded by the real OllamaEmbedder, so the
first-turn ``<user_profile>``/``<memory>`` injection and the recall tool both
see the real memory with KNN. recall, current_time and convert_time EXECUTE
for real (local, read-only); the turn STOPS at the first call of anything
else (web_search, web_fetch, load_tools, call_tool, …) without executing it,
so nothing reaches Google or the web. The post-turn memory extractor is off.

The :8080 local server is SHARED with the live chat (-np 1, one cached
prefix): running this evicts the chat's prefix. Manual only — run it once,
never in loops. Needs HARNESS_LOCAL_URL (.env) and Ollama for real KNN
(without it recall degrades to keyword search; the header says which).

Run with: uv run python scripts/recall_check.py [--effort low]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from harness.composition import build_agent, open_memory
from harness.core.agent import Agent
from harness.core.session import Session
from harness.core.types import ToolCall, ToolResult
from harness.mcp import MCPManager
from harness.memory import MemoryStore
from harness.store import Database
from harness.store.approvals import ApprovalStore
from harness.store.audit import AllowlistStore, AuditStore
from harness.store.client_config import ClientConfigStore
from harness.store.db import default_db_path
from harness.store.runs import RunStore
from harness.store.sessions import SessionStore
from harness.store.triggers import TriggerStore
from harness.tools.notify import NotifyTool

PROMPT = "Which supermarket near me has the cheapest sparkling water 12-pack?"
#: Local, read-only tools the check lets run for real; anything else stops it.
EXECUTE = {"recall", "current_time", "convert_time"}
MAX_STEPS = 8
_FACT_COLUMNS = (
    "id, content, category, confidence, source_run_id, active, "
    "created_at, updated_at, last_recalled_at"
)


class StopAtCall(Exception):
    """The model asked for a tool the check never executes."""

    def __init__(self, call: ToolCall) -> None:
        super().__init__(call.name)
        self.call = call


async def _no_push(title: str, body: str, url: str) -> int:
    raise RuntimeError("recall_check never sends pushes")


def _live_facts(db_path: Path) -> list[sqlite3.Row]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            f"SELECT {_FACT_COLUMNS} FROM facts WHERE active = 1 ORDER BY id"
        ).fetchall()
    finally:
        con.close()


async def _seed_memory(db: Database, memory: MemoryStore, rows: list[sqlite3.Row]) -> int:
    """Copy the live facts (same ids, so the trace matches the live DB), then
    embed them all through the store's own backfill path."""
    marks = ",".join("?" * len(_FACT_COLUMNS.split(",")))
    await db.conn.executemany(
        f"INSERT INTO facts ({_FACT_COLUMNS}) VALUES ({marks})",
        [tuple(r) for r in rows],
    )
    await db.conn.commit()
    return await memory.backfill_vectors()


def _wire(agent: Agent, trace: list[str]) -> None:
    """Record every model step and tool call; run only EXECUTE tools."""
    real_chat = agent.provider.chat
    real_dispatch = agent._dispatcher.dispatch

    async def chat(*args, **kwargs):
        resp = await real_chat(*args, **kwargs)
        u = resp.usage
        usage = f" [in={u.input_tokens} out={u.output_tokens}]" if u else ""
        calls = ", ".join(tc.name for tc in resp.tool_calls) or "no tool calls"
        text = f" text={(resp.text or '')[:160]!r}" if resp.text else ""
        trace.append(f"model step{usage}: {calls}{text}")
        # The dispatcher stops at the FIRST non-allowed call, so log every
        # parallel call the model requested here, args included.
        for tc in resp.tool_calls:
            args = json.dumps(tc.arguments, ensure_ascii=False)
            trace.append(f"  requested: {tc.name} {args}")
        return resp

    async def dispatch(tc: ToolCall, run_id: str | None) -> ToolResult:
        trace.append(f"tool call: {tc.name} {json.dumps(tc.arguments, ensure_ascii=False)}")
        if tc.name not in EXECUTE:
            raise StopAtCall(tc)
        result = await real_dispatch(tc, run_id)
        trace.extend(_result_lines(tc.name, result))
        return result

    agent.provider.chat = chat
    agent._dispatcher.dispatch = dispatch


def _result_lines(name: str, result: ToolResult) -> list[str]:
    if name != "recall":
        return [f"  -> {result.content[:120]!r}"]
    lines = result.content.splitlines()
    out = [f"  -> {len(lines)} line(s){' (error)' if result.is_error else ''}"]
    for line in lines:
        m = re.match(r"\[(\d+)\] \([^)]*\) (.*)", line)
        out.append(f"     [{m.group(1)}] {m.group(2)[:80]}" if m else f"     {line[:80]}")
    return out


async def _memory_block_ids(db: Database, first_user_text: str) -> list[str]:
    """The <memory> block's facts, mapped back to ids by exact content."""
    m = re.search(r"<memory>\n(.*?)\n</memory>", first_user_text, re.S)
    if m is None:
        return ["(no <memory> block — nothing similar matched)"]
    out = []
    for line in m.group(1).splitlines():
        fm = re.match(r"- \[[^\]]*\] (.*)", line)
        if fm is None:
            continue
        row = await (
            await db.conn.execute(
                "SELECT id FROM facts WHERE content = ? AND active = 1", (fm.group(1),)
            )
        ).fetchone()
        out.append(f"[{row['id'] if row else '?'}] {fm.group(1)[:80]}")
    return out


def _verdict(trace: list[str], stop: ToolCall | None) -> tuple[bool, str]:
    """PASS = recall ran before the first web_search (or, when web_search was
    never reached, before the turn stopped or ended)."""
    names = [line.split()[2] for line in trace if line.startswith("tool call:")]
    searched = "web_search" in names
    before = names[: names.index("web_search")] if searched else names
    passed = "recall" in before
    where = f"stopped at {stop.name}" if stop else "turn ended without a stopping call"
    target = "the first web_search" if searched else "acting (web_search not reached)"
    return passed, f"{'recall' if passed else 'NO recall'} before {target}; {where}"


async def run_check(db: Database, memory: MemoryStore, effort: str, trace: list[str]):
    """Build the agent, run the one turn; returns (session, stop call | None,
    answer | None). A turn error lands in the trace (an infrastructure fault
    is not a verdict)."""
    manager = MCPManager.from_config()  # open + close in this same task
    try:
        mcp_tools = await manager.start()
        agent, model = build_agent(
            run_store=RunStore(db), audit=AuditStore(db), allowlist=AllowlistStore(db),
            triggers=TriggerStore(db), memory=memory, client_config=ClientConfigStore(db),
            approvals=ApprovalStore(db), extra_tools=[*mcp_tools, NotifyTool(_no_push)],
        )
        agent.memory_extractor = None  # no Apple FM write-back from a check
        agent.max_steps = MAX_STEPS
        print(f"model={model} effort={effort} grouped={agent.toolset.grouped} "
              f"tools sent={len(agent.toolset.tools())}", flush=True)
        _wire(agent, trace)
        session = await Session.create(SessionStore(db), channel="web")
        try:
            answer = await agent.run_turn(session, PROMPT, channel="web", effort=effort)
        except StopAtCall as stop:
            return session, stop.call, None
        except Exception as e:  # reported with context by main(), exit 3
            trace.append(f"ERROR during the turn: {type(e).__name__}: {e}")
            return session, None, None
        return session, None, answer
    finally:
        await manager.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--effort", default="low", choices=["off", "low", "medium", "xhigh"])
    args = parser.parse_args()
    load_dotenv()
    if not os.environ.get("HARNESS_LOCAL_URL"):
        print("HARNESS_LOCAL_URL is not set — this check targets the local lane.", file=sys.stderr)
        return 2
    rows = _live_facts(default_db_path())
    trace: list[str] = []
    with tempfile.TemporaryDirectory(prefix="recall_check-") as tmp:
        db = await Database.open(Path(tmp) / "check.db")
        try:
            memory = await open_memory(db)
            indexed = await _seed_memory(db, memory, rows)
            print(f"facts copied={len(rows)} vectors={indexed} vec={memory.vec_available}"
                  f"{' embed_error=' + str(memory.embedder.last_error) if indexed == 0 else ''}")
            session, stop, answer = await run_check(db, memory, args.effort, trace)
            print("\n<memory> block facts:")
            first = session.messages[0].text if session.messages else ""
            for line in await _memory_block_ids(db, first or ""):
                print(f"  {line}")
        finally:
            await db.close()
    print("\ntrace:")
    for line in trace:
        print(f"  {line}")
    if answer is not None:
        print(f"\nfinal answer: {answer[:300]!r}")
    if any(line.startswith("ERROR") for line in trace):
        print("\nERROR: the turn failed before a verdict — see the trace")
        return 3
    passed, why = _verdict(trace, stop)
    print(f"\n{'PASS' if passed else 'FAIL'}: {why}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
