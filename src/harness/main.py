"""CLI entrypoint: the `harness` REPL plus the trigger/memory/runs/db
subcommands. Agent assembly lives in composition.py (shared with the daemon's
composition root, gateway/agentd.py)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv

from .composition import build_agent, open_memory
from .core.session import Session
from .interfaces.cli import CLI
from .mcp import MCPManager
from .store import Database, default_db_path
from .store.approvals import ApprovalStore
from .store.audit import AllowlistStore, AuditStore
from .store.client_config import ClientConfigStore
from .store.runs import RunStore
from .store.sessions import SessionStore
from .store.triggers import TriggerStore


async def amain(resume: str | None) -> None:
    load_dotenv()
    db = await Database.open(default_db_path())
    mcp_manager = MCPManager.from_config()
    try:
        sessions = SessionStore(db)
        agent, model = build_agent(
            run_store=RunStore(db),
            audit=AuditStore(db),
            allowlist=AllowlistStore(db),
            triggers=TriggerStore(db),
            memory=await open_memory(db),
            client_config=ClientConfigStore(db),
            approvals=ApprovalStore(db),
            extra_tools=await mcp_manager.start(),
        )

        session = None
        if resume is not None:
            session_id = None if resume == "latest" else resume
            session = await Session.resume(sessions, session_id)
            if session is None:
                print("No session to resume; starting a new one.")
        if session is None:
            session = await Session.create(sessions)

        cli = CLI(agent, model_label=model, session=session)
        agent.policy.approver = cli.approve
        await cli.run()
    finally:
        await mcp_manager.close()
        await db.close()


async def trigger_main(args: argparse.Namespace) -> None:
    """`harness trigger ...` — manage the schedule from the terminal."""
    load_dotenv()
    db = await Database.open(default_db_path())
    try:
        triggers = TriggerStore(db)
        if args.trigger_cmd == "add":
            from croniter import croniter

            from .tools.schedule_tools import validate_job

            if not croniter.is_valid(args.cron):
                sys.exit(f"invalid cron expression: {args.cron!r}")
            tools = [t.strip() for t in (args.tools or "").split(",") if t.strip()] or None
            # catalog=None: no live registry before the daemon; unknown names
            # surface at fire time in the tool_profile trace instead.
            error = validate_job(autonomy=args.autonomy, tools=tools, catalog=None)
            if error:
                sys.exit(error)
            tid = await triggers.add(
                "cron",
                args.cron,
                args.prompt,
                autonomy=args.autonomy,
                one_shot=args.one_shot,
                label=args.label,
                tools=tools,
            )
            print(f"added trigger {tid} (the daemon fires it; start with `uv run agentd`)")
        elif args.trigger_cmd == "list":
            rows = await triggers.list()
            if not rows:
                print("no triggers")
            for t in rows:
                state = "on " if t.enabled else "off"
                title = t.label or (t.playbook if len(t.playbook) <= 60 else t.playbook[:57] + "...")
                mode = "" if t.autonomy == "supervised" else f" ·{t.autonomy}"
                print(
                    f"{t.id}  [{state}{' ·1shot' if t.one_shot else ''}{mode}]  {title!r}  —  "
                    f"{t.spec!r}  →  {t.playbook!r}  (last: {t.last_fired_at or 'never'})"
                )
        elif args.trigger_cmd == "fire":
            await fire_trigger(db, triggers, args.id)
    finally:
        await db.close()


async def fire_trigger(db: Database, triggers: TriggerStore, trigger_id: str) -> None:
    """Fire via the daemon when it's up (the normal path); otherwise run the
    playbook inline with interactive approvals so it's testable without it."""
    trigger = await triggers.get(trigger_id)
    if trigger is None:
        sys.exit(f"no trigger with id {trigger_id!r}")

    import httpx

    port = os.environ.get("HARNESS_PORT", "8787")
    headers = {}
    if token := os.environ.get("HARNESS_API_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"http://127.0.0.1:{port}/api/triggers/{trigger_id}/fire",
                timeout=5,
                headers=headers,
            )
        resp.raise_for_status()
        print(f"fired {trigger_id} via agentd — watch the web UI / notifications")
        return
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 403:
            print(
                "agentd rejected the request (403) — set HARNESS_API_TOKEN in "
                ".env to match the daemon's. Running inline instead."
            )
        else:
            print("agentd not reachable — running inline (interactive approvals)")
    except httpx.ConnectError:
        print("agentd not reachable — running inline (interactive approvals)")

    from . import playbooks
    from .interfaces.cli import CLI

    # Same resolution as the orchestrator (row tools beat frontmatter; unsafe
    # autonomous rows downgrade) so inline testing matches production. The run
    # itself stays interactive — a human is at this terminal approving calls.
    job = playbooks.resolve_job(trigger.playbook, trigger.tools, trigger.autonomy)
    if job.downgraded:
        print(f"note: {job.downgraded}")
    pb = job.playbook
    mcp_manager = MCPManager.from_config()
    try:
        sessions = SessionStore(db)
        agent, model = build_agent(
            run_store=RunStore(db),
            audit=AuditStore(db),
            allowlist=AllowlistStore(db),
            triggers=triggers,
            memory=await open_memory(db),
            extra_tools=await mcp_manager.start(),
        )
        session = await Session.create(
            sessions, channel="cli", title=trigger.label or pb.name or pb.prompt[:60]
        )
        cli = CLI(agent, model_label=model, session=session)
        agent.policy.approver = cli.approve
        text = await agent.run_turn(
            session,
            pb.prompt,
            trigger=f"cron:{trigger_id}",
            channel="cli",
            playbook=pb.name,
            tool_profile=job.tools,
        )
        print(text)
        await triggers.mark_fired(trigger_id)
    finally:
        await mcp_manager.close()


