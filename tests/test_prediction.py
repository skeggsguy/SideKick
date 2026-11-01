"""Approval prediction: fire-and-forget P(approve) forecasts on parked rows."""

from __future__ import annotations

import asyncio

from harness.core.approvals import ApprovalWaiter
from harness.core.policy import ActionClass
from harness.core.prediction import BUCKETS, ApprovalPredictor
from harness.store.approvals import ApprovalStore
from harness.store.runs import RunStore


class StubClassifier:
    def __init__(self, label="probably_approve", delay=0.0, error=None):
        self.label = label
        self.delay = delay
        self.error = error
        self.calls = []

    async def classify(self, text, labels, instruction):
        self.calls.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.label, "local"


async def _drain(predictor):
    if predictor._tasks:
        await asyncio.gather(*predictor._tasks, return_exceptions=True)


async def test_prediction_written_to_row(db):
    store = ApprovalStore(db)
    runs = RunStore(db)
    run_id = await runs.start(None, model="m", trigger="watch:gmail", channel="web")
    clf = StubClassifier("almost_certainly_approve")
    predictor = ApprovalPredictor(clf, store, runs)

    aid = await store.create(run_id, "gmail_draft_reply", {"body": "x"}, "MUTATING_LOCAL")
    predictor.spawn(aid, run_id, "gmail_draft_reply", {"body": "x"}, ActionClass.MUTATING_LOCAL)
    await _drain(predictor)

    assert (await store.get(aid)).predicted_p == BUCKETS["almost_certainly_approve"]
    # Forecast input carries the tool's history so far.
    assert "Past verdicts" in clf.calls[0]
    kinds = [e["kind"] for e in await runs.events(run_id)]
    assert "approval_prediction" in kinds


async def test_prediction_failure_traces_and_leaves_null(db):
    store = ApprovalStore(db)
    runs = RunStore(db)
    run_id = await runs.start(None, model="m", trigger="watch:gmail", channel="web")
    predictor = ApprovalPredictor(
        StubClassifier(error=RuntimeError("ollama down, no cloud")), store, runs
    )

    aid = await store.create(run_id, "edit_file", {}, "MUTATING_LOCAL")
    predictor.spawn(aid, run_id, "edit_file", {}, ActionClass.MUTATING_LOCAL)
    await _drain(predictor)

    assert (await store.get(aid)).predicted_p is None
    kinds = [e["kind"] for e in await runs.events(run_id)]
    assert "approval_prediction_error" in kinds


async def test_slow_predictor_never_delays_the_gate(db):
    """The forecast races the human: resolving the approval must not wait for
    (or be affected by) a predictor that is still running."""
    store = ApprovalStore(db)
    predictor = ApprovalPredictor(StubClassifier(delay=0.2), store, None)
    waiter = ApprovalWaiter(store, predictor=predictor)

    async def decide_soon():
        for _ in range(100):
            await asyncio.sleep(0)
            pending = await store.pending()
            if pending:
                await waiter.resolve(pending[0].id, True)
                return

    t0 = asyncio.get_event_loop().time()
    approved, _ = await asyncio.gather(
        waiter.request("r1", "edit_file", {}, ActionClass.MUTATING_LOCAL),
        decide_soon(),
    )
    elapsed = asyncio.get_event_loop().time() - t0
    assert approved is True
    assert elapsed < 0.15  # gate resolved while the 0.2s prediction ran
    await _drain(predictor)  # prediction still completes afterwards
    hist = await store.history()
    assert hist[0]["status"] == "approved"
