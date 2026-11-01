"""Tests for agentd background task supervision and the approval push hook."""

from __future__ import annotations

import asyncio
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import gotcha
from harness.core.policy import ActionClass
from harness.core.events import EventBus
from harness.core.orchestrator import Orchestrator
from harness.gateway.agentd import (
    GRACEFUL_SHUTDOWN_S,
    build_server,
    make_on_pending,
    shutdown_signal_handler,
    task_reaper,
)
from harness.gateway.broadcast import RunBroadcaster
from harness.store.approvals import Approval


@pytest.mark.asyncio
async def test_normal_return_is_fatal():
    """Normal return from a background task is fatal, not just exceptions.

    This is why task_reaper exists as a separate module and doesn't return early
    on cancellation — the invariant is "normal exit kills the daemon" and tests must
    prove it."""
    async def do_work():
        return

    fired = []
    def record_fatal():
        fired.append(True)

    task = asyncio.create_task(do_work())
    task.add_done_callback(task_reaper("test_task", record_fatal))

    await task
    await asyncio.sleep(0)

    assert fired, gotcha(
        "supervise-any-exit-fatal",
        "normal return must trigger the fatal callback — supervise() treats ANY "
        "background-task exit as fatal, so daemon tasks loop forever and optional "
        "ones are created conditionally, never started-then-returned"
    )


@pytest.mark.asyncio
async def test_exception_is_fatal():
    """An exception in a background task is fatal."""
    async def do_work():
        raise ValueError("test error")

    fired = []
    def record_fatal():
        fired.append(True)

    task = asyncio.create_task(do_work())
    task.add_done_callback(task_reaper("test_task", record_fatal))

    with pytest.raises(ValueError):
        await task
    await asyncio.sleep(0)

    assert fired, gotcha(
        "supervise-any-exit-fatal",
        "exception must trigger the fatal callback"
    )


@pytest.mark.asyncio
async def test_cancellation_is_clean():
    """Cancellation of a background task is clean shutdown, not fatal."""
    async def do_work():
        await asyncio.sleep(10)

    fired = []
    def record_fatal():
        fired.append(True)

    task = asyncio.create_task(do_work())
    task.add_done_callback(task_reaper("test_task", record_fatal))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert not fired, gotcha(
        "supervise-any-exit-fatal",
        "cancellation must NOT trigger the fatal callback"
    )


# ---- approval push suppression (make_on_pending) ----


class _FakePush:
    def __init__(self):
        self.sent = []

    async def notify_all(self, title, body, url="/"):
        self.sent.append({"title": title, "body": body, "url": url})


class _FakeApprovals:
    def __init__(self, source_kind=None):
        self.source_kind = source_kind

    async def get(self, approval_id):
        return Approval(
            id=approval_id, run_id=None, tool="t", args={}, status="pending",
            created_at="", resolved_at=None, source_kind=self.source_kind,
        )


@pytest.mark.asyncio
async def test_pending_approval_pushes_when_no_client_connected():
    push = _FakePush()
    on_pending = make_on_pending(push, _FakeApprovals("watch"), RunBroadcaster())

    await on_pending("a1", "gmail_send", {"to": "x@y.z", "subject": "Hi"},
                     ActionClass.OUTWARD)

    assert len(push.sent) == 1
    assert push.sent[0]["title"] == "Send approval needed (email-triggered)"
    assert push.sent[0]["url"] == "/#approvals"


@pytest.mark.asyncio
async def test_pending_approval_title_for_local_scheduled():
    push = _FakePush()
    on_pending = make_on_pending(push, _FakeApprovals("scheduled"), RunBroadcaster())

    await on_pending("a1", "edit_file", {"path": "x"}, ActionClass.MUTATING_LOCAL)

    assert push.sent[0]["title"] == "Approval needed (scheduled run)"


@pytest.mark.asyncio
async def test_pending_approval_push_suppressed_while_client_in_app():
    """A connected /api/events subscriber means a client is VISIBLY in the app
    (app.js closes the stream when hidden) — the approvals modal covers the
    pending approval, so the push is redundant and must be skipped. Once the
    last subscriber disconnects, pushes resume."""
    push = _FakePush()
    broadcaster = RunBroadcaster()
    on_pending = make_on_pending(push, _FakeApprovals(), broadcaster)

    queue = broadcaster.subscribe()
    await on_pending("a1", "edit_file", {"path": "x"}, ActionClass.MUTATING_LOCAL)
    assert push.sent == [], "push must be suppressed while a client is in-app"

    broadcaster.unsubscribe(queue)
    await on_pending("a2", "edit_file", {"path": "x"}, ActionClass.MUTATING_LOCAL)
    assert len(push.sent) == 1, "push must resume once no client is connected"


