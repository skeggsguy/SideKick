"""agentd: the long-lived daemon. The second composition root (the CLI's
main.py is the first) — assembles the bus, orchestrator, scheduler, MCP
connections, push service, and the web gateway into one asyncio process.

Run: `uv run agentd` (HARNESS_HOST/HARNESS_PORT override 127.0.0.1:8787).
Expose to the iPad with: `tailscale serve --bg 8787`.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Callable

from dotenv import load_dotenv

from ..core.agent import Agent
from ..core.approvals import ApprovalWaiter
from ..core.events import EventBus
from ..core.naming import SessionNamer
from ..core.orchestrator import Orchestrator
from ..core.policy import ActionClass, Autonomy, deferred_approver
from ..core.prediction import ApprovalPredictor
from ..composition import build_agent, open_memory, sonnet_available
from ..prompt import user_name
from .approval_text import approval_summary
from ..tools.notify import NotifyTool
from ..providers.apple_fm import AppleFMProvider
from ..providers.ollama_provider import Classifier
from ..scheduler import Scheduler
from ..scheduler.watchers import GmailWatcher, WatcherRunner
from ..scheduler.watchers.gmail import GoogleGmailAPI
from ..store.client_config import ClientConfigStore
from ..store.watch_state import WatchStateStore
from ..store import Database, default_db_path
from ..store.approvals import ApprovalStore
from ..store.audit import AllowlistStore, AuditStore
from ..store.runs import RunStore
from ..store.sessions import SessionStore
from ..store.triggers import TriggerStore
from ..mcp import MCPManager
from ._daemon_lifecycle import (
    GRACEFUL_SHUTDOWN_S,
    backup_loop,
    build_server,
    install_shutdown_signals,
    shutdown_signal_handler,
    task_reaper,
)
from .app import Gateway, create_app
from .preview import ApprovalPreviews
from .stt import WhisperTranscriber
from .broadcast import RunBroadcaster
from .live import BgLive
from .model_status import SlotWatcher, make_slots_fetch
from .push import PushService

import os

DEFAULT_PORT = 8787


def background_agent_factory(
    agent_factory: Callable[..., Agent],
) -> Callable[[Autonomy], Agent]:
    """The orchestrator's factory: cron and watcher runs get the SAME lane as
    chat (the default). On the fork's -np 1 slot a chat sent mid-run then
    waits its turn behind the run's current call instead of preempting it —
    the 2026-10-25 background run shows what preempting led to: a cut call,
    then a stall of about an hour. Never passes chat_model (always local)."""
    return lambda autonomy: agent_factory(autonomy)


def _slot_watcher(local_url: str | None):
    """BK-041 G2b: bg runs ride the local lane, so their rows get the same
    busy/reading status a chat's does (None without a local server)."""
    if not local_url:
        return None
    fetch = make_slots_fetch(local_url)
    return lambda emit: SlotWatcher(fetch, emit)


def make_on_pending(
    push: PushService,
    approval_store: ApprovalStore,
    broadcaster: RunBroadcaster,
    previews: ApprovalPreviews | None = None,
):
    """The ApprovalWaiter's new-pending hook: push a notification — unless a
    client is visibly in the app, where the approvals poll already pops the
    modal and a push would be redundant. subscriber_count means "visible"
    only because app.js closes /api/events when the tab is hidden and
    reconnects when it becomes visible. Web-chat approvals flow through the
    same waiter and are suppressed too (the UI surfaces them inline)."""

    async def on_pending(
        approval_id: str, tool: str, args: dict, classification: ActionClass
    ) -> None:
        if broadcaster.subscriber_count > 0:
            return
        title = (
            "Send approval needed"
            if classification is ActionClass.OUTWARD
            else "Approval needed"
        )
        # Source suffix so a push read on the lock screen already says
        # whether an email/schedule (not Tom) instigated the action. The
        # row is committed before this hook fires, so get() sees it.
        a = await approval_store.get(approval_id)
        suffix = {"watch": " (email-triggered)", "scheduled": " (scheduled run)"}
        title += suffix.get(a.source_kind if a else "", "")
        # Best-effort preview so the lock-screen line names the real target
        # ("Trash Drive file "Q3 budget.xlsx"", not an opaque id). This hook
        # is awaited inside ApprovalWaiter.request(), so a resolver failure
        # must never propagate into the policy gate — and only the no-client
        # path (past the early-return above) ever pays the read.
        resolved = None
        if previews is not None and previews.previewable(tool):
            try:
                resolved = await asyncio.wait_for(
                    previews.resolve(tool, args, run_id=a.run_id if a else None), 5
                )
            except Exception:
                resolved = None
        await push.notify_all(
            title, approval_summary(tool, args, resolved), url="/#approvals"
        )

    return on_pending


