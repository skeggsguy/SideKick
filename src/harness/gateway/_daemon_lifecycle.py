"""Daemon lifecycle plumbing: daily-backup loop, the uvicorn Server subclass
that leaves signal ownership to agentd, and the background-task reaper.
Kept out of agentd.py (the composition root) — pure mechanism, no wiring."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import traceback

import uvicorn

from ..store import Database
from ..store.backup import backup_if_stale, default_backup_dir

# Seconds uvicorn waits for open connections at shutdown before cancelling
# them — /api/events is an endless SSE stream, so without a bound one open
# PWA holds the daemon up forever.
GRACEFUL_SHUTDOWN_S = 5


async def backup_loop(db: Database) -> None:
    """Daily DB backup (P7). Checks hourly, snapshots when the newest backup
    is ~a day old. A failed attempt (disk full, etc.) is reported and retried
    next hour — it must not kill the daemon, but never fails silently."""
    backup_dir = default_backup_dir()
    while True:
        try:
            dest = await backup_if_stale(db, backup_dir)
            if dest is not None:
                print(f"[agentd] db backed up → {dest}", file=sys.stderr)
        except Exception:
            print("[agentd] db backup failed (retrying in 1h):", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        await asyncio.sleep(3600)


class _Server(uvicorn.Server):
    """uvicorn minus its signal capture: agentd owns SIGINT/SIGTERM
    (install_shutdown_signals). uvicorn's capture_signals RE-RAISES the signal
    once serve() returns; asyncio.run's SIGINT handler then cancels amain
    mid-cleanup, db.close() is skipped, and aiosqlite's non-daemon worker
    thread blocks interpreter exit forever (SIGTERM instead dies on the
    default handler and skips cleanup entirely)."""

    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


def build_server(
    app, host: str, port: int, graceful_timeout: float = GRACEFUL_SHUTDOWN_S
) -> _Server:
    return _Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=graceful_timeout,
        )
    )


def shutdown_signal_handler(server: uvicorn.Server) -> callable:
    """First signal: graceful stop. Second: force (skip uvicorn's connection
    wait; cleanup still runs). Third: hard exit, in case cleanup wedges."""
    def on_signal() -> None:
        if server.force_exit:
            os._exit(1)
        if server.should_exit:
            print("[agentd] forcing shutdown", file=sys.stderr, flush=True)
            server.force_exit = True
            return
        print(
            "[agentd] shutting down… (Ctrl-C again to force)",
            file=sys.stderr,
            flush=True,
        )
        server.should_exit = True
    return on_signal


def install_shutdown_signals(server: uvicorn.Server) -> None:
    """Route SIGINT/SIGTERM to the server for the rest of the loop's life
    (loop.close() removes them), so no signal ever cancels the cleanup."""
    loop = asyncio.get_running_loop()
    handler = shutdown_signal_handler(server)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handler)


def task_reaper(name: str, on_fatal) -> callable:
    """Done-callback for daemon background tasks. ANY exit — normal return
    included — is fatal (pinned: tests/test_agentd.py::test_normal_return_is_fatal),
    so tasks loop forever and optional ones are created conditionally; only
    cancellation is clean shutdown."""
    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        print(f"[agentd] FATAL: {name} task died:", file=sys.stderr)
        if task.exception():
            traceback.print_exception(task.exception(), file=sys.stderr)
        on_fatal()
    return _done