# ---- push bodies use resolved preview info (approval_summary) ----


def test_approval_summary_prefers_resolved_names():
    """The push body is where an opaque id is most harmful (lock screen) —
    resolver output turns it into the real target's name; without it the
    templates fall back to id-based wording, never a bare k=v dump."""
    from harness.gateway.agentd import approval_summary

    s = approval_summary(
        "drive_trash_file", {"file_id": "f1"},
        {"name": "Q3 budget.xlsx", "type": "application/vnd.ms-excel"},
    )
    assert "Q3 budget.xlsx" in s and "f1" not in s and "restorable" in s
    fallback = approval_summary("drive_trash_file", {"file_id": "f1"})
    assert "f1" in fallback and "restorable" in fallback

    s = approval_summary(
        "gmail_draft_reply", {"message_id": "m1"},
        {"replying_to": "Alice <a@x.com>", "subject": "Hello"},
    )
    assert "Alice" in s and "Hello" in s

    s = approval_summary(
        "schedule_delete", {"trigger_id": "t1"},
        {"label": "Morning brief", "cron": "0 8 * * *"},
    )
    assert "Morning brief" in s and "permanently" in s
    assert "Delete job t1 permanently" == approval_summary(
        "schedule_delete", {"trigger_id": "t1"}
    )

    s = approval_summary(
        "drive_move", {"file_id": "f", "folder_id": "d"},
        {"file": "report.pdf", "destination_folder": "Archive"},
    )
    assert "report.pdf" in s and "Archive" in s

    s = approval_summary(
        "calendar_event_attendees",
        {"event_id": "e1", "add": ["bob@x.com"], "remove": ["carol@x.com"]},
        {"current_title": "Planning"},
    )
    assert "Planning" in s and "e1" not in s
    assert "invite bob@x.com" in s and "remove carol@x.com" in s

    s = approval_summary(
        "calendar_event_respond", {"event_id": "e1", "response": "accepted"},
        {"current_title": "Dinner", "organizer": "alice@x.com"},
    )
    assert "RSVP accepted" in s and "Dinner" in s and "alice@x.com" in s
    assert "e1" not in s

    s = approval_summary(
        "calendar_event_delete", {"event_id": "e1"},
        {"current_title": "Standup", "current_start": "2026-07-20T10:00:00+01:00"},
    )
    assert "Standup" in s and "e1" not in s and "Bin" in s
    fallback = approval_summary("calendar_event_delete", {"event_id": "e1"})
    assert "e1" in fallback and "Bin" in fallback


def test_approval_summary_schedule_edit_keeps_security_emphasis():
    """Autonomy flips and tool grants must survive the resolved-label rewrite —
    they're what the user is really approving from the notification alone."""
    from harness.gateway.agentd import approval_summary

    s = approval_summary(
        "schedule_edit",
        {"trigger_id": "t1", "cron": "0 9 * * *", "autonomy": "autonomous",
         "tools": ["web_search"]},
        {"label": "Morning brief", "cron": "0 8 * * *"},
    )
    assert "Morning brief" in s
    assert "AUTONOMOUS" in s and "web_search" in s


class _RaisingPreviews:
    def previewable(self, tool):
        return True

    async def resolve(self, tool, args):
        raise RuntimeError("resolver exploded")


@pytest.mark.asyncio
async def test_on_pending_survives_resolver_failure():
    """on_pending is awaited inside ApprovalWaiter.request() — a resolver
    failure must never propagate into the policy gate. The push still goes
    out with the args-only body."""
    push = _FakePush()
    on_pending = make_on_pending(
        push, _FakeApprovals(), RunBroadcaster(), previews=_RaisingPreviews()
    )

    await on_pending(
        "a1", "drive_trash_file", {"file_id": "f1"}, ActionClass.DESTRUCTIVE
    )

    assert len(push.sent) == 1
    assert "f1" in push.sent[0]["body"]  # args-only fallback wording


# -- shutdown: agentd owns SIGINT/SIGTERM ------------------------------------

