"""calibration_report tool: predicted-vs-actual bins and promotion candidates."""

from __future__ import annotations

from harness.core.policy import ActionClass
from harness.store.approvals import ApprovalStore
from harness.tools.calibration import CalibrationReportTool, format_report


async def _resolved(store, tool, p, status, classification="MUTATING_LOCAL"):
    aid = await store.create("r", tool, {}, classification)
    await store.set_prediction(aid, p)
    await store.resolve(aid, status)


async def test_report_bins_and_flags_candidate(db):
    store = ApprovalStore(db)
    # A well-calibrated MUTATING_LOCAL tool: 10 approvals, high forecasts.
    for _ in range(10):
        await _resolved(store, "gmail_draft_reply", 0.95, "approved")
    # A poorly-calibrated one: forecast high, actually denied.
    await _resolved(store, "edit_file", 0.95, "denied")
    # OUTWARD must never surface as a candidate however clean its record.
    for _ in range(10):
        await _resolved(store, "gmail_send", 0.95, "approved", "OUTWARD")

    out = await CalibrationReportTool(store).run({"days": 30})
    assert "gmail_draft_reply: 10 decisions · approved 100%" in out
    assert "Measured allowlist candidates" in out
    assert "gmail_draft_reply (10 approvals, 0 denials" in out
    assert "gmail_send" not in out.split("candidates")[1]  # never promoted
    assert "edit_file: 1 decisions · approved 0%" in out


async def test_candidate_requires_zero_denials_and_samples(db):
    store = ApprovalStore(db)
    # 9 samples: below the floor.
    for _ in range(9):
        await _resolved(store, "task_create", 0.95, "approved")
    out = await CalibrationReportTool(store).run({})
    assert "No allowlist candidates yet" in out

    # 10th sample is a denial: still no candidate (zero-denials rule).
    await _resolved(store, "task_create", 0.95, "denied")
    out = await CalibrationReportTool(store).run({})
    assert "No allowlist candidates yet" in out


async def test_report_empty_state(db):
    out = await CalibrationReportTool(ApprovalStore(db)).run({"days": 7})
    assert "No predicted-and-resolved approvals" in out


def test_tool_is_read_only():
    assert CalibrationReportTool.classification is ActionClass.READ_ONLY
    assert format_report([], 7)  # pure formatter handles empty