async def memory_main(args: argparse.Namespace) -> None:
    """`harness memory ...` — inspect and prune long-term memory."""
    load_dotenv()
    db = await Database.open(default_db_path())
    try:
        memory = await open_memory(db)
        if args.memory_cmd == "list":
            facts = await memory.list_facts(
                category=args.category, include_inactive=args.all
            )
            if not facts:
                print("no facts in memory")
            for f in facts:
                state = "" if f.active else " (forgotten)"
                recalled = f.last_recalled_at[:10] if f.last_recalled_at else "never"
                print(
                    f"[{f.id}] ({f.category}, {f.confidence:.2f}, "
                    f"saved {f.updated_at[:10]}, last recalled {recalled})"
                    f"{state} {f.content}"
                )
        elif args.memory_cmd == "search":
            facts = await memory.search(args.query)
            if not facts:
                print("no matching facts")
            for f in facts:
                print(f"[{f.id}] ({f.category}) {f.content}")
            if not memory.vec_available:
                print(f"(keyword search only — vectors off: {memory.vec_error})")
        elif args.memory_cmd == "forget":
            if await memory.forget(args.id):
                print(f"fact {args.id} forgotten")
            else:
                sys.exit(f"no active fact with id {args.id}")
        elif args.memory_cmd == "reindex":
            await _memory_reindex(memory, rebuild=args.all)
    finally:
        await db.close()


async def _memory_reindex(memory, *, rebuild: bool) -> None:
    """`harness memory reindex [--all]`: backfill missing fact vectors, or
    (--all) re-embed every active fact. Exits non-zero when nothing could be
    embedded, so a silent keyword-only store never reads as success."""
    if not memory.vec_available:
        sys.exit(f"vectors off: {memory.vec_error}")
    indexed = await (memory.reindex_all() if rebuild else memory.backfill_vectors())
    error = getattr(memory.embedder, "last_error", None)
    if indexed == 0 and error:
        sys.exit(f"embedder unavailable, index unchanged: {error}")
    mode = "re-embedded" if rebuild else "back-filled"
    print(f"{mode} {indexed} fact vector(s)")
    print(
        f"coverage: {await memory.count_indexed()} of "
        f"{await memory.count_active()} active facts have vectors"
    )


async def runs_main(args: argparse.Namespace) -> None:
    """`harness runs ...` — inspect run telemetry and spend from the terminal."""
    load_dotenv()
    db = await Database.open(default_db_path())
    try:
        runs = RunStore(db)
        if args.runs_cmd == "list":
            rows = await runs.list(limit=args.limit)
            if not rows:
                print("no runs")
            for r in rows:
                source = r["playbook"] or r["trigger"]
                print(
                    f"{r['id']}  {r['status']:<7}  {r['started_at'][:16]}  "
                    f"{source:<24}  {r['input_tokens']}/{r['output_tokens']} tok  "
                    f"${r['cost_usd']:.4f}"
                )
        elif args.runs_cmd == "show":
            run = await runs.get(args.id)
            if run is None:
                sys.exit(f"no run with id {args.id!r}")
            for key in (
                "id", "status", "trigger", "channel", "playbook", "model",
                "started_at", "ended_at",
            ):
                print(f"{key:>12}: {run[key]}")
            print(
                f"{'tokens':>12}: {run['input_tokens']} in / "
                f"{run['output_tokens']} out · cache read "
                f"{run['cache_read_tokens']} / written {run['cache_write_tokens']}"
            )
            print(f"{'cost':>12}: ${run['cost_usd']:.4f}")
            for event in await runs.events(args.id):
                data = json.dumps(event["data"], default=str)
                if len(data) > 200:
                    data = data[:200] + "…"
                print(f"  {event['ts'][11:19]}  {event['kind']:<16} {data}")
        elif args.runs_cmd == "summary":
            from .tools.run_stats import format_summary

            print(format_summary(await runs.summary(days=args.days)))
    finally:
        await db.close()


