"""Manual measurement: what a chat's FIRST request costs, section by section.

Builds the main agent exactly as the harness does (build_agent() over the real
MCP tool list, throwaway stores on a temp DB — never harness.db), takes one
real session's frozen ``<user_profile>`` and first user message (idx 0, the
persisted ``<memory>`` block + the user's text) from the live DB READ-ONLY
(sqlite ``mode=ro``), and asks the local server's tokenizer for the totals via
``POST /v1/messages/count_tokens``. Each section is the delta between two
counts, so the chat template's framing is charged to no section:

  base    = system prompt alone                 (vs. no system)
  profile = + the frozen <user_profile> suffix  (vs. base only)
  tools   = + the tool definitions sent         (vs. no tools)
  memory  = the real first user message         (vs. a one-word "hi")

The request shape matches the 18 Oct 2026 baseline (5,912 tokens: base 627,
profile 794, tools 3,625, memory 866) — no chat_template_kwargs, so the
numbers compare like for like; a live call's effort flags add a few more.

The local server is SHARED with the live chat (-np 1): token counting takes no
generation slot, but run this by hand, once — never in loops.

Run with: uv run python scripts/prompt_budget.py [--session ID] [--per-tool]
Needs HARNESS_LOCAL_URL (from .env or the shell).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from dotenv import load_dotenv

from harness.composition import build_agent
from harness.core import tool_groups
from harness.core.agent import Agent
from harness.mcp import MCPManager
from harness.memory import MemoryStore
from harness.providers._anthropic_wire import _to_api_messages
from harness.store import Database
from harness.store.approvals import ApprovalStore
from harness.store.audit import AllowlistStore, AuditStore
from harness.store.client_config import ClientConfigStore
from harness.store.db import default_db_path
from harness.store.runs import RunStore
from harness.store.sessions import _message_from_dict
from harness.store.triggers import TriggerStore
from harness.tools.notify import NotifyTool

# 18 Oct 2026, session cd70e29bda99, before the prompt trim (plan prompt-trim).
BASELINE = {"base": 627, "profile": 794, "tools": 3625, "memory": 866}
HI = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


async def _no_push(title: str, body: str, url: str) -> int:
    raise RuntimeError("prompt_budget never sends pushes")


def _read_session(db_path: Path, session_id: str | None) -> tuple[str, str, Any]:
    """(session id, frozen profile, first user Message) — read-only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        if session_id is None:
            row = con.execute(
                "SELECT s.id FROM sessions s WHERE s.memory_profile IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id "
                "AND m.idx = 0) ORDER BY s.created_at DESC LIMIT 1"
            ).fetchone()
            if row is None:
                raise SystemExit("no session with a frozen profile and a first message")
            session_id = row["id"]
        srow = con.execute(
            "SELECT memory_profile FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        mrow = con.execute(
            "SELECT payload_json FROM messages WHERE session_id = ? AND idx = 0",
            (session_id,),
        ).fetchone()
    finally:
        con.close()
    if srow is None or mrow is None:
        raise SystemExit(f"session {session_id!r} not found (or has no first message)")
    first = _message_from_dict(json.loads(mrow["payload_json"]))
    if first.role != "user":
        raise SystemExit(f"session {session_id!r} idx 0 is {first.role!r}, not user")
    return session_id, srow["memory_profile"] or "", first


class Counter:
    def __init__(self, client: httpx.Client, model: str) -> None:
        self.client, self.model = client, model

    def count(self, **body: Any) -> int:
        resp = self.client.post(
            "/v1/messages/count_tokens", json={"model": self.model, **body}
        )
        resp.raise_for_status()
        return resp.json()["input_tokens"]

    def tokenize(self, text: str) -> int:
        resp = self.client.post("/tokenize", json={"content": text})
        resp.raise_for_status()
        return len(resp.json()["tokens"])


def _api_tools(tools) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def measure(agent: Agent, model: str, profile: str, first, per_tool: bool) -> None:
    system_base = agent.system_prompt
    # The agent's own suffixing (profile at the END, NULL/"" sends base).
    system = agent._system_for(SimpleNamespace(memory_profile=profile))
    tools = _api_tools(agent.toolset.tools())
    first_api = _to_api_messages(
        [first], False, keep_all_thinking=True, load_image=lambda p: Path(p).read_bytes()
    )
    url = os.environ["HARNESS_LOCAL_URL"].rstrip("/")
    with httpx.Client(base_url=url, timeout=30, trust_env=False) as client:
        c = Counter(client, model)
        frame = c.count(messages=HI)
        with_base = c.count(system=system_base, messages=HI)
        with_profile = c.count(system=system, messages=HI)
        with_tools = c.count(system=system, tools=tools, messages=HI)
        full = c.count(system=system, tools=tools, messages=first_api)
        sections = {
            "base": with_base - frame,
            "profile": with_profile - with_base,
            "tools": with_tools - with_profile,
            "memory": full - with_tools,
        }
        print(f"{'section':<8} {'tokens':>7} {'baseline':>9} {'delta':>7}")
        for name, n in sections.items():
            print(f"{name:<8} {n:>7} {BASELINE[name]:>9} {n - BASELINE[name]:>+7}")
        total, base_total = sum(sections.values()), sum(BASELINE.values())
        print(f"{'total':<8} {total:>7} {base_total:>9} {total - base_total:>+7}")
        print(f"(raw first-request count incl. template framing: {full}; frame {frame})")
        print(f"system prompt: {len(system_base)} chars; tools sent: {len(tools)}")
        groups = agent.toolset.groups()
        if "files" in groups:
            content, _ = tool_groups.load_tools_result({"group": "files"}, groups)
            print(f"load_tools('files') result, when a file task loads it: "
                  f"{c.tokenize(content)} tokens")
        if per_tool:
            for t in tools:
                text = json.dumps(
                    {"name": t["name"], "description": t["description"],
                     "parameters": t["input_schema"]},
                    ensure_ascii=False,
                )
                print(f"  {t['name']:<14} {c.tokenize(text):>5}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--session", help="session id (default: the latest with a profile)")
    parser.add_argument("--per-tool", action="store_true", help="tokenize each sent tool")
    parser.add_argument("--db", type=Path, default=None, help="live DB (read-only)")
    args = parser.parse_args()

    load_dotenv()
    if not os.environ.get("HARNESS_LOCAL_URL"):
        print("HARNESS_LOCAL_URL is not set — this measures the local lane.", file=sys.stderr)
        return 2
    session_id, profile, first = _read_session(args.db or default_db_path(), args.session)

    with tempfile.TemporaryDirectory(prefix="prompt_budget-") as tmp:
        db = await Database.open(Path(tmp) / "budget.db")
        # MCP connections must open and close in this same task.
        manager = MCPManager.from_config()
        try:
            mcp_tools = await manager.start()
            agent, model = build_agent(
                run_store=RunStore(db),
                audit=AuditStore(db),
                allowlist=AllowlistStore(db),
                triggers=TriggerStore(db),
                memory=MemoryStore(db),
                client_config=ClientConfigStore(db),
                approvals=ApprovalStore(db),
                extra_tools=[*mcp_tools, NotifyTool(_no_push)],
            )
            print(f"model={model} grouped={agent.toolset.grouped} session={session_id}")
            measure(agent, model, profile, first, args.per_tool)
        finally:
            await manager.close()
            await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
