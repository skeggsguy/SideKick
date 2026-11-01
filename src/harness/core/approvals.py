"""In-memory wait/wake for deferred approvals.

A supervised run that hits a gated tool calls request() and pauses on a
future; the web UI (or CLI) resolves the approval and the run resumes. The
durable record lives in store/approvals.py — if the daemon restarts, pending
rows survive but their waiting runs do not, so unresolved rows are simply
expired by the next decision attempt or left for the inbox to show as stale.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from .policy import ActionClass
from ..store.approvals import ApprovalStore

# A cron run waits this long for a human before giving up.
DEFAULT_TIMEOUT_S = 30 * 60

# Outward sends (e.g. email) wait much longer: the user may only see the
# push when they next pick up their phone, and a missed send is fail-safe.
# It OVERRIDES timeout_s: an "expires" test must use a non-OUTWARD class or hang.
OUTWARD_TIMEOUT_S = 6 * 3600

# Called after a pending approval is created — the gateway hooks push
# notifications here. (approval_id, tool, args, classification)
OnPending = Callable[[str, str, dict, ActionClass], Awaitable[None]]


class ApprovalWaiter:
    def __init__(
        self,
        store: ApprovalStore,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        on_pending: Optional[OnPending] = None,
        predictor=None,  # core.prediction.ApprovalPredictor | None
    ) -> None:
        self.store = store
        self.timeout_s = timeout_s
        self.on_pending = on_pending
        #: When set, each parked approval spawns a fire-and-forget P(approve)
        #: forecast that races the human — never on the gate's critical path.
        self.predictor = predictor
        self._futures: dict[str, asyncio.Future[bool]] = {}

    async def request(
        self,
        run_id: str | None,
        tool: str,
        args: dict,
        classification: ActionClass = ActionClass.MUTATING_LOCAL,
        gate_kind: str | None = None,
    ) -> bool:
        """Create a pending approval and wait for the decision.
        Returns the decision; timeout expires the approval and denies.
        Outward sends get a longer window than local mutations. gate_kind
        (policy GATE_* constant) records which egress gate parked a
        READ_ONLY call, so the approval surfaces can explain it."""
        approval_id = await self.store.create(
            run_id, tool, args, classification.value, gate_kind=gate_kind
        )
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._futures[approval_id] = future
        if self.predictor is not None:
            self.predictor.spawn(approval_id, run_id, tool, args, classification)
        if self.on_pending is not None:
            await self.on_pending(approval_id, tool, args, classification)
        timeout = (
            OUTWARD_TIMEOUT_S
            if classification is ActionClass.OUTWARD
            else self.timeout_s
        )
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            await self.store.resolve(approval_id, "expired")
            return False
        except asyncio.CancelledError:
            # The waiting run was stopped (web UI Stop / shutdown): resolve
            # the row `expired` before re-raising, or a pending approval can
            # never fire and lingers in the inbox forever. Shielded so the
            # in-flight cancellation can't kill the write mid-flight.
            await asyncio.shield(self.store.resolve(approval_id, "expired"))
            raise
        finally:
            self._futures.pop(approval_id, None)

    async def resolve(self, approval_id: str, approved: bool) -> bool:
        """Apply a decision. Returns False if the approval wasn't pending
        (already decided or expired), so a double-tap can't double-fire."""
        ok, _ = await self.resolve_detailed(approval_id, approved)
        return ok

    async def resolve_detailed(
        self, approval_id: str, approved: bool
    ) -> tuple[bool, bool]:
        """Like resolve(), but also reports whether a *live* run was actually
        woken. After a daemon restart the pending row survives but its future
        does not, so resolving marks the row (ok=True) yet wakes nobody
        (woken=False) — the gateway surfaces that so the user isn't told an
        irreversible send went through when no run was waiting to perform it."""
        ok = await self.store.resolve(
            approval_id, "approved" if approved else "denied"
        )
        woken = False
        if ok:
            future = self._futures.get(approval_id)
            if future is not None and not future.done():
                future.set_result(approved)
                woken = True
        return ok, woken