async def db_main(args: argparse.Namespace) -> None:
    """`harness db ...` — database maintenance."""
    from .store.backup import backup_database, default_backup_dir

    load_dotenv()
    db = await Database.open(default_db_path())
    try:
        if args.db_cmd == "backup":
            from pathlib import Path

            backup_dir = Path(args.dir).expanduser() if args.dir else default_backup_dir()
            dest = await backup_database(db, backup_dir, keep=args.keep)
            print(f"backed up {default_db_path()} → {dest}")
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="harness", description="Personal agent harness")
    parser.add_argument(
        "--resume",
        nargs="?",
        const="latest",
        default=None,
        metavar="SESSION_ID",
        help="resume a session (most recent if no id given)",
    )
    sub = parser.add_subparsers(dest="cmd")
    trigger = sub.add_parser("trigger", help="manage scheduled triggers")
    trigger_sub = trigger.add_subparsers(dest="trigger_cmd", required=True)
    add = trigger_sub.add_parser("add", help="add a cron trigger")
    add.add_argument("cron", help="5-field cron expression, local time")
    add.add_argument("prompt", help="playbook name or inline prompt")
    add.add_argument("--one-shot", action="store_true", help="disable after first fire")
    add.add_argument("--label", default=None, help="short display name")
    add.add_argument(
        "--autonomy",
        default="supervised",
        choices=["supervised", "autonomous"],
        help="autonomous honors the always-allow list within --tools (requires --tools)",
    )
    add.add_argument(
        "--tools", default=None, help="comma-separated tool allocation for the job"
    )
    trigger_sub.add_parser("list", help="list triggers")
    fire = trigger_sub.add_parser("fire", help="fire a trigger immediately")
    fire.add_argument("id")

    memory = sub.add_parser("memory", help="inspect long-term memory")
    memory_sub = memory.add_subparsers(dest="memory_cmd", required=True)
    mem_list = memory_sub.add_parser("list", help="list remembered facts")
    mem_list.add_argument("--category", default=None, help="filter by category")
    mem_list.add_argument("--all", action="store_true", help="include forgotten facts")
    mem_search = memory_sub.add_parser("search", help="search memory")
    mem_search.add_argument("query")
    mem_forget = memory_sub.add_parser("forget", help="deactivate a fact by id")
    mem_forget.add_argument("id", type=int)
    mem_reindex = memory_sub.add_parser(
        "reindex", help="embed facts missing a vector (needs Ollama)"
    )
    mem_reindex.add_argument(
        "--all", action="store_true",
        help="re-embed EVERY active fact, replacing all stored vectors",
    )

    runs = sub.add_parser("runs", help="inspect run telemetry and spend")
    runs_sub = runs.add_subparsers(dest="runs_cmd", required=True)
    runs_list = runs_sub.add_parser("list", help="recent runs with cost")
    runs_list.add_argument("--limit", type=int, default=20)
    runs_show = runs_sub.add_parser("show", help="one run's details and trace")
    runs_show.add_argument("id")
    runs_summary = runs_sub.add_parser("summary", help="cost/token totals by source")
    runs_summary.add_argument("--days", type=int, default=7)

    db = sub.add_parser("db", help="database maintenance")
    db_sub = db.add_subparsers(dest="db_cmd", required=True)
    db_backup = db_sub.add_parser("backup", help="snapshot the live DB (online, safe)")
    db_backup.add_argument("--dir", default=None, help="backup directory")
    db_backup.add_argument("--keep", type=int, default=7, help="backups to retain")

    args = parser.parse_args()
    if args.cmd == "trigger":
        asyncio.run(trigger_main(args))
    elif args.cmd == "memory":
        asyncio.run(memory_main(args))
    elif args.cmd == "runs":
        asyncio.run(runs_main(args))
    elif args.cmd == "db":
        asyncio.run(db_main(args))
    else:
        asyncio.run(amain(args.resume))


if __name__ == "__main__":
    main()
