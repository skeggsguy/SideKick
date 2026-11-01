"""Raw Chrome DevTools Protocol client over a WebSocket.

Protocol plumbing only — no fetch policy. Connects to an already-running
Chrome's webSocketDebuggerUrl and multiplexes commands (id-keyed futures)
and events (listeners + one-shot waiters). Deliberately NEVER touches the
Runtime domain: Runtime.enable is a known Cloudflare bot-detection signal,
so browser_fetch reads the DOM via DOM.getDocument + DOM.getOuterHTML
instead (see docs/browser-fallback-setup.md).
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any, Callable

import websockets

# A heavy page's outerHTML exceeds websockets' 1 MB default max_size in a
# single CDP frame — raise it or big pages truncate mid-read (pinned by
# tests/test_browser_fetch.py::test_large_outerhtml_arrives_intact).
_MAX_SIZE = 32 * 1024 * 1024


class CDPError(Exception):
    """A CDP command failed, timed out, or the connection dropped."""


class CDPClient:
    """One WebSocket to Chrome; commands via send(), events via listeners."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._waiters: list[tuple[Callable[[dict], bool], asyncio.Future]] = []
        self._dead: CDPError | None = None
        self._reader = asyncio.create_task(self._read_loop())

    @classmethod
    async def connect(
        cls, ws_url: str, *, ws_connect=websockets.connect
    ) -> "CDPClient":
        ws = await ws_connect(ws_url, max_size=_MAX_SIZE, compression=None)
        return cls(ws)

    async def send(
        self,
        method: str,
        params: dict | None = None,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Issue one CDP command and await its result. Session-scoped commands
        (everything after Target.attachToTarget) pass session_id."""
        if self._dead is not None:
            # The read loop is gone — fail fast instead of waiting out `timeout`
            # on a reply that can never arrive.
            raise self._dead
        msg_id = next(self._ids)
        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise CDPError(f"CDP timeout: {method}") from None
        except websockets.WebSocketException as exc:
            raise CDPError(f"CDP transport error on {method}: {exc}") from exc
        finally:
            self._pending.pop(msg_id, None)

    def expect_event(self, predicate: Callable[[dict], bool]) -> asyncio.Future:
        """Register a one-shot event waiter NOW (synchronously); await it via
        wait_event(). Register BEFORE sending the command that triggers the
        event, or a fast event can fire in the gap after the command's ack and
        be missed (costing the caller its full timeout)."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if self._dead is not None:
            fut.set_result(None)  # disconnected: resolve like _fail_all would
            return fut
        self._waiters.append((predicate, fut))
        return fut

    async def wait_event(self, fut: asyncio.Future, timeout: float) -> dict | None:
        """Await an expect_event() future: the matching event, or None on
        timeout/disconnect."""
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._waiters = [(p, f) for (p, f) in self._waiters if f is not fut]

    def discard_waiter(self, fut: asyncio.Future) -> None:
        """Drop an expect_event() registration that will never be awaited
        (the triggering command failed before wait_event could consume it)."""
        self._waiters = [(p, f) for (p, f) in self._waiters if f is not fut]

    async def wait_for_event(
        self, predicate: Callable[[dict], bool], timeout: float
    ) -> dict | None:
        """The first event matching predicate, or None on timeout/disconnect.
        Only safe when the event cannot already be in flight — otherwise use
        expect_event() before the triggering command, then wait_event()."""
        return await self.wait_event(self.expect_event(predicate), timeout)

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """Register a sync callback for every event message."""
        self._listeners.append(fn)

    async def close(self) -> None:
        self._reader.cancel()
        try:
            await self._reader
        except asyncio.CancelledError:
            pass
        try:
            await self._ws.close()
        except (OSError, websockets.WebSocketException):
            pass

    async def _read_loop(self) -> None:
        error: CDPError | None = None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._dispatch(msg)
        except websockets.ConnectionClosed:
            pass
        except Exception as exc:  # noqa: BLE001 — carried into every failed future
            # A listener/predicate blew up. Don't die silently AND don't leave
            # the failure stored on the task (close() awaits it, and a stored
            # exception would raise out of a `finally: await cdp.close()`,
            # masking the fetch result): surface it via the failed futures.
            error = CDPError(f"CDP read loop failed: {type(exc).__name__}: {exc}")
        finally:
            # Never hang a caller on a dead socket: fail every pending command
            # and resolve waiters (silent task death is the daemon's primary
            # failure mode).
            self._fail_all(error or CDPError("CDP connection closed"))

    def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        if mid is not None and mid in self._pending:
            fut = self._pending.pop(mid)
            if not fut.done():
                if "error" in msg:
                    err = msg["error"]
                    detail = err.get("message", err) if isinstance(err, dict) else err
                    fut.set_exception(CDPError(str(detail)))
                else:
                    fut.set_result(msg.get("result", {}))
            return
        for fn in list(self._listeners):
            fn(msg)
        for predicate, fut in list(self._waiters):
            if not fut.done() and predicate(msg):
                fut.set_result(msg)

    def _fail_all(self, err: CDPError) -> None:
        self._dead = err
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(err)
        self._pending.clear()
        for _predicate, fut in list(self._waiters):
            if not fut.done():
                fut.set_result(None)
        self._waiters.clear()