def build_watchers(db, bus) -> list[WatcherRunner]:
    """Watchers the daemon should run. Gmail needs a cached Google token and
    can be disabled with HARNESS_GMAIL_WATCHER=off; triage runs on the
    on-device Apple Foundation Model (mail stays local; triage failure raises
    the email as actionable rather than dropping it)."""
    if os.environ.get("HARNESS_GMAIL_WATCHER", "on").lower() in ("off", "0", "false"):
        return []
    from mcp_servers.google_workspace.auth import token_path

    if not token_path().exists():
        print(
            "[agentd] gmail watcher off: no Google token "
            "(run `uv run google-workspace-mcp --auth`)",
            file=sys.stderr,
        )
        return []
    classifier = Classifier(AppleFMProvider())
    watcher = GmailWatcher(GoogleGmailAPI(), classifier, WatchStateStore(db))
    return [WatcherRunner(watcher, bus)]




async def amain() -> None:
    load_dotenv()
    db = await Database.open(default_db_path())
    mcp_manager = MCPManager.from_config()
    try:
        mcp_tools = await mcp_manager.start()

        sessions = SessionStore(db)
        run_store = RunStore(db)
        audit = AuditStore(db)
        allowlist = AllowlistStore(db)
        triggers = TriggerStore(db)
        approval_store = ApprovalStore(db)
        client_config = ClientConfigStore(db)
        # Before the orchestrator, scheduler and server exist, so no run can
        # be live: every non-CLI 'running' row died with an earlier process.
        if swept := await run_store.sweep_orphans():
            print(f"[agentd] runs: marked {swept} orphaned run(s) killed")
        memory = await open_memory(db)
        # Facts stored while Ollama was down get vectors now (KNN can't see
        # an unindexed fact). 0 when Ollama is still down; a DB error here
        # must not take the daemon down with it.
        try:
            if backfilled := await memory.backfill_vectors():
                print(f"[agentd] memory: back-filled {backfilled} fact vector(s)")
        except Exception as e:  # noqa: BLE001 - startup optimisation only
            print(f"[agentd] memory backfill failed: {type(e).__name__}: {e}")
        push = PushService(db, default_db_path().parent / "vapid_private.pem")
        broadcaster = RunBroadcaster()
        # LIVE resolver registry for approval previews: adapted READ_ONLY MCP
        # handles + the trigger store (the tool_catalog snapshot has no
        # callable handles). Backs /api/approvals/{id}/preview and push bodies.
        previews = ApprovalPreviews(
            tool_handles={t.name: t for t in mcp_tools},
            triggers=triggers,
            client_config=client_config,
        )
        on_pending = make_on_pending(push, approval_store, broadcaster, previews)

        # P(approve) forecasting on parked approvals (fire-and-forget;
        # on-device Apple FM). HARNESS_APPROVAL_PREDICTION=off disables.
        predictor = None
        if os.environ.get("HARNESS_APPROVAL_PREDICTION", "on").lower() not in (
            "off", "0", "false",
        ):
            predictor = ApprovalPredictor(
                Classifier(AppleFMProvider()), approval_store, run_store
            )

        waiter = ApprovalWaiter(
            approval_store, on_pending=on_pending, predictor=predictor
        )
        bus = EventBus()

        def agent_factory(
            autonomy: Autonomy, chat_model: str | None = None, lane: str = "chat"
        ):
            """chat_model="sonnet" is honored ONLY by the interactive chat
            route (opt-in Sonnet turns); the orchestrator and the catalog
            snapshot build plain local agents. Every daemon agent rides the
            default chat lane, background runs included (see
            background_agent_factory); `lane` stays a supported option."""
            agent, _ = build_agent(
                run_store=run_store,
                audit=audit,
                allowlist=allowlist,
                triggers=triggers,
                memory=memory,
                client_config=client_config,
                approvals=approval_store,
                extra_tools=[*mcp_tools, NotifyTool(push.notify_all)],
                autonomy=autonomy,
                chat_model=chat_model,
                lane=lane,
            )
            # Every daemon run gets the deferred approver: supervised runs
            # park on the approvals inbox; interactive web runs use the same
            # inbox as their "prompt" (the UI surfaces it inline).
            agent.policy.approver = deferred_approver(
                waiter, lambda: agent.current_run_id
            )
            return agent

        # Snapshot the tool catalog ONCE: classifications are immutable after
        # MCPManager.start(), so the Tools-Security screen serves this rather
        # than building an Agent per request. Build one throwaway agent to
        # enumerate the full registry (in-process + MCP + browser_delegate).
        _catalog_agent = agent_factory(Autonomy.INTERACTIVE)
        tool_catalog = [
            {
                "name": t.name,
                "description": t.description,
                "classification": t.classification.value,
                "remote": t.remote,
            }
            for t in _catalog_agent.registry.all()
        ]

        # Local-only chat naming: degrade to NULL → "New chat" when the model
        # is unavailable.
        namer = SessionNamer(AppleFMProvider(), sessions, run_store)

        # In-process Whisper dictation; the model lazy-loads on first use, so
        # there is no startup cost and no background task to supervise.
        stt = None
        if os.environ.get("HARNESS_STT", "on").lower() not in ("off", "0", "false"):
            stt_device = os.environ.get("HARNESS_STT_DEVICE", "auto").lower()
            if stt_device not in ("auto", "cuda", "cpu"):
                print(
                    f"[stt] unknown HARNESS_STT_DEVICE={stt_device!r}, using auto",
                    file=sys.stderr,
                )
                stt_device = "auto"
            stt = WhisperTranscriber(
                model_name=os.environ.get("HARNESS_STT_MODEL", "distil-large-v3"),
                device=stt_device,
            )

        async def on_run_complete(
            title: str, text: str, channel: str, session_id: str | None
        ) -> None:
            url = f"/#session={session_id}" if session_id else "/"
            await push.notify_all(title, text, url=url)

        bg_live = BgLive()

        def on_run_event(ev: dict) -> None:
            bg_live.observe(ev)  # BK-041 G4: /messages' bg_live
            broadcaster.publish(ev)

        # Background runs (cron + watcher) ride the same lane as chat: on the
        # fork's -np 1 slot a chat waits behind a run's in-flight call rather
        # than cutting it (see background_agent_factory).
        orchestrator = Orchestrator(
            bus,
            sessions,
            background_agent_factory(agent_factory),
            waiter,
            on_run_complete=on_run_complete,
            on_run_event=on_run_event,
            slot_watcher=_slot_watcher(os.environ.get("HARNESS_LOCAL_URL")),
        )
        scheduler = Scheduler(triggers, bus)
        watcher_runners = build_watchers(db, bus)

        host = os.environ.get("HARNESS_HOST", "127.0.0.1")
        port = int(os.environ.get("HARNESS_PORT", DEFAULT_PORT))
        gw = Gateway(
            sessions=sessions,
            runs=run_store,
            approval_store=approval_store,
            waiter=waiter,
            triggers=triggers,
            bus=bus,
            push=push,
            agent_factory=agent_factory,
            memory=memory,
            client_config=client_config,
            broadcaster=broadcaster,
            allowlist=allowlist,
            tool_catalog=tool_catalog,
            namer=namer,
            # The tailnet URL the in-app OAuth redirect comes back to, e.g.
            # https://<host>.ts.net (see docs/google-oauth-setup.md).
            public_url=os.environ.get("HARNESS_PUBLIC_URL"),
            previews=previews,
            stt=stt,
            # The orchestrator's own set, by reference: the chat 409 reads it.
            background_sessions=orchestrator.background_sessions,
            bg_live=bg_live,
            # Enables the chat /slots poller for the live model-status row.
            local_url=os.environ.get("HARNESS_LOCAL_URL"),
            # Gates the Sonnet chat entry (the key's only remaining purpose).
            sonnet_available=sonnet_available(),
            user_name=user_name(),
        )
        app = create_app(gw)
        server = build_server(app, host, port)

        def _stop() -> None:
            server.should_exit = True

        # A dead orchestrator or scheduler means the daemon is lying
        # about being alive — fail loud and stop the server.
        background = [
            (asyncio.create_task(orchestrator.run()), "orchestrator"),
            (asyncio.create_task(scheduler.run()), "scheduler"),
        ] + [
            (asyncio.create_task(runner.run()), f"watcher:{runner.watcher.name}")
            for runner in watcher_runners
        ]
        if os.environ.get("HARNESS_DB_BACKUP", "on").lower() not in ("off", "0", "false"):
            background.append((asyncio.create_task(backup_loop(db)), "db-backup"))
        for task, name in background:
            task.add_done_callback(task_reaper(name, _stop))

        install_shutdown_signals(server)
        print(f"agentd listening on http://{host}:{port}")
        await server.serve()

        for task, _ in background:
            task.cancel()
        # Detached chat runs would otherwise outlive the server teardown and
        # die mid-await on a closing DB. Cancelling mid-dispatch is safe:
        # add_step's single commit never leaves an orphaned tool_use.
        for task in list(gw.active_chats.values()):
            task.cancel()
        # Same for background runs: one stuck on the local model would hold
        # drain() for up to the lane's 1800s timeout.
        orchestrator.cancel_inflight()
        await orchestrator.drain()
    finally:
        # Nested so a failing MCP close can't skip the DB close — an unclosed
        # aiosqlite connection's non-daemon thread hangs interpreter exit.
        try:
            await mcp_manager.close()
        finally:
            await db.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