_SHUTDOWN_APP = Path(__file__).parent / "fixtures" / "shutdown_app.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_app(tmp_path, graceful: float = 1.0) -> tuple[subprocess.Popen, int]:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, str(_SHUTDOWN_APP), str(port), str(tmp_path / "t.db"), str(graceful)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "READY", proc.stderr.read()
    return proc, port


def _stop(proc: subprocess.Popen, sig: int, timeout: float) -> tuple[float, str]:
    start = time.monotonic()
    proc.send_signal(sig)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(
            gotcha(
                "agentd-owns-shutdown-signals",
                f"one {signal.Signals(sig).name} did not stop the daemon within {timeout}s",
            )
        )
    return time.monotonic() - start, out


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_one_signal_stops_idle_daemon_and_closes_db(tmp_path, sig):
    """The 2026-09-30 hang: uvicorn re-raised the signal after serve(), the
    stray cancel skipped db.close(), and aiosqlite's non-daemon thread blocked
    exit forever (SIGTERM skipped cleanup outright)."""
    proc, _ = _start_app(tmp_path)
    _, out = _stop(proc, sig, timeout=5)
    assert "CLOSED" in out, gotcha(
        "agentd-owns-shutdown-signals",
        "DB close skipped — agentd must own SIGINT/SIGTERM (capture_signals no-op) "
        "or uvicorn re-raises, the stray cancel skips db.close(), and aiosqlite hangs exit",
    )
    assert proc.returncode == 0


def test_open_sse_client_cannot_hold_shutdown(tmp_path):
    proc, port = _start_app(tmp_path, graceful=1.0)
    with socket.create_connection(("127.0.0.1", port)) as client:
        client.sendall(b"GET /events HTTP/1.1\r\nHost: t\r\n\r\n")
        assert b"200" in client.recv(1024)
        elapsed, out = _stop(proc, signal.SIGINT, timeout=6)
    assert "CLOSED" in out
    assert elapsed < 4


def test_graceful_shutdown_is_bounded_by_default():
    server = build_server(object(), "127.0.0.1", 0)
    assert server.config.timeout_graceful_shutdown == GRACEFUL_SHUTDOWN_S, (
        "agentd owns SIGINT/SIGTERM (capture_signals is a no-op) and needs a bounded "
        "timeout_graceful_shutdown — without it an open SSE client holds shutdown, "
        "db.close() is skipped, and aiosqlite's non-daemon thread hangs exit"
    )


class _FakeServer:
    should_exit = False
    force_exit = False


def test_second_signal_forces():
    server = _FakeServer()
    on_signal = shutdown_signal_handler(server)
    on_signal()
    assert server.should_exit and not server.force_exit
    on_signal()
    assert server.force_exit


@pytest.mark.asyncio
async def test_cancel_inflight_lets_drain_return():
    orch = Orchestrator(EventBus(), None, None, None)
    orch._spawn(asyncio.sleep(3600), "stuck run")
    orch.cancel_inflight()
    await asyncio.wait_for(orch.drain(), timeout=1)
    assert not orch._tasks


# -- structural pins over amain (it builds its factories inline) ----------------

_AGENTD_SRC = (
    Path(__file__).parent.parent / "src" / "harness" / "gateway" / "agentd.py"
).read_text()


def test_agentd_never_selects_the_code_lane():
    """Decision 2026-10-25: background runs ride the chat lane. The provider's
    code-lane machinery stays (pinned in test_effort) — only the daemon stops
    using it."""
    assert 'lane="code"' not in _AGENTD_SRC
    assert "lane='code'" not in _AGENTD_SRC


def test_agentd_shares_background_sessions_with_the_gateway():
    """BK-013: the chat route's 409 reads the orchestrator's OWN set (shared
    by reference) — a copy would never see a run register."""
    assert "background_sessions=orchestrator.background_sessions" in _AGENTD_SRC, (
        "cron/watch runs own their session via orchestrator.background_sessions, "
        "shared BY REFERENCE into gw — gw.busy_reason reads it, and any new entry "
        "point that resumes a session must check gw.busy_reason"
    )


def test_agentd_sweeps_orphans_before_anything_can_start_a_run():
    """BK-014: the sweep may only run while no run can be live — before the
    orchestrator, scheduler and server exist."""
    sweep = _AGENTD_SRC.index("run_store.sweep_orphans()")
    for later in ("Orchestrator(", "Scheduler(", "build_server(app"):
        assert sweep < _AGENTD_SRC.index(later), later
